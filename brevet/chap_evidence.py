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
  synced folder. Checked structurally only: every line parses, sequence
  numbers are contiguous, the entry at seq 0 points at the genesis hash and
  every entry carries a prev-hash. Checking the hashes themselves needs a
  coordinator.
- **SQLite store** (path ending ``.db``): the coordinator's own
  ``SqliteStore``. A real ``chap_coordinator.Coordinator`` is started on
  the store and queried over ``audit.read`` (nothing here reimplements
  CHAP), and ``--strict`` runs ``audit.verify_chain`` first.
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
per (source, workspace). Re-running is safe and cheap. It is also
path-idempotent: a correction already captured in-session through
``brevet_record`` absorbs one matching CHAP verdict (same family, draft and
final), so running both capture paths does not count one judgment twice.
Identical corrections on different CHAP tasks are separate judgments and
are all kept. Ingestion creates evidence only; it grants no authority.
"""

from __future__ import annotations

import hashlib
import json
import urllib.error
import urllib.request
from collections import Counter
from pathlib import Path
from typing import Any

from brevet.ledger import Ledger
from brevet.models import OverrideRecord, new_id
from brevet.workdir import ensure_workdir

GENESIS = "sha256:" + "0" * 64
_DECIDE_METHODS = {"decide.override", "decide.approve", "decide.reject"}
INGEST_TAG = "chap-ingest"


# ------------------------------------------------------------ sources

def _iter_jsonl_file(path: Path) -> tuple[list[dict[str, Any]], int]:
    """Entries sorted by seq, and the number of lines that did not parse
    (a synced file can end in a half-written line)."""
    entries, damaged = [], 0
    with path.open(encoding="utf-8", errors="replace") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                entry = json.loads(line)
            except ValueError:
                damaged += 1
                continue
            if isinstance(entry, dict):
                entries.append(entry)
            else:
                damaged += 1
    return sorted(entries, key=lambda e: e.get("seq", 0)), damaged


def _jsonl_sources(path: Path) -> tuple[dict[str, list[dict[str, Any]]], set[str]]:
    """Map workspace id -> audit entries for a file or directory of files,
    plus the workspaces whose files had lines that did not parse.

    A missing path yields nothing rather than raising: scheduled ingestion
    runs before the sink exists, and "no evidence yet" is not an error."""
    if path.is_dir():
        files = sorted(path.glob("audit-*.jsonl"))
    else:
        files = [path] if path.is_file() else []
    out: dict[str, list[dict[str, Any]]] = {}
    damaged: set[str] = set()
    for f in files:
        entries, bad_lines = _iter_jsonl_file(f)
        ws = (entries[0].get("envelope", {}).get("params", {}).get("workspace")
              if entries else None) or f.stem.removeprefix("audit-")
        if bad_lines:
            damaged.add(ws)
        if entries:
            out.setdefault(ws, []).extend(entries)
    for entries_for_ws in out.values():
        entries_for_ws.sort(key=lambda e: e.get("seq", 0))
    return out, damaged


class _CoordinatorSource:
    """audit.read / audit.verify_chain against a real coordinator,
    embedded on a SQLite store or served over HTTP."""

    def __init__(self, source: str):
        self._post_url: str | None = None
        self._coord = None
        if source.startswith(("http://", "https://")):
            self._post_url = source.rstrip("/")
        else:
            if not Path(source).is_file():
                # Opening a missing path would create an empty store.
                raise FileNotFoundError(f"no CHAP coordinator store at {source}")
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
    without a coordinator we check shape, not cryptography: the entry at
    seq 0 points at the genesis hash, sequence numbers are contiguous, and
    every entry carries a prev-hash."""
    if not entries:
        return True
    seqs = [e.get("seq") for e in entries]
    if not all(isinstance(s, int) for s in seqs):
        return False
    if seqs != list(range(seqs[0], seqs[0] + len(seqs))):
        return False
    if seqs[0] == 0 and entries[0].get("prev_hash") != GENESIS:
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


def _norm(value: Any) -> str:
    """Comparable text for a draft or final.

    CHAP carries artefacts as canonical JSON while in-session capture
    stores plain text, so the same content differs by quoting alone.
    """
    if isinstance(value, str):
        try:
            decoded = json.loads(value)
        except (ValueError, TypeError):
            return value.strip()
        return _norm(decoded) if not isinstance(decoded, str) else decoded.strip()
    return _canon(value).strip()


def _fingerprint(family: str, draft: Any, final: Any) -> str:
    """Identity of a captured correction, independent of capture path."""
    return _canon([family or "", _norm(draft), _norm(final)])


def _fingerprint_key(family: str, draft: Any, final: Any) -> str:
    """Short, stable key for a captured correction."""
    return hashlib.sha256(
        _fingerprint(family, draft, final).encode("utf-8")).hexdigest()[:32]


_MATCHED_KEY = "__matched__"


def _in_session_fingerprints(ledger: Ledger) -> Counter[str]:
    """Corrections recorded in-session (not imported from CHAP), counted.

    A deployment may run both capture paths (``brevet_record`` in-session
    and CHAP ingestion). Each in-session correction absorbs one CHAP verdict
    with the same family, draft and final, so one judgment counts once,
    while identical corrections on different CHAP tasks all still count:
    dropping them would hide real recurrence."""
    seen: Counter[str] = Counter()
    for env in ledger.read("brevet.override"):
        body = env.get("body", {})
        from_chap = (str(body.get("trace_ref") or "").startswith("chap:")
                     or INGEST_TAG in (body.get("tags") or []))
        if not from_chap:
            seen[_fingerprint_key(body.get("task_family", ""),
                                  body.get("draft"), body.get("final"))] += 1
    return seen


def _ingest_workspace(
    ledger: Ledger,
    workspace: str,
    entries: list[dict[str, Any]],
    after_seq: int,
    family_map: dict[str, str] | None,
    available: Counter[str] | None = None,
    matched: Counter[str] | None = None,
    imported: set[tuple[str, int]] | None = None,
) -> dict[str, int]:
    tasks: dict[str, _TaskContext] = {}
    pending: list[_TaskContext] = []
    available = available if available is not None else Counter()
    matched = matched if matched is not None else Counter()
    counts = {"overrides": 0, "approvals": 0, "rejections": 0,
              "duplicates_skipped": 0}

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
            if imported is not None and (workspace, entry.get("seq")) in imported:
                # imported by an earlier run whose cursor was lost
                counts["duplicates_skipped"] += 1
                continue
            ctx = tasks.get(task_id) or _TaskContext()
            family = (family_map or {}).get(ctx.kind, ctx.kind)
            who = params.get("from", "human:unknown")
            draft = _canon(ctx.artefact) if ctx.artefact is not None else ""
            trace_ref = f"chap:{workspace}#{entry.get('seq')}"
            ov: OverrideRecord | None = None
            if method == "decide.override":
                patched = _apply_patch(ctx.artefact, params.get("diff") or []) \
                    if ctx.artefact is not None else None
                ov = OverrideRecord(
                    task_id="", trace_ref=trace_ref, participant=who,
                    intent_preserved=bool(params.get("intent_preserved", True)),
                    diff=params.get("diff") or [],
                    draft=draft, final=_canon(patched) if patched is not None else None,
                    rationale=params.get("rationale", ""),
                    tags=list(params.get("tags") or []) + [INGEST_TAG],
                    task_family=family)
            elif method == "decide.reject":  # the human reached a different decision
                ov = OverrideRecord(
                    task_id="", trace_ref=trace_ref, participant=who,
                    intent_preserved=False, diff=[], draft=draft, final="",
                    rationale=params.get("comment", ""),
                    tags=list(params.get("tags") or []) + [INGEST_TAG, "rejected"],
                    task_family=family)
            if ov is not None:
                key = _fingerprint_key(family, ov.draft, ov.final)
                if available[key] > 0:
                    # already captured in-session via brevet_record: one
                    # judgment, one override, whatever the capture path
                    available[key] -= 1
                    matched[key] += 1
                    counts["duplicates_skipped"] += 1
                    continue
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
            if ov is None:  # decide.approve
                ledger.append("brevet.artefact",
                              {"task_id": bt_id, "accepted_verbatim": True,
                               "comment": params.get("comment", "")},
                              refs=[bt_id])
                counts["approvals"] += 1
                continue
            ov.task_id = bt_id
            ledger.append("brevet.override", ov.model_dump(), refs=[bt_id])
            counts["overrides" if method == "decide.override" else "rejections"] += 1
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
    wd = ensure_workdir(workdir)
    ledger = Ledger(wd / "ledger.jsonl")
    cursor_path = wd / "chap_cursor.json"
    cursors: dict[str, Any] = (
        json.loads(cursor_path.read_text(encoding="utf-8")) if cursor_path.exists() else {})
    matched: Counter[str] = Counter(cursors.pop(_MATCHED_KEY, None) or {})
    available = _in_session_fingerprints(ledger) - matched
    imported = {(e["body"].get("chap_workspace"), e["body"].get("chap_seq"))
                for e in ledger.read("brevet.task") if e["body"].get("source") == "chap"}

    if source.startswith(("http://", "https://")) or source.endswith(".db"):
        if not workspace:
            raise ValueError("workspace is required for store/URL sources")
        coord = _CoordinatorSource(source)
        chain = "verified" if coord.verify(workspace) else "failed"
        if chain == "failed" and strict:
            raise RuntimeError(f"chain verification failed for {workspace}")
        streams = {workspace: coord.entries(workspace)}
    else:
        streams, damaged = _jsonl_sources(Path(source))
        if workspace:
            streams = {workspace: streams.get(workspace, [])}
            damaged &= {workspace}
        bad = sorted(damaged | {ws for ws, es in streams.items() if not _structural_ok(es)})
        if bad and strict:
            raise RuntimeError(f"structural chain check failed: {bad}")
        chain = "structural" if not bad else "failed"

    summary: dict[str, Any] = {"source": source, "chain": chain,
                               "workspaces": {}, "overrides": 0,
                               "approvals": 0, "rejections": 0,
                               "duplicates_skipped": 0}
    for ws, entries in streams.items():
        key = f"{source}::{ws}"
        counts = _ingest_workspace(ledger, ws, entries,
                                   cursors.get(key, -1), family_map,
                                   available=available, matched=matched,
                                   imported=imported)
        if entries:
            cursors[key] = max(e.get("seq", -1) for e in entries)
        summary["workspaces"][ws] = counts
        for k in ("overrides", "approvals", "rejections",
                  "duplicates_skipped"):
            summary[k] += counts.get(k, 0)
    cursors[_MATCHED_KEY] = dict(matched)
    cursor_path.write_text(json.dumps(cursors, indent=2), encoding="utf-8")
    return summary
