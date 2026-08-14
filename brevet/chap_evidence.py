"""Native CHAP evidence source (inbound).

``chap_bridge`` mirrors brevet envelopes *out* to a CHAP workspace. This
module is the other half: it ingests a CHAP audit chain and turns human
verdicts into brevet evidence, so a deployment that already records
reviews through CHAP (e.g. Claude Cowork with the chap-capture skill)
feeds the dream/dawn pipeline with no separate ``brevet_record`` calls.
CHAP is the capture surface; brevet stays the learning gate.

Three source shapes, auto-detected from the ``source`` string:

- **JSONL sink** (path to ``audit-<workspace>.jsonl`` or a directory of
  them): the files written by an ``on_audit`` listener, typically in a
  synced folder. Verified structurally (seq contiguity, prev-hash
  linkage); cryptographic verification needs a coordinator.
- **SQLite store** (path ending ``.db``): the coordinator's own
  ``SqliteStore``. A real ``chap_coordinator.Coordinator`` is started on
  the store and queried over ``audit.read`` — nothing here reimplements
  CHAP — and ``--strict`` runs ``audit.verify_chain`` first.
- **Served coordinator** (``http(s)://...``): the same JSON-RPC calls
  POSTed remotely.

Mapping, per decided task (one brevet.task per CHAP task, exactly the
shape ``brevet_record`` writes, so mining and recurrence are unchanged):

    decide.override  -> brevet.override, CHAP's own diff/rationale/tags/
                        intent_preserved carried through verbatim
    decide.reject    -> brevet.override with intent_preserved=False
                        (a substituting judgment: the draft was discarded)
    decide.approve   -> brevet.artefact marked accepted_verbatim

Ingestion is idempotent: a cursor file remembers the last seq consumed
per (source, workspace). Re-running is safe and cheap. Ingestion creates
evidence only; it grants no authority.
"""

from __future__ import annotations

import json
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

from brevet.ledger import Ledger
from brevet.models import OverrideRecord, new_id

GENESIS = "sha256:" + "0" * 64
_DECIDE_METHODS = {"decide.override", "decide.approve", "decide.reject"}
INGEST_TAG = "chap-ingest"


# ------------------------------------------------------------ sources

def _iter_jsonl_file(path: Path) -> list[dict[str, Any]]:
    entries = []
    with path.open() as f:
        for line in f:
            line = line.strip()
            if line:
                entries.append(json.loads(line))
    return sorted(entries, key=lambda e: e.get("seq", 0))


def _jsonl_sources(path: Path) -> dict[str, list[dict[str, Any]]]:
    """Map workspace id -> audit entries for a file or directory of files."""
    files = sorted(path.glob("audit-*.jsonl")) if path.is_dir() else [path]
    out: dict[str, list[dict[str, Any]]] = {}
    for f in files:
        entries = _iter_jsonl_file(f)
        if not entries:
            continue
        ws = entries[0].get("envelope", {}).get("params", {}).get("workspace")
        ws = ws or f.stem.removeprefix("audit-")
        out.setdefault(ws, []).extend(entries)
    for entries_for_ws in out.values():
        entries_for_ws.sort(key=lambda e: e.get("seq", 0))
    return out


class _CoordinatorSource:
    """audit.read / audit.verify_chain against a real coordinator,
    embedded on a SQLite store or served over HTTP."""

    def __init__(self, source: str):
        self._post_url: str | None = None
        self._coord = None
        if source.startswith(("http://", "https://")):
            self._post_url = source.rstrip("/")
        else:
            from chap_coordinator import Coordinator, CoordinatorOptions
            from chap_coordinator.storage.sqlite import SqliteStore
            self._coord = Coordinator(
                CoordinatorOptions(store=SqliteStore(source)))

    def _dispatch(self, method: str, params: dict[str, Any]) -> dict[str, Any]:
        call = {"jsonrpc": "2.0", "id": new_id("rpc"),
                "method": method, "params": params}
        if self._coord is not None:
            return self._coord.dispatch(call)
        body = json.dumps(call).encode("utf-8")
        req = urllib.request.Request(
            self._post_url, data=body,
            headers={"Content-Type": "application/json"}, method="POST")
        try:
            with urllib.request.urlopen(req, timeout=10.0) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except (urllib.error.URLError, TimeoutError, OSError,
                json.JSONDecodeError) as exc:  # unreachable is a hard stop
            return {"error": {"message": str(exc)}}

    def entries(self, workspace: str) -> list[dict[str, Any]]:
        resp = self._dispatch("audit.read", {"workspace": workspace})
        if "error" in resp:
            raise RuntimeError(f"audit.read failed: {resp['error'].get('message')}")
        return sorted(resp["result"]["entries"], key=lambda e: e.get("seq", 0))

    def verify(self, workspace: str) -> bool:
        resp = self._dispatch("audit.verify_chain", {"workspace": workspace})
        return "error" not in resp and bool(resp["result"].get("ok"))


def _structural_ok(entries: list[dict[str, Any]]) -> bool:
    """JSONL files carry each entry's prev_hash but not its own hash, so
    without a coordinator we check linkage shape, not cryptography:
    genesis prev-hash first, then contiguous seq with a hash present."""
    if not entries:
        return True
    if entries[0].get("seq") == 0 and entries[0].get("prev_hash") != GENESIS:
        return False
    seqs = [e.get("seq") for e in entries]
    if seqs != sorted(set(seqs)):
        return False
    return all(str(e.get("prev_hash", "")).startswith("sha256:") for e in entries)


# ------------------------------------------------------------ mapping

def _apply_patch(artefact: Any, diff: list[dict[str, Any]]) -> Any | None:
    """Minimal RFC 6902 (add/replace/remove on object paths). CHAP overrides
    in the wild patch small artefact objects; anything fancier keeps the
    diff verbatim and returns None for the patched form."""
    try:
        doc = json.loads(json.dumps(artefact))
        for op in diff:
            kind, path = op.get("op"), op.get("path", "")
            keys = [k.replace("~1", "/").replace("~0", "~")
                    for k in path.lstrip("/").split("/") if k != ""]
            if not keys or kind not in {"add", "replace", "remove"}:
                return None
            parent = doc
            for k in keys[:-1]:
                parent = parent[int(k)] if isinstance(parent, list) else parent[k]
            last = keys[-1]
            if kind == "remove":
                del parent[int(last) if isinstance(parent, list) else last]
            else:
                if isinstance(parent, list):
                    idx = len(parent) if last == "-" else int(last)
                    if kind == "add":
                        parent.insert(idx, op.get("value"))
                    else:
                        parent[idx] = op.get("value")
                else:
                    parent[last] = op.get("value")
        return doc
    except (KeyError, IndexError, ValueError, TypeError):
        return None


def _canon(obj: Any) -> str:
    return json.dumps(obj, sort_keys=True, ensure_ascii=False, indent=2)


class _TaskContext:
    def __init__(self) -> None:
        self.kind: str = "chap_task"
        self.request: str = ""
        self.artefact: Any = None
        self.delegator: str = ""


def _ingest_workspace(
    ledger: Ledger,
    workspace: str,
    entries: list[dict[str, Any]],
    after_seq: int,
    family_map: dict[str, str] | None,
) -> dict[str, int]:
    tasks: dict[str, _TaskContext] = {}
    pending: list[_TaskContext] = []
    counts = {"overrides": 0, "approvals": 0, "rejections": 0}

    for entry in entries:
        env = entry.get("envelope", {})
        method, params = env.get("method"), env.get("params", {})
        task_id = params.get("task_id") or ""
        if method == "task.create":
            # task.create carries no task_id in its params; the id arrives in
            # the response, which the audit log does not store. Bind the
            # context to the first task-scoped call that follows (FIFO).
            ctx = _TaskContext()
            ctx.kind = params.get("kind", "chap_task")
            ctx.request = str((params.get("input") or {}).get("request", ""))
            ctx.delegator = params.get("from", "")
            pending.append(ctx)
        elif method in {"task.complete", "review.request"} and task_id:
            ctx = tasks.get(task_id)
            if ctx is None:
                ctx = pending.pop(0) if pending else _TaskContext()
                tasks[task_id] = ctx
            artefact = params.get("artefact") or params.get("output")
            if artefact is not None:
                ctx.artefact = artefact
        elif method in _DECIDE_METHODS and entry.get("seq", 0) > after_seq:
            ctx = tasks.get(task_id) or _TaskContext()
            family = (family_map or {}).get(ctx.kind, ctx.kind)
            who = params.get("from", "human:unknown")
            draft = _canon(ctx.artefact) if ctx.artefact is not None else ""
            provenance = {
                "task": ctx.request or f"CHAP task {task_id}",
                "task_family": family,
                "agent": "agent:chap-ingest",
                "agent_version": "n/a",
                "channel": "chap",
                "source": "chap",
                "chap_workspace": workspace,
                "chap_task_id": task_id,
                "chap_seq": entry.get("seq"),
                "decided_at": entry.get("arrived"),
            }
            bt_id = ledger.append("brevet.task", provenance)
            ledger.append("brevet.artefact",
                          {"task_id": bt_id, "output": draft,
                           "trace_len": 0, "trace": []}, refs=[bt_id])
            trace_ref = f"chap:{workspace}#{entry.get('seq')}"
            if method == "decide.approve":
                ledger.append("brevet.artefact",
                              {"task_id": bt_id, "accepted_verbatim": True,
                               "comment": params.get("comment", "")},
                              refs=[bt_id])
                counts["approvals"] += 1
                continue
            if method == "decide.override":
                patched = _apply_patch(ctx.artefact, params.get("diff") or []) \
                    if ctx.artefact is not None else None
                ov = OverrideRecord(
                    task_id=bt_id, trace_ref=trace_ref, participant=who,
                    intent_preserved=bool(params.get("intent_preserved", True)),
                    diff=params.get("diff") or [],
                    draft=draft, final=_canon(patched) if patched is not None else None,
                    rationale=params.get("rationale", ""),
                    tags=list(params.get("tags") or []) + [INGEST_TAG],
                    task_family=family)
                counts["overrides"] += 1
            else:  # decide.reject: the human reached a different decision
                ov = OverrideRecord(
                    task_id=bt_id, trace_ref=trace_ref, participant=who,
                    intent_preserved=False, diff=[], draft=draft, final="",
                    rationale=params.get("comment", ""),
                    tags=list(params.get("tags") or []) + [INGEST_TAG, "rejected"],
                    task_family=family)
                counts["rejections"] += 1
            ledger.append("brevet.override", ov.model_dump(), refs=[bt_id])
    return counts


# ------------------------------------------------------------ entry point

def ingest(
    source: str,
    *,
    workdir: Path | str = ".brevet",
    workspace: str | None = None,
    strict: bool = False,
    family_map: dict[str, str] | None = None,
) -> dict[str, Any]:
    """Ingest CHAP verdicts into the brevet ledger. Returns a summary dict.

    ``source``: JSONL file/dir, ``.db`` SQLite store, or coordinator URL.
    ``workspace``: required for store/URL sources; inferred for JSONL.
    ``strict``: fail instead of ingesting when the chain does not verify.
    """
    wd = Path(workdir)
    wd.mkdir(parents=True, exist_ok=True)
    ledger = Ledger(wd / "ledger.jsonl")
    cursor_path = wd / "chap_cursor.json"
    cursors: dict[str, int] = (
        json.loads(cursor_path.read_text()) if cursor_path.exists() else {})

    if source.startswith(("http://", "https://")) or source.endswith(".db"):
        if not workspace:
            raise ValueError("workspace is required for store/URL sources")
        coord = _CoordinatorSource(source)
        chain = "verified" if coord.verify(workspace) else "failed"
        if chain == "failed" and strict:
            raise RuntimeError(f"chain verification failed for {workspace}")
        streams = {workspace: coord.entries(workspace)}
    else:
        streams = _jsonl_sources(Path(source))
        if workspace:
            streams = {workspace: streams.get(workspace, [])}
        bad = [ws for ws, es in streams.items() if not _structural_ok(es)]
        if bad and strict:
            raise RuntimeError(f"structural chain check failed: {bad}")
        chain = "structural" if not bad else "failed"

    summary: dict[str, Any] = {"source": source, "chain": chain,
                               "workspaces": {}, "overrides": 0,
                               "approvals": 0, "rejections": 0}
    for ws, entries in streams.items():
        key = f"{source}::{ws}"
        counts = _ingest_workspace(ledger, ws, entries,
                                   cursors.get(key, -1), family_map)
        if entries:
            cursors[key] = max(e.get("seq", -1) for e in entries)
        summary["workspaces"][ws] = counts
        for k in ("overrides", "approvals", "rejections"):
            summary[k] += counts[k]
    cursor_path.write_text(json.dumps(cursors, indent=2))
    return summary
