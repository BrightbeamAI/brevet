"""The harness bill of materials and the drift check.

A release locks the learned capabilities and, beside them, the harness the
agent runs with:

    files   the files the manifest names in ``bindings.harness_files``:
            prompts, skills, tool code, MCP configuration, anything whose
            change alters what the agent does
    agent   what the live agent object exposes: its code, instructions,
            model and settings, tools (with their descriptions and input
            schemas), MCP servers, sub-agents and graph structure
    env     the versions of Brevet, Python and the agent framework's
            libraries

Only digests and short labels are kept, never content. Code from Python
itself, or from the framework packages whose versions ``env`` records, is
recorded by name rather than source, so a framework upgrade shows up under
``env`` rather than as a change to the agent; any other code, your own
installed packages included, is hashed. Secret values (API keys, passwords,
tokens in ``env`` and ``headers`` maps) count by name only.

Before each run the wrapper takes the same inventory again and compares it
with the release. Outside the shadow channel, a file or agent component that
changed without a release stops the run; library changes are recorded but do
not stop it, unless the manifest's ``runtime_safety.harness`` says otherwise.
Each distinct drift is recorded once as a ``brevet.drift`` envelope.
"""

from __future__ import annotations

import contextlib
import contextvars
import dataclasses
import functools
import hashlib
import inspect
import os
import re
import sys
import sysconfig
from importlib import metadata
from pathlib import Path
from typing import Any

from brevet.canonical import object_sha256
from brevet.models import AgentManifest, CapabilitiesLock, HarnessComponent

#: Distributions whose versions describe each framework's runtime.
FRAMEWORK_DISTRIBUTIONS: dict[str, tuple[str, ...]] = {
    "langgraph": ("langgraph", "langgraph-prebuilt", "langgraph-checkpoint", "langchain-core",
                  "langchain"),
    "deepagents": ("deepagents", "langgraph", "langgraph-prebuilt", "langgraph-checkpoint",
                   "langchain-core", "langchain"),
    "claude_agent_sdk": ("claude-agent-sdk",),
    "autogen": ("autogen-agentchat", "autogen-core", "autogen-ext"),
    "llamaindex": ("llama-index-core", "llama-index"),
    "pydantic_ai": ("pydantic-ai", "pydantic-ai-slim", "pydantic-graph"),
    "google_adk": ("google-adk", "google-genai"),
    "crewai": ("crewai", "crewai-tools"),
    "openai_agents": ("openai-agents", "openai"),
}
DEFAULT_POLICY = {"on_drift": "block", "env": "record"}
_ALLOWED_POLICY = {"on_drift": ("block", "record"), "env": ("record", "block")}
MAX_FILES = 5000
_SKIP_DIRS = {".brevet", ".git", "__pycache__", "node_modules", ".venv", "venv"}
_SKIP_NAMES = {".DS_Store", "Thumbs.db", "desktop.ini"}
_CACHE_FROM = 1 << 20   # hash small files every time; cache only large ones
_DIGESTS: dict[tuple[str, int, int], str] = {}

_INSTRUCTIONS = ("instructions", "system_prompt", "system_message", "instruction")
_SETTINGS = ("model_settings", "temperature", "max_turns", "max_iterations",
             "permission_mode", "allowed_tools", "disallowed_tools", "output_type",
             "setting_sources")
_SUBAGENTS = ("handoffs", "sub_agents", "agents")
_TOOL_FUNCTIONS = ("func", "fn", "function", "coroutine", "async_fn", "_func", "_fn",
                   "__wrapped__")
_ENV_MAPS = {"env", "headers", "extra_headers", "default_headers", "http_headers"}
_SECRET = re.compile(r"(^|_)(api_?key|access_token|auth_token|refresh_token|secret|"
                     r"client_secret|password|passwd|authorization|bearer)$", re.IGNORECASE)
_ENV_SECRET = re.compile(r"(^|[_-])(token|pat|key|secret|password|passwd|authorization|"
                         r"bearer|credentials?)$", re.IGNORECASE)
_STDLIB = tuple(sorted({str(Path(p).resolve()) for key in ("stdlib", "platstdlib")
                        if (p := sysconfig.get_paths().get(key))}))
_FRAMEWORK: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "brevet_framework", default=None)


# ------------------------------------------------------------- digests

def lock_digest(lock: CapabilitiesLock) -> str:
    """The digest a release records for its lock. A lock with no harness
    section keeps the digest earlier releases used."""
    caps = [r.model_dump() for r in lock.resolved]
    if not lock.harness and not lock.harness_sources:
        return object_sha256(caps)
    return object_sha256({"agent": lock.agent, "agent_version": lock.agent_version,
                          "capabilities": caps,
                          "harness": [h.model_dump() for h in lock.harness],
                          "harness_sources": sorted(lock.harness_sources)})


def _file_digest(path: Path) -> str:
    st = path.stat()
    key = (str(path), st.st_size, st.st_mtime_ns)
    if st.st_size >= _CACHE_FROM and key in _DIGESTS:
        return _DIGESTS[key]
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 16), b""):
            h.update(chunk)
    digest = "sha256:" + h.hexdigest()
    if st.st_size >= _CACHE_FROM:
        if len(_DIGESTS) > 10_000:
            _DIGESTS.clear()
        _DIGESTS[key] = digest
    return digest


def _qualname(obj: Any) -> str:
    mod = getattr(obj, "__module__", "") or ""
    name = getattr(obj, "__qualname__", None) or getattr(obj, "__name__", None) \
        or type(obj).__qualname__
    return f"{mod}.{name}" if mod else str(name)


@functools.cache
def _library_files(framework: str | None) -> frozenset[str]:
    """Source files of the packages whose versions ``env`` records: Brevet
    and the framework's own distributions."""
    out: set[str] = set()
    for dist in ("brevet", *FRAMEWORK_DISTRIBUTIONS.get(framework or "", ())):
        try:
            found = metadata.distribution(dist)
        except metadata.PackageNotFoundError:
            continue
        for f in found.files or ():
            if f.suffix == ".py":
                with contextlib.suppress(OSError, ValueError):
                    out.add(str(Path(str(found.locate_file(f))).resolve()))
    return frozenset(out)


def _is_library(obj: Any) -> bool:
    """True for code from Python itself or from a package whose version the
    ``env`` components record, so its changes arrive as recorded upgrades.
    Any other code, the user's own installed packages included, is not."""
    target = obj.func if isinstance(obj, functools.partial) else obj
    target = getattr(target, "__func__", target)  # bound methods
    if not (inspect.isclass(target) or inspect.isfunction(target)
            or inspect.ismethod(target) or inspect.isbuiltin(target)):
        target = type(target)
    module = inspect.getmodule(target)
    if module is None:
        return False
    name = getattr(module, "__name__", "")
    if name == "builtins" or name in sys.builtin_module_names:
        return True
    file = getattr(module, "__file__", None)
    if not file:
        return False
    path = Path(file).resolve()
    if "site-packages" in path.parts or "dist-packages" in path.parts:
        return str(path) in _library_files(_FRAMEWORK.get())
    return any(str(path).startswith(root + os.sep) for root in _STDLIB)


def _source(fn: Any) -> str:
    if isinstance(fn, functools.partial):
        # bound arguments by type and value, never by memory address
        return (_source(fn.func) + object_sha256(_plain(list(fn.args)))
                + object_sha256(_plain(dict(fn.keywords))))
    if _is_library(fn):
        return _qualname(fn)  # a library upgrade is recorded under env
    try:
        return inspect.getsource(fn)
    except (OSError, TypeError):
        return _qualname(fn)


def _redact(value: Any, env: bool = False) -> Any:
    """Secret values by name only, so a rotated key or token is not a change
    to the agent; every other setting, in ``env`` and ``headers`` maps too,
    keeps its value."""
    if isinstance(value, dict):
        out: dict[str, Any] = {}
        for k, v in value.items():
            if (_ENV_SECRET if env else _SECRET).search(k) and not isinstance(v, (dict, list)):
                out[k] = "<set>" if v else "<unset>"
            else:
                out[k] = _redact(v, env=k.lower() in _ENV_MAPS)
        return out
    if isinstance(value, list):
        return [_redact(v, env) for v in value]
    return value


def _plain(value: Any, depth: int = 0) -> Any:
    """A deterministic, JSON-able description of a value for hashing. It does
    not recurse into arbitrary objects, whose state may change from run to
    run; it records their type instead."""
    if depth > 6:
        return "..."
    if value is None or isinstance(value, (bool, int, float, str)):
        return value
    if isinstance(value, (list, tuple)):
        return [_plain(v, depth + 1) for v in value]
    if isinstance(value, (set, frozenset)):
        return sorted((_plain(v, depth + 1) for v in value), key=repr)
    if isinstance(value, dict):
        return {str(k): _plain(v, depth + 1) for k, v in sorted(value.items(), key=lambda kv: str(kv[0]))}
    if isinstance(value, type):
        schema = getattr(value, "model_json_schema", None)
        if callable(schema):
            with contextlib.suppress(Exception):  # fall back to the name
                return _plain(schema(), depth + 1)
        return {"type": _qualname(value)}
    dump = getattr(value, "model_dump", None)
    if callable(dump):
        with contextlib.suppress(Exception):  # not every model_dump takes mode
            return _plain(dump(mode="json"), depth + 1)
    if dataclasses.is_dataclass(value):  # field by field; asdict would deep-copy
        return _plain({f.name: getattr(value, f.name, None)
                       for f in dataclasses.fields(value)}, depth + 1)
    if callable(value):
        return {"callable": _qualname(value), "source": _source(value)}
    return {"type": _qualname(type(value))}


def _tool_function(tool: Any) -> Any:
    """The user's function behind a tool object, where the framework keeps it."""
    if inspect.isfunction(tool):
        return tool
    for attr in _TOOL_FUNCTIONS:
        fn = _get(tool, attr)
        if inspect.isfunction(fn) or isinstance(fn, functools.partial):
            return fn
    return None


def _get(obj: Any, attr: str) -> Any:
    try:
        return getattr(obj, attr, None)
    except Exception:  # noqa: BLE001 - a property may raise; treat as absent
        return None


# ------------------------------------------------------------- files

def _label(path: Path, base: Path) -> str:
    try:
        return path.resolve().relative_to(base.resolve()).as_posix()
    except ValueError:
        pass
    try:
        return "~/" + path.resolve().relative_to(Path.home().resolve()).as_posix()
    except ValueError:
        return path.resolve().as_posix()


def _matches(base: Path, pattern: str) -> list[Path]:
    """Paths a pattern names. Uses pathlib, so the folder names around the
    pattern are never read as wildcards and hidden files are included."""
    target = Path(os.path.expanduser(pattern))
    root, rel = (Path(target.anchor), str(target.relative_to(target.anchor))) \
        if target.is_absolute() else (base, pattern)
    if rel in ("", "."):
        return [root]
    try:
        return list(root.glob(rel))
    except (ValueError, OSError):
        return []


def _patterns(manifest: AgentManifest) -> list[str]:
    patterns = (manifest.bindings or {}).get("harness_files") or []
    return [patterns] if isinstance(patterns, str) else [str(p) for p in patterns]


def _collect(patterns: list[str], base: Path, exclude: list[Path] | None,
             exclude_dirs: list[Path] | None) -> dict[str, Path]:
    skip = {p.resolve() for p in (exclude or []) if p is not None}
    skip_dirs = [d.resolve() for d in (exclude_dirs or []) if d is not None]
    found: dict[str, Path] = {}
    for pattern in patterns:
        for path in _matches(base, pattern):
            files = [p for p in path.rglob("*") if p.is_file()] if path.is_dir() else [path]
            for f in files:
                if not f.is_file() or f.name in _SKIP_NAMES:
                    continue
                resolved = f.resolve()
                if resolved in skip or any(d == resolved or d in resolved.parents
                                           for d in skip_dirs):
                    continue
                label = _label(f, base)
                if any(part in _SKIP_DIRS for part in Path(label).parts):
                    continue
                found[label] = f
                if len(found) > MAX_FILES:
                    raise ValueError(f"bindings.harness_files matches more than {MAX_FILES} "
                                     f"files; narrow the patterns")
    return found


def file_components(manifest: AgentManifest, base: Path,
                    exclude: list[Path] | None = None,
                    exclude_dirs: list[Path] | None = None) -> list[HarnessComponent]:
    """Hash every file the manifest's ``bindings.harness_files`` patterns name.
    A pattern may be relative to the manifest's folder, absolute, or start
    with ``~``; a folder includes everything under it. The workspace itself,
    the manifest and the lock are never part of the harness."""
    found = _collect(_patterns(manifest), base, exclude, exclude_dirs)
    return [HarnessComponent(component_id=f"file:{label}", kind="file",
                             digest=_file_digest(path), detail=f"{path.stat().st_size} bytes")
            for label, path in sorted(found.items())]


def unmatched_patterns(manifest: AgentManifest, manifest_path: str | Path | None, *,
                       workdir: str | Path | None = None) -> list[str]:
    """The ``harness_files`` patterns that name no file a release would lock,
    such as a misspelt folder or one inside the workspace."""
    mpath = Path(manifest_path) if manifest_path else None
    base = mpath.parent if mpath else Path(".")
    exclude = [mpath, base / "capabilities.lock"] if mpath else None
    exclude_dirs = [Path(workdir)] if workdir else None
    return [p for p in _patterns(manifest)
            if not _collect([p], base, exclude, exclude_dirs)]


# ------------------------------------------------------------- agent

def describe_agent(target: Any, framework: str | None = None) -> list[HarnessComponent]:
    """Best-effort inventory of a live agent object from public attributes
    that agent frameworks commonly expose. Adapters may refine it.
    ``framework`` names the adapter, so the framework's own code is recorded
    by name and its upgrades under ``env``."""
    token = _FRAMEWORK.set(framework)
    try:
        return _describe(target)
    finally:
        _FRAMEWORK.reset(token)


def _instruction_text(value: Any) -> Any:
    """Instructions as text, from the shapes frameworks keep them in."""
    if isinstance(value, (list, tuple)):
        return [_instruction_text(v) for v in value]
    if isinstance(value, (str, dict)) or value is None:
        return value
    for attr in ("instruction", "content", "text", "function"):
        inner = _get(value, attr)
        if inner is not None and inner is not value:
            return _instruction_text(inner)
    if callable(value):
        return _source(value)
    return _plain(value)


def _tools_of(holder: Any) -> list[Any]:
    """Tools from the places frameworks keep them: a tools list or dict,
    AutoGen's private list and workbenches, a Pydantic AI toolset, and the
    tool nodes of a LangGraph graph."""
    found: list[Any] = []
    for value in (_get(holder, "tools"), _get(holder, "_tools"),
                  _get(_get(holder, "_function_toolset"), "tools")):
        if isinstance(value, dict):
            value = list(value.values())
        if isinstance(value, (list, tuple, set)):
            found += [t for t in value if not isinstance(t, (str, bytes, int, float))]
    benches = _get(holder, "_workbench")
    for bench in benches if isinstance(benches, (list, tuple)) else [benches]:
        tools = _get(bench, "_tools") if bench is not None else None
        if isinstance(tools, (list, tuple)):
            found += list(tools)
    nodes = _get(holder, "nodes")
    if isinstance(nodes, dict):
        for node in nodes.values():
            by_name = _get(_get(node, "bound"), "tools_by_name")
            if isinstance(by_name, dict):
                found += list(by_name.values())
    unique: dict[int, Any] = {}
    for tool in found:
        unique.setdefault(id(tool), tool)
    return list(unique.values())


def _describe(target: Any) -> list[HarnessComponent]:
    out: dict[str, HarnessComponent] = {}

    def add(cid: str, value: Any, detail: str = "") -> None:
        cid = f"agent:{cid}"
        if cid not in out:
            out[cid] = HarnessComponent(component_id=cid, kind="agent",
                                        digest=object_sha256(_redact(_plain(value))),
                                        detail=detail[:120])

    if inspect.isfunction(target) or inspect.ismethod(target) or inspect.isbuiltin(target) \
            or isinstance(target, functools.partial):
        add("code", _source(target), _qualname(target))
        return list(out.values())

    for holder in (target, _get(target, "options")):  # Claude Agent SDK keeps settings in options
        if holder is None:
            continue
        for attr in _INSTRUCTIONS:
            value = _get(holder, attr)
            if isinstance(value, (str, dict)) and value:
                add("instructions", value, attr)
            elif callable(value) and not _is_library(value):  # not a framework method
                add("instructions", _source(value), attr)
        for attr in ("_instructions", "_system_prompts", "_system_prompt_functions",
                     "_system_messages"):  # Pydantic AI and AutoGen keep them here
            value = _get(holder, attr)
            if value:
                add(f"instructions:{attr.strip('_')}", _instruction_text(value), attr)
        model = (_get(holder, "model") or _get(holder, "model_name") or _get(holder, "_model")
                 or _get(holder, "_model_client"))
        if model is not None:
            config = _get(model, "_raw_config")
            name = model if isinstance(model, str) else (
                _get(model, "model") or _get(model, "model_name")
                or (config.get("model") if isinstance(config, dict) else None)
                or _qualname(type(model)))
            add("model", str(name), str(name))
        for attr in (*_SETTINGS, "_output_type", "_max_tool_iterations", "_reflect_on_tool_use"):
            value = _get(holder, attr)
            if (value is not None and not callable(value)) or isinstance(value, type):
                add(f"settings:{attr.strip('_')}", value, attr)
        for tool in _tools_of(holder):
            name = str(_get(tool, "name") or _get(tool, "_name") or _get(tool, "__name__")
                       or _qualname(type(tool)))
            schema = next((s for s in (_get(tool, "params_json_schema"), _get(tool, "args_schema"),
                                       _get(tool, "parameters"), _get(tool, "input_schema"),
                                       _get(tool, "inputSchema"), _get(tool, "json_schema"),
                                       _get(_get(tool, "function_schema"), "json_schema"),
                                       _get(tool, "_args_type"))
                           if s is not None and not inspect.ismethod(s)), None)
            described = {"name": name,
                         "description": (_get(tool, "description") or _get(tool, "_description")
                                         or inspect.getdoc(tool)),
                         "schema": _plain(schema)}
            fn = _tool_function(tool)
            if fn is not None and not _is_library(fn):
                described["code"] = _source(fn)
            add(f"tool:{name}", described, name)
        servers = _get(holder, "mcp_servers")
        if isinstance(servers, dict):
            for name, cfg in servers.items():
                add(f"mcp:{name}", cfg, str(name))
        elif isinstance(servers, (list, tuple)):
            for server in servers:
                name = str(_get(server, "name") or _qualname(type(server)))
                add(f"mcp:{name}", _get(server, "params") or server, name)
        for attr in (*_SUBAGENTS, "_handoffs"):
            subs = _get(holder, attr)
            names = list(subs) if isinstance(subs, dict) else None
            if isinstance(subs, dict):
                subs = list(subs.values())
            if isinstance(subs, (list, tuple)) and subs:
                described = [{"name": str((names[i] if names else None) or _get(s, "name")
                                          or _get(s, "role") or _qualname(type(s))),
                              "role": _get(s, "role"), "goal": _get(s, "goal"),
                              "description": _get(s, "description"),
                              "instructions": _instruction_text(
                                  _get(s, "instructions") or _get(s, "prompt")
                                  or _get(s, "backstory") or _get(s, "system_message")),
                              "tools": _plain(_get(s, "tools")),
                              "model": _plain(_get(s, "model"))}
                             for i, s in enumerate(subs)]
                add(f"subagents:{attr.strip('_')}", described, f"{len(subs)} {attr.strip('_')}")

    nodes = _get(target, "nodes")
    if isinstance(nodes, dict):  # LangGraph: the code of each node you wrote
        for name, node in nodes.items():
            bound = _get(node, "bound")
            fn = _get(bound, "func") or _get(bound, "afunc")
            if callable(fn) and not _is_library(fn):
                add(f"node:{name}", _source(fn), str(name))
    get_graph = _get(target, "get_graph")
    if callable(get_graph):
        with contextlib.suppress(Exception):  # graph structure is optional
            graph = get_graph()
            nodes_ = sorted(str(n) for n in getattr(graph, "nodes", {}))
            edges = sorted((str(e.source), str(e.target)) for e in getattr(graph, "edges", []))
            add("graph", {"nodes": nodes_, "edges": edges}, f"{len(nodes_)} nodes")

    if not out:  # a library class is named, so its upgrades are recorded under env
        cls = type(target)
        add("class", _source(cls), _qualname(cls))
    return sorted(out.values(), key=lambda c: c.component_id)


@functools.cache
def _env(framework: str | None) -> tuple[HarnessComponent, ...]:
    return tuple(_env_components(framework))


def env_components(framework: str | None = None) -> list[HarnessComponent]:
    """Brevet, Python and framework library versions (read once per process)."""
    return list(_env(framework))


def _env_components(framework: str | None) -> list[HarnessComponent]:
    from brevet import __version__

    def comp(name: str, version: str) -> HarnessComponent:
        return HarnessComponent(component_id=f"env:{name}", kind="env",
                                digest=object_sha256(version), detail=version)

    python = f"{sys.version_info.major}.{sys.version_info.minor}"
    out = [comp("brevet", __version__), comp("python", python)]
    for dist in FRAMEWORK_DISTRIBUTIONS.get(framework or "", ()):
        try:
            out.append(comp(dist, metadata.version(dist)))
        except metadata.PackageNotFoundError:
            continue
    return out


# ------------------------------------------------------------- inventory

def inventory(manifest: AgentManifest, manifest_path: str | Path | None, *,
              adapter: Any = None, workdir: str | Path | None = None,
              sources: list[str] | None = None) -> tuple[list[HarnessComponent], list[str]]:
    """The harness components to lock or to compare, and the sources taken.

    Files are inventoried when the manifest declares ``harness_files``; the
    live agent and library versions when an adapter is given, and otherwise
    carried over from the last release. ``sources`` restricts the inventory,
    so a comparison takes only what a release locked."""
    want = set(sources) if sources is not None else {"files", "agent", "env"}
    mpath = Path(manifest_path) if manifest_path else None
    base = mpath.parent if mpath else Path(".")
    comps: list[HarnessComponent] = []
    used: list[str] = []
    if "files" in want and (manifest.bindings or {}).get("harness_files"):
        comps += file_components(manifest, base,
                                 exclude=[mpath, base / "capabilities.lock"] if mpath else None,
                                 exclude_dirs=[Path(workdir)] if workdir else None)
        used.append("files")
    if adapter is not None and "agent" in want:
        listing = getattr(adapter, "inventory", None)
        comps += listing() if callable(listing) else describe_agent(
            getattr(adapter, "target", adapter), framework=getattr(adapter, "name", None))
        used.append("agent")
    if adapter is not None and "env" in want:
        comps += env_components(getattr(adapter, "name", None))
        used.append("env")
    if adapter is None and sources is None:
        # Releasing without the running agent (the CLI, the MCP server): keep
        # the agent and library components the last release locked, so a
        # wrapped agent is still checked against them.
        previous = load_lock(base / "capabilities.lock") or (
            load_lock(Path(workdir) / "capabilities.lock") if workdir else None)
        for source in ("agent", "env"):
            if previous is not None and source in previous.harness_sources:
                comps += [c for c in previous.harness if c.kind == source]
                used.append(source)
    unique = {c.component_id: c for c in comps}
    return sorted(unique.values(), key=lambda c: c.component_id), used


def compare(locked: list[HarnessComponent],
            live: list[HarnessComponent]) -> list[dict[str, str]]:
    """What changed between the released harness and the live one."""
    before = {c.component_id: c for c in locked}
    now = {c.component_id: c for c in live}
    changes = []
    for cid in sorted(set(before) | set(now)):
        was, live = before.get(cid), now.get(cid)
        if live is None:
            change = "removed"
        elif was is None:
            change = "added"
        elif was.digest != live.digest:
            change = "changed"
        else:
            continue
        changes.append({"component_id": cid, "kind": (live or was).kind, "change": change,
                        "released": was.digest if was else None,
                        "now": live.digest if live else None})
    return changes


def policy(manifest: AgentManifest) -> dict[str, str]:
    """The drift policy. A value it does not recognise means block, so a typo
    can never switch the check off."""
    configured = (manifest.runtime_safety or {}).get("harness") or {}
    if not isinstance(configured, dict):
        configured = {}
    out = dict(DEFAULT_POLICY)
    for key, allowed in _ALLOWED_POLICY.items():
        if key in configured:
            out[key] = configured[key] if configured[key] in allowed else "block"
    return out


def blocking(changes: list[dict[str, str]], manifest: AgentManifest) -> list[dict[str, str]]:
    """The changes that stop a run outside the shadow channel."""
    rules = policy(manifest)
    return [c for c in changes
            if (c["kind"] in ("file", "agent") and rules["on_drift"] == "block")
            or (c["kind"] == "env" and rules["env"] == "block")]


def drift_digest(changes: list[dict[str, str]]) -> str:
    return object_sha256(changes)


def load_lock(path: str | Path) -> CapabilitiesLock | None:
    p = Path(path)
    if not p.exists():
        return None
    import json
    return CapabilitiesLock(**json.loads(p.read_text(encoding="utf-8")))
