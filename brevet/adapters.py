"""Framework adapters.

Brevet binds to whatever you already build on. Every supported framework
exposes the same seam (invoke with a task, observe a trace, intercept the
human-facing output), and an adapter is nothing more than that seam mapping.

Built-in adapters (auto-detected from the wrapped object's class/module):

    callable            any fn(task[, context]) -> str
    langgraph           compiled LangGraph apps (.stream / .invoke)
    deepagents          create_deep_agent() agents
    claude_agent_sdk    Claude Agent SDK clients (async .query)
    autogen             AutoGen AgentChat (.run) / classic (.generate_reply)
    llamaindex          LlamaIndex agents (.chat / async .run)
    pydantic_ai         Pydantic AI Agent (.run_sync)
    google_adk          Google ADK runners (.run / InMemoryRunner)
    crewai              CrewAI Crew (.kickoff)
    openai_agents       OpenAI Agents SDK Agent (via agents.Runner)

Community frameworks register in one line:

    @brevet.register_adapter("myframework", prefixes=("myframework",))
    class MyAdapter(BaseAdapter):
        def invoke(self, task, context): ...

Adapters are duck-typed and defensive: they try the framework's stable public
entry points in order and raise a clear error naming the seam if none fit.
Brevet never modifies the wrapped object; execution stays in your framework.
"""

from __future__ import annotations

import asyncio
import inspect
from typing import Any, Callable, Optional

Trace = list[dict[str, Any]]


class AdapterError(RuntimeError):
    pass


def _run_async(coro: Any) -> Any:
    """Run a coroutine to completion, tolerating an already-running loop."""
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(coro)
    import concurrent.futures
    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
        return pool.submit(asyncio.run, coro).result()


def _text_of(obj: Any) -> str:
    """Best-effort extraction of the human-facing text from a framework result."""
    for attr in ("final_output", "output", "response", "content", "raw", "data", "result", "text"):
        if hasattr(obj, attr):
            val = getattr(obj, attr)
            if isinstance(val, str) and val:
                return val
            if val is not None and not callable(val):
                return _text_of(val) if not isinstance(val, (int, float, bool)) else str(val)
    if isinstance(obj, dict):
        msgs = obj.get("messages")
        if msgs:
            return _text_of(msgs[-1])
        for key in ("output", "response", "content", "result"):
            if key in obj:
                return _text_of(obj[key])
    if isinstance(obj, (list, tuple)) and obj:
        return _text_of(obj[-1])
    return str(obj)


class BaseAdapter:
    """The whole adapter contract: one method."""

    name = "base"

    def __init__(self, target: Any):
        self.target = target

    def invoke(self, task: str, context: dict[str, Any]) -> tuple[str, Trace]:
        raise NotImplementedError

    def _fail(self, tried: list[str]) -> tuple[str, Trace]:
        raise AdapterError(
            f"brevet[{self.name}]: could not find an entry point on "
            f"{type(self.target).__name__} (tried: {', '.join(tried)}). "
            f"Wrap a lower-level object, or register a custom adapter: "
            f"brevet.register_adapter(...)"
        )


# --------------------------------------------------------------- registry

_REGISTRY: dict[str, type[BaseAdapter]] = {}
_PREFIXES: list[tuple[str, str]] = []  # (module_prefix, adapter_name)


def register_adapter(name: str, *, prefixes: tuple[str, ...] = ()) -> Callable:
    def deco(cls: type[BaseAdapter]) -> type[BaseAdapter]:
        cls.name = name
        _REGISTRY[name] = cls
        for p in prefixes:
            _PREFIXES.append((p, name))
        return cls
    return deco


def get_adapter(name: str) -> type[BaseAdapter]:
    if name not in _REGISTRY:
        raise AdapterError(f"unknown adapter '{name}'; known: {sorted(_REGISTRY)}")
    return _REGISTRY[name]


def detect(target: Any) -> str:
    """Auto-detect the right adapter from the class hierarchy's modules."""
    modules: list[str] = []
    for cls in type(target).__mro__:
        modules.append(getattr(cls, "__module__", "") or "")
    modules.append(getattr(target, "__module__", "") or "")
    # longest prefix wins (e.g. 'google.adk' beats 'google')
    best: Optional[tuple[int, str]] = None
    for prefix, name in _PREFIXES:
        for mod in modules:
            if mod == prefix or mod.startswith(prefix + "."):
                if best is None or len(prefix) > best[0]:
                    best = (len(prefix), name)
    if best:
        return best[1]
    if callable(target):
        return "callable"
    raise AdapterError(
        f"brevet could not auto-detect a framework for {type(target).__name__}; "
        f"pass adapter=<name>, one of {sorted(_REGISTRY)}"
    )


# --------------------------------------------------------------- adapters

@register_adapter("callable")
class CallableAdapter(BaseAdapter):
    def invoke(self, task: str, context: dict[str, Any]) -> tuple[str, Trace]:
        fn = self.target
        try:
            out = fn(task, context) if len(inspect.signature(fn).parameters) >= 2 else fn(task)
        except (ValueError, TypeError):
            out = fn(task)
        if inspect.iscoroutine(out):
            out = _run_async(out)
        return _text_of(out), [{"step": "invoke", "input": task}]


@register_adapter("langgraph", prefixes=("langgraph",))
class LangGraphAdapter(BaseAdapter):
    def invoke(self, task: str, context: dict[str, Any]) -> tuple[str, Trace]:
        payload = {"messages": [("user", task)]}
        if hasattr(self.target, "stream"):
            trace: Trace = []
            final: Any = None
            # stream_mode="values": each chunk is the full state; the last
            # chunk is the final state carrying the answer.
            for state in self.target.stream(payload, stream_mode="values"):
                trace.append({"step": "state", "data": str(state)[:2000]})
                final = state
            return _text_of(final), trace
        if hasattr(self.target, "invoke"):
            result = self.target.invoke(payload)
            return _text_of(result), [{"step": "invoke"}]
        return self._fail(["stream", "invoke"])


@register_adapter("deepagents", prefixes=("deepagents",))
class DeepAgentsAdapter(BaseAdapter):
    def invoke(self, task: str, context: dict[str, Any]) -> tuple[str, Trace]:
        if hasattr(self.target, "invoke"):
            result = self.target.invoke({"messages": [{"role": "user", "content": task}]})
            msgs = result.get("messages", []) if isinstance(result, dict) else []
            return _text_of(result), [{"step": "invoke", "n_messages": len(msgs)}]
        return self._fail(["invoke"])


@register_adapter("claude_agent_sdk", prefixes=("claude_agent_sdk", "claude_code_sdk"))
class ClaudeAgentSDKAdapter(BaseAdapter):
    def invoke(self, task: str, context: dict[str, Any]) -> tuple[str, Trace]:
        client = self.target

        async def _go() -> tuple[str, Trace]:
            trace: Trace = []
            texts: list[str] = []
            if hasattr(client, "connect"):
                await client.connect()
            await client.query(task)
            async for message in client.receive_response():
                trace.append({"step": type(message).__name__})
                for block in getattr(message, "content", []) or []:
                    text = getattr(block, "text", None)
                    if text:
                        texts.append(text)
                if type(message).__name__ == "ResultMessage":
                    result = getattr(message, "result", None)
                    if result:
                        texts.append(result)
                    break
            return ("\n".join(texts) or "", trace)

        if hasattr(client, "query") and hasattr(client, "receive_response"):
            return _run_async(_go())
        return self._fail(["query/receive_response (ClaudeSDKClient)"])


@register_adapter("autogen", prefixes=("autogen", "autogen_agentchat", "autogen_core"))
class AutoGenAdapter(BaseAdapter):
    def invoke(self, task: str, context: dict[str, Any]) -> tuple[str, Trace]:
        t = self.target
        if hasattr(t, "run"):  # autogen-agentchat AssistantAgent / teams
            result = t.run(task=task)
            if inspect.iscoroutine(result):
                result = _run_async(result)
            msgs = getattr(result, "messages", []) or []
            trace = [{"step": "message", "source": getattr(m, "source", "?")} for m in msgs]
            return _text_of(msgs[-1] if msgs else result), trace
        if hasattr(t, "generate_reply"):  # classic ConversableAgent
            reply = t.generate_reply(messages=[{"role": "user", "content": task}])
            if inspect.iscoroutine(reply):
                reply = _run_async(reply)
            return _text_of(reply), [{"step": "generate_reply"}]
        return self._fail(["run", "generate_reply"])


@register_adapter("llamaindex", prefixes=("llama_index",))
class LlamaIndexAdapter(BaseAdapter):
    def invoke(self, task: str, context: dict[str, Any]) -> tuple[str, Trace]:
        t = self.target
        if hasattr(t, "chat"):  # AgentRunner / ReActAgent
            resp = t.chat(task)
            return _text_of(resp), [{"step": "chat"}]
        if hasattr(t, "run"):  # AgentWorkflow / newer workflow agents
            result = t.run(user_msg=task)
            if inspect.iscoroutine(result) or hasattr(result, "__await__"):
                result = _run_async(result)
            return _text_of(result), [{"step": "run"}]
        if hasattr(t, "query"):  # query engines
            return _text_of(t.query(task)), [{"step": "query"}]
        return self._fail(["chat", "run", "query"])


@register_adapter("pydantic_ai", prefixes=("pydantic_ai",))
class PydanticAIAdapter(BaseAdapter):
    def invoke(self, task: str, context: dict[str, Any]) -> tuple[str, Trace]:
        t = self.target
        if hasattr(t, "run_sync"):
            result = t.run_sync(task)
            usage = getattr(result, "usage", None)
            trace: Trace = [{"step": "run_sync",
                             "usage": str(usage() if callable(usage) else usage)[:500]}]
            return _text_of(result), trace
        if hasattr(t, "run"):
            result = _run_async(t.run(task))
            return _text_of(result), [{"step": "run"}]
        return self._fail(["run_sync", "run"])


@register_adapter("google_adk", prefixes=("google.adk",))
class GoogleADKAdapter(BaseAdapter):
    def invoke(self, task: str, context: dict[str, Any]) -> tuple[str, Trace]:
        t = self.target
        if hasattr(t, "run"):  # Runner / InMemoryRunner
            trace: Trace = []
            texts: list[str] = []
            try:
                events = t.run(
                    user_id=context.get("user_id", "brevet"),
                    session_id=context.get("session_id", context.get("task_id", "brevet")),
                    new_message=task,
                )
            except TypeError:
                events = t.run(task)
            for event in events:
                trace.append({"step": type(event).__name__})
                content = getattr(event, "content", None)
                parts = getattr(content, "parts", None) if content else None
                for part in parts or []:
                    text = getattr(part, "text", None)
                    if text:
                        texts.append(text)
            return ("\n".join(texts) or "", trace)
        return self._fail(["run (Runner/InMemoryRunner)"])


@register_adapter("crewai", prefixes=("crewai",))
class CrewAIAdapter(BaseAdapter):
    def invoke(self, task: str, context: dict[str, Any]) -> tuple[str, Trace]:
        t = self.target
        if hasattr(t, "kickoff"):
            result = t.kickoff(inputs={"task": task, "input": task})
            return _text_of(result), [{"step": "kickoff"}]
        return self._fail(["kickoff"])


@register_adapter("openai_agents", prefixes=("agents",))
class OpenAIAgentsAdapter(BaseAdapter):
    def invoke(self, task: str, context: dict[str, Any]) -> tuple[str, Trace]:
        try:
            from agents import Runner  # openai-agents SDK
        except ImportError as e:
            raise AdapterError("brevet[openai_agents]: `pip install openai-agents`") from e
        result = Runner.run_sync(self.target, task)
        items = getattr(result, "new_items", []) or []
        return _text_of(result), [{"step": type(i).__name__} for i in items]
