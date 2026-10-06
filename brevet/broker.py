"""The tool broker: the agent's tool calls checked against what the manifest grants.

The manifest declares the agent's tools in four tiers (``bindings.tools``),
the read/act contract:

    read            looks things up; always allowed
    suggest         drafts something a person decides on; always allowed
    act             changes something; allowed on the trial and production
                    channels, never in shadow, where nothing is acted on
    controlled_act  changes something consequential; allowed only in
                    production, and only with a person's grant for that call

A name may be a pattern (``mcp__github__*``). A tool matching several tiers
takes the strictest. Declaring any tool switches the broker on, and from then
on a tool that is not declared is refused. ``runtime_safety.loop.budgets``
caps the tool calls and tool errors per task.

Every refusal and every act or controlled_act call is recorded on the
evidence chain as a ``brevet.tool_call`` envelope, with the person who
granted it where one did.

The broker reaches the tools of a wrapped agent wherever the framework lets
it: Claude Agent SDK hooks, OpenAI Agents function tools, LangChain and
LangGraph tools, Pydantic AI tools, AutoGen tools and any agent that keeps
its tools in a ``tools`` list. Tools in your own code go through
``agent.tool`` (a decorator). In Claude Code, ``brevet hook`` applies the
released policy to Claude's tool calls as a PreToolUse hook.
"""

from __future__ import annotations

import contextvars
import fnmatch
import functools
import inspect
import json
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from brevet.identity import require_identity

TIERS = ("read", "suggest", "act", "controlled_act")
_STRICTNESS = {tier: i for i, tier in enumerate(TIERS)}
_TASK: contextvars.ContextVar[str | None] = contextvars.ContextVar("brevet_task", default=None)


class ToolRefused(PermissionError):
    """A tool call the manifest does not allow."""


@dataclass
class Decision:
    allowed: bool
    tool: str
    tier: str | None
    reason: str
    granted_by: str | None = None
    ask: bool = False   # for hooks: let the client ask the person


class ToolPolicy:
    """The read/act contract as one manifest declares it."""

    def __init__(self, manifest: Any):
        tools = (manifest.bindings or {}).get("tools") or {}
        self.tiers = {tier: [str(p) for p in (tools.get(tier) or [])] for tier in TIERS}
        self.enforced = any(self.tiers.values())
        self.channel = (manifest.release or {}).get("channel", "shadow")
        budgets = ((manifest.runtime_safety or {}).get("loop") or {}).get("budgets") or {}
        self.max_calls = budgets.get("max_tool_calls")
        self.max_errors = budgets.get("max_tool_errors")

    def tier_of(self, name: str) -> str | None:
        found = [tier for tier in TIERS
                 if any(fnmatch.fnmatchcase(name, pattern) for pattern in self.tiers[tier])]
        return max(found, key=_STRICTNESS.__getitem__) if found else None

    def decide(self, name: str, *, granted_by: str | None = None,
               calls: int = 0, errors: int = 0) -> Decision:
        if not self.enforced:
            return Decision(True, name, None, "no tools are declared, so none are brokered")
        tier = self.tier_of(name)
        if tier is None:
            return Decision(False, name, None,
                            f"{name} is not declared under bindings.tools")
        if self.max_calls is not None and calls >= int(self.max_calls):
            return Decision(False, name, tier,
                            f"the task reached its budget of {self.max_calls} tool calls")
        if self.max_errors is not None and errors >= int(self.max_errors):
            return Decision(False, name, tier,
                            f"the task reached its budget of {self.max_errors} tool errors")
        if tier in ("read", "suggest"):
            return Decision(True, name, tier, f"{tier} tools are always allowed")
        if self.channel == "shadow":
            return Decision(False, name, tier,
                            f"{name} is {'an' if tier == 'act' else 'a'} {tier} tool, and on "
                            f"the shadow channel nothing is acted on")
        if tier == "act":
            return Decision(True, name, tier, f"act tools run on the {self.channel} channel")
        if self.channel != "production":
            return Decision(False, name, tier,
                            f"{name} is a controlled_act tool, which runs only in production")
        if not granted_by:
            return Decision(False, name, tier,
                            f"{name} is a controlled_act tool and needs a person's grant "
                            f"for this call", ask=True)
        return Decision(True, name, tier, f"granted by {granted_by}", granted_by=granted_by)


class ToolBroker:
    """Checks each tool call of one agent and records the ones that matter.

    ``grantor`` is asked for controlled_act calls: a callable taking the tool
    name and its arguments and returning the ``human:`` or ``mission_group:``
    identity that grants the call, or None to refuse it."""

    def __init__(self, manifest: Callable[[], Any], ledger: Any, *,
                 agent: Callable[[], str] | None = None,
                 grantor: Callable[[str, dict[str, Any]], str | None] | None = None,
                 record: str = "act"):
        self._manifest = manifest
        self.ledger = ledger
        self._agent = agent or (lambda: getattr(manifest(), "agent", "agent"))
        self.grantor = grantor
        self.record = record  # "act" (refusals and act tiers), "all" or "refusals"
        self._calls: dict[str | None, int] = {}
        self._errors: dict[str | None, int] = {}
        self.guarded: list[str] = []
        self._active: set[str] = set()  # tasks running now, for tools run in other threads

    @staticmethod
    def task(task_id: str | None) -> contextvars.Token:
        """Mark the calls that follow as belonging to one task."""
        return _TASK.set(task_id)

    @staticmethod
    def reset(token: contextvars.Token) -> None:
        _TASK.reset(token)

    def begin(self, task_id: str) -> contextvars.Token:
        self._active.add(task_id)
        return _TASK.set(task_id)

    def end(self, task_id: str, token: contextvars.Token) -> None:
        self._active.discard(task_id)
        _TASK.reset(token)

    def _task_id(self) -> str | None:
        """The task a call belongs to. A framework may run a tool in another
        thread, out of reach of the task's context; then the call belongs to
        the one task running, or to no task when several are."""
        task_id = _TASK.get()
        if task_id is None and len(self._active) == 1:
            task_id = next(iter(self._active))
        return task_id

    def check(self, name: str, args: dict[str, Any] | None = None) -> Decision:
        task_id = self._task_id()
        policy = ToolPolicy(self._manifest())
        granted = None
        if policy.enforced and policy.tier_of(name) == "controlled_act" and self.grantor:
            who = self.grantor(name, args or {})
            granted = require_identity(who, role="tool grantor") if who else None
        decision = policy.decide(name, granted_by=granted,
                                 calls=self._calls.get(task_id, 0) if task_id else 0,
                                 errors=self._errors.get(task_id, 0) if task_id else 0)
        if decision.allowed and task_id:
            self._calls[task_id] = self._calls.get(task_id, 0) + 1
        wanted = (self.record == "all" or not decision.allowed
                  or (self.record == "act" and decision.tier in ("act", "controlled_act")))
        if policy.enforced and wanted and self.ledger is not None:
            body = {"agent": self._agent(), "task_id": task_id, "tool": name,
                    "tier": decision.tier, "allowed": decision.allowed,
                    "reason": decision.reason, "channel": policy.channel}
            if decision.ask:
                body["asked"] = True
            if decision.granted_by:
                body["granted_by"] = decision.granted_by
            if args:
                body["args_digest"] = _digest(args)
            self.ledger.append("brevet.tool_call", body, refs=[task_id] if task_id else [])
        return decision

    def failed(self) -> None:
        task_id = self._task_id()
        if task_id:
            self._errors[task_id] = self._errors.get(task_id, 0) + 1

    def guard(self, fn: Callable, name: str | None = None, *,
              on_refusal: str = "raise") -> Callable:
        """``fn``, with every call checked first. A refused call raises
        ``ToolRefused`` (``on_refusal="raise"``), or returns the refusal as
        the tool's result (``"message"``), so a framework's model reads why
        and carries on."""
        tool = name or getattr(fn, "__name__", "tool")
        if getattr(fn, "__brevet_guarded__", False):
            if getattr(fn, "__brevet_broker__", None) is self:
                return fn
            fn = fn.__brevet_original__  # guarded by another wrapper: this policy now applies

        def refused(decision: Decision) -> Any:
            message = f"Refused by Brevet: {decision.reason}"
            if on_refusal == "message":
                return message
            if on_refusal == "pair":  # tools that return (content, artifact)
                return message, None
            raise ToolRefused(f"refused by Brevet: {decision.reason}")

        if inspect.iscoroutinefunction(fn):
            @functools.wraps(fn)
            async def guarded_async(*args, **kwargs):
                decision = self.check(tool, _args(fn, args, kwargs))
                if not decision.allowed:
                    return refused(decision)
                try:
                    return await fn(*args, **kwargs)
                except Exception:
                    self.failed()
                    raise
            _mark(guarded_async, self, fn)
            return guarded_async

        @functools.wraps(fn)
        def guarded(*args, **kwargs):
            decision = self.check(tool, _args(fn, args, kwargs))
            if not decision.allowed:
                return refused(decision)
            try:
                return fn(*args, **kwargs)
            except Exception:
                self.failed()
                raise
        _mark(guarded, self, fn)
        return guarded


def _mark(wrapper: Callable, broker: ToolBroker, original: Callable) -> None:
    wrapper.__brevet_guarded__ = True
    wrapper.__brevet_broker__ = broker
    wrapper.__brevet_original__ = original


def _mine(fn: Any, broker: ToolBroker) -> bool:
    return getattr(fn, "__brevet_broker__", None) is broker


def _args(fn: Callable, args: tuple, kwargs: dict) -> dict[str, Any]:
    try:
        bound = inspect.signature(fn).bind_partial(*args, **kwargs)
        return dict(bound.arguments)
    except (TypeError, ValueError):
        return {"args": list(args), **kwargs}


def _digest(args: dict[str, Any]) -> str:
    from brevet.canonical import object_sha256
    try:
        return object_sha256(json.loads(json.dumps(args, default=str)))
    except (TypeError, ValueError):
        return object_sha256(repr(sorted(args)))


# ------------------------------------------------------------- installing

def install(target: Any, broker: ToolBroker) -> list[str]:
    """Route the wrapped agent's tools through the broker where its framework
    allows. Safe to repeat: tools added since are guarded, guarded ones are
    left alone. Returns the names of the tools now guarded."""
    names: list[str] = []
    _install(target, broker, names, set(), depth=0)
    broker.guarded = sorted(set(names))
    return broker.guarded


def _install(target: Any, broker: ToolBroker, names: list[str], seen: set[int],
             depth: int) -> None:
    if target is None or id(target) in seen or depth > 6 \
            or isinstance(target, (str, bytes, int, float, bool)):
        return
    seen.add(id(target))
    options = getattr(target, "options", None)
    if options is not None and hasattr(options, "hooks"):  # Claude Agent SDK client
        _install_claude_sdk(options, broker, names)
    if hasattr(target, "hooks") and hasattr(target, "can_use_tool"):  # ClaudeAgentOptions
        _install_claude_sdk(target, broker, names)
    nodes = getattr(target, "nodes", None)
    if isinstance(nodes, dict):  # LangGraph compiled graphs, subgraphs included
        for node in nodes.values():
            bound = getattr(node, "bound", None)
            tools = getattr(bound, "tools_by_name", None)
            if isinstance(tools, dict):
                for tool in tools.values():
                    _guard_tool(tool, broker, names, seen, depth)
            elif isinstance(getattr(bound, "nodes", None), dict):
                _install(bound, broker, names, seen, depth + 1)
    if getattr(target, "_function_toolset", None) is not None:  # Pydantic AI
        _install_pydantic_ai(target, broker, names)
        return
    for attr in ("_tools", "tools"):
        tools = getattr(target, attr, None)
        if isinstance(tools, dict):
            tools = list(tools.values())
        if isinstance(tools, list):
            for i, tool in enumerate(tools):
                if isinstance(tool, (str, bytes, int, float, bool)):
                    continue
                if inspect.isfunction(tool) or inspect.ismethod(tool):
                    tools[i] = broker.guard(tool, on_refusal="message")
                    names.append(getattr(tool, "__name__", "tool"))
                else:
                    _guard_tool(tool, broker, names, seen, depth)
    workbench = getattr(target, "_workbench", None)  # AutoGen AgentChat
    for bench in workbench if isinstance(workbench, (list, tuple)) else [workbench]:
        if bench is not None:
            _install(bench, broker, names, seen, depth + 1)
    for attr in ("handoffs", "sub_agents", "agents"):  # nested agents
        subs = getattr(target, attr, None)
        if isinstance(subs, dict):
            subs = list(subs.values())
        if isinstance(subs, (list, tuple)):
            for sub in subs:
                _install(getattr(sub, "agent", sub), broker, names, seen, depth + 1)


def _install_pydantic_ai(agent: Any, broker: ToolBroker, names: list[str]) -> None:
    """Pydantic AI runs each tool through the function its ``Tool`` keeps
    (``function_schema.function``), whichever toolset copy the run uses, so
    that function is guarded: for the agent's own tools, for function
    toolsets passed in, and for the toolsets dynamic factories build, as
    they are built. Other toolsets, such as MCP servers, are guarded at
    ``call_tool``."""

    def guard_toolset(toolset: Any) -> None:
        tools = getattr(toolset, "tools", None)
        if not isinstance(tools, dict):
            _guard_toolset(toolset, broker)
            return
        for name, tool in tools.items():
            schema = getattr(tool, "function_schema", None)
            fn = getattr(schema, "function", None)
            if callable(fn) and not _mine(fn, broker):
                try:
                    schema.function = broker.guard(fn, str(name), on_refusal="message")
                except (AttributeError, TypeError):
                    continue
            names.append(str(name))

    guard_toolset(agent._function_toolset)
    for toolset in getattr(agent, "_user_toolsets", None) or []:
        guard_toolset(toolset)
    for dynamic in getattr(agent, "_dynamic_toolsets", None) or []:
        func = getattr(dynamic, "toolset_func", None)
        if not callable(func) or _mine(func, broker):
            continue
        original = getattr(func, "__brevet_original__", func)

        def built(ctx, _original=original):
            made = _original(ctx)
            if inspect.isawaitable(made):
                async def later():
                    toolset = await made
                    if toolset is not None:
                        guard_toolset(toolset)
                    return toolset
                return later()
            if made is not None:
                guard_toolset(made)
            return made
        _mark(built, broker, original)
        try:
            dynamic.toolset_func = built
        except (AttributeError, TypeError):
            continue
    names.append("pydantic_ai:*")


def _guard_toolset(toolset: Any, broker: ToolBroker) -> None:
    call = getattr(toolset, "call_tool", None)
    if not callable(call) or _mine(call, broker):
        return
    original = getattr(call, "__brevet_original__", call)

    async def call_tool(name, tool_args, ctx, tool, _call=original):
        if getattr(getattr(tool, "tool_def", None), "kind", None) != "output":
            decision = broker.check(str(name), tool_args if isinstance(tool_args, dict) else {})
            if not decision.allowed:
                return f"Refused by Brevet: {decision.reason}"
        return await _call(name, tool_args, ctx, tool)
    _mark(call_tool, broker, original)
    try:
        toolset.call_tool = call_tool
    except (AttributeError, TypeError):
        return


def _guard_tool(tool: Any, broker: ToolBroker, names: list[str], seen: set[int],
                depth: int) -> None:
    name = str(getattr(tool, "name", None) or getattr(tool, "__name__", "tool"))
    invoke = getattr(tool, "on_invoke_tool", None)
    if callable(invoke):  # OpenAI Agents function tools, agents as tools included
        if not _mine(invoke, broker):
            original = getattr(invoke, "__brevet_original__", invoke)

            async def on_invoke(ctx, raw, _invoke=original, _name=name):
                try:
                    args = json.loads(raw) if raw else {}
                except ValueError:
                    args = {"input": raw}
                decision = broker.check(_name, args if isinstance(args, dict) else {"input": args})
                if not decision.allowed:
                    return f"Refused by Brevet: {decision.reason}"
                out = _invoke(ctx, raw)
                return await out if inspect.isawaitable(out) else out
            _mark(on_invoke, broker, original)
            try:
                tool.on_invoke_tool = on_invoke
            except (AttributeError, TypeError):
                return
        names.append(name)
        _install(getattr(tool, "_agent_instance", None), broker, names, seen, depth + 1)
        return
    pair = getattr(tool, "response_format", None) == "content_and_artifact"
    for attr in ("func", "coroutine", "function", "fn", "_func", "_fn", "async_fn",
                 "_async_fn"):
        fn = getattr(tool, attr, None)
        if callable(fn) and not _mine(fn, broker):
            try:
                object.__setattr__(tool, attr, broker.guard(
                    fn, name, on_refusal="pair" if pair else "message"))
                names.append(name)
            except (AttributeError, TypeError):
                continue
        elif callable(fn):
            names.append(name)


def _install_claude_sdk(options: Any, broker: ToolBroker, names: list[str]) -> None:
    """A PreToolUse hook that applies the policy to every Claude tool call."""
    try:
        from claude_agent_sdk import HookMatcher
    except ImportError:
        return

    async def pre_tool_use(input_data, tool_use_id, context):
        name = str(input_data.get("tool_name", ""))
        decision = broker.check(name, input_data.get("tool_input") or {})
        if decision.allowed:
            return {}
        return {"hookSpecificOutput": {"hookEventName": "PreToolUse",
                                       "permissionDecision": "ask" if decision.ask else "deny",
                                       "permissionDecisionReason": decision.reason}}
    _mark(pre_tool_use, broker, pre_tool_use)
    hooks = dict(options.hooks or {})
    others = [m for m in hooks.get("PreToolUse") or []
              if not any(getattr(h, "__brevet_guarded__", False) for h in getattr(m, "hooks", []))]
    mine = [m for m in hooks.get("PreToolUse") or []
            if any(_mine(h, broker) for h in getattr(m, "hooks", []))]
    if not mine:  # replaces another wrapper's hook, keeps the user's own
        hooks["PreToolUse"] = [HookMatcher(matcher=None, hooks=[pre_tool_use]), *others]
        options.hooks = hooks
    names.append("claude_agent_sdk:*")


# ------------------------------------------------------------- Claude Code hook

def hook_decision(event: dict[str, Any], manifest: Any, ledger: Any | None,
                  channel: str | None = None) -> dict[str, Any]:
    """The answer to one Claude Code hook event. PreToolUse: nothing when the
    call is allowed, a denial (or a request to ask the person, for a
    controlled_act call) when it is not. PostToolUse: a controlled_act call
    the person allowed is recorded as granted. ``manifest`` should be the
    released manifest and ``channel`` the released channel (see
    ``brevet.releases.released_manifest``), never unreleased edits."""
    if channel is not None:
        manifest = manifest.model_copy(update={"release": {**(manifest.release or {}),
                                                           "channel": channel}})
    policy = ToolPolicy(manifest)
    name = str(event.get("tool_name", ""))
    session = event.get("session_id")
    event_name = event.get("hook_event_name", "PreToolUse")
    if event_name == "PostToolUse":
        if policy.enforced and policy.tier_of(name) == "controlled_act" and ledger is not None:
            ledger.append("brevet.tool_call", {
                "agent": manifest.agent, "task_id": session, "tool": name,
                "tier": "controlled_act", "allowed": True, "channel": policy.channel,
                "granted_by": "the person, asked by Claude Code"},
                refs=[session] if session else [])
        return {}
    if event_name != "PreToolUse":
        return {}
    budgeted = policy.max_calls is not None and ledger is not None
    broker = ToolBroker(lambda: manifest, ledger, record="all" if budgeted else "act")
    if budgeted and session:
        broker._calls[session] = sum(
            1 for e in ledger.read("brevet.tool_call")
            if e["body"].get("task_id") == session and e["body"].get("allowed"))
    token = ToolBroker.task(session)
    try:
        decision = broker.check(name, event.get("tool_input") or {})
    finally:
        ToolBroker.reset(token)
    if decision.allowed:
        return {}
    return {"hookSpecificOutput": {"hookEventName": "PreToolUse",
                                   "permissionDecision": "ask" if decision.ask else "deny",
                                   "permissionDecisionReason": f"Brevet: {decision.reason}"}}
