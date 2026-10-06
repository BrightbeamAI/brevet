"""The evidence chain.

CHAP-shaped envelopes in an append-only, hash-linked JSONL file. When the
manifest declares a ``chap:`` ledger, envelopes are also mirrored through
the official CHAP coordinator: embedded in-process via the
``chap-coordinator`` Python package, or over JSON-RPC to a served
coordinator (``chap:<workspace>@<url>``). The local chain remains the
offline-verifiable copy. Envelope kinds use the ``brevet.*`` namespace,
declared by the ``brevet/1.0`` profile:

    brevet.task            one wrapped agent invocation
    brevet.artefact        an agent output attached to a task
    brevet.override        a human judgment (diff + rationale + tags)
    brevet.candidate       a capability proposed by the dream cycle
    brevet.promotion       a dawn-gate decision (promote/hold/reject/re_elicit)
    brevet.release         a signed harness version transition
    brevet.recall          a capability recall notice
    brevet.eval_run        an eval run over the override-compiled cases
    brevet.model_assist    a logged model-drafting event (drafts only)
    brevet.approver        an approver registered or revoked, or a threshold
    brevet.drift           the harness differs from its release
    brevet.recall_ack      a running agent confirmed it stopped using a recalled
                           capability
    brevet.tool_call       a tool call the tool broker allowed or refused
    brevet.consent         a participant withdrew consent
    brevet.gate            the conservative gate blocked a release

Each envelope names the runtime that wrote it (``"runtime": "brevet/0.4.0"``).

Appends take a file lock and re-read the chain's last hash, so several
processes (an MCP server, a scheduled ingest, the CLI) can share one chain.
After every governing envelope the chain's head is anchored outside the
workspace, where anchors are configured (see ``brevet.anchor``).
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from brevet.anchor import ANCHORED_KINDS
from brevet.canonical import chain_hash
from brevet.models import new_id
from brevet.workdir import file_lock

GENESIS = "sha256:" + "0" * 64
_TAIL_WINDOW = 1 << 20  # bytes read from the end of the file to find the last hash


def _runtime() -> str:
    """The runtime that writes an envelope, named in it so replay knows which
    rules the envelope was written under."""
    from brevet import __version__
    return f"brevet/{__version__}"


def _chain_hash_of(raw: bytes) -> str | None:
    """The chain_hash of one well-formed envelope line, else None."""
    raw = raw.strip()
    if not raw:
        return None
    try:
        env = json.loads(raw)
    except ValueError:
        return None
    value = env.get("chain_hash") if isinstance(env, dict) else None
    return value if isinstance(value, str) else None


class Ledger:
    def __init__(self, path: Path | str, dispatcher: Any | None = None, *,
                 manifest: Any = None, anchoring: bool = True):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.dispatcher = dispatcher  # optional live CHAP mirror (fails soft)
        self.manifest = manifest      # where anchors may be declared
        self.anchoring = anchoring
        self.on_append: list[Any] = []  # callbacks(before, after): file stats around each append
        self._prev = self._tail_hash()

    def _tail_hash(self) -> str:
        """chain_hash of the last well-formed envelope, or GENESIS."""
        if not self.path.exists():
            return GENESIS
        size = self.path.stat().st_size
        with self.path.open("rb") as f:
            if size > _TAIL_WINDOW:
                f.seek(size - _TAIL_WINDOW)
                lines = f.read().split(b"\n")[1:]  # the first piece may be partial
            else:
                lines = f.read().split(b"\n")
        for raw in reversed(lines):
            found = _chain_hash_of(raw)
            if found:
                return found
        if size > _TAIL_WINDOW:  # very long lines: scan the whole file
            prev = GENESIS
            with self.path.open("rb") as f:
                for raw in f:
                    prev = _chain_hash_of(raw) or prev
            return prev
        return GENESIS

    def append(self, kind: str, body: dict[str, Any], *, refs: list[str] | None = None) -> str:
        with file_lock(self.path):
            before = self.path.stat() if self.path.exists() else None
            prev = self._tail_hash()  # another process may have appended
            envelope = {
                "envelope_id": new_id("env"),
                "kind": kind,
                "created_at": datetime.now(timezone.utc).isoformat(),
                "runtime": _runtime(),
                "refs": refs or [],
                "body": body,
                "prev_hash": prev,
            }
            envelope["chain_hash"] = chain_hash(
                {k: v for k, v in envelope.items() if k != "chain_hash"}, prev
            )
            with self.path.open("a", encoding="utf-8") as f:
                f.write(json.dumps(envelope, ensure_ascii=False) + "\n")
            self._prev = envelope["chain_hash"]
            after = self.path.stat()
            for callback in self.on_append:
                callback(before, after)
        if self.dispatcher is not None:
            self.dispatcher.dispatch(envelope)
        if self.anchoring and kind in ANCHORED_KINDS:
            self.anchor()
        return envelope["envelope_id"]

    def anchor(self) -> dict[str, Any] | None:
        """Write the chain's head to the configured anchors, if any. A
        failure to anchor never undoes or blocks the append itself."""
        import warnings

        from brevet.anchor import anchor, configured
        try:
            if configured(self.path.parent, self.manifest):
                return anchor(self, self.path.parent, manifest=self.manifest)
        except Exception as e:  # noqa: BLE001 - reported, never allowed to undo the append
            warnings.warn(f"brevet: could not anchor the evidence chain: {e}", stacklevel=2)
        return None

    def read(self, kind: str | None = None) -> Iterator[dict[str, Any]]:
        """Envelopes in order. A damaged line is skipped here; verify()
        reports it as a break in the chain."""
        if not self.path.exists():
            return
        with self.path.open(encoding="utf-8", errors="replace") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    env = json.loads(line)
                except ValueError:
                    continue
                if isinstance(env, dict) and (kind is None or env.get("kind") == kind):
                    yield env

    def verify(self) -> tuple[bool, int]:
        """Independent replay of the chain. Returns (ok, envelopes_checked):
        on a break, the number of intact envelopes before it."""
        prev, n = GENESIS, 0
        if not self.path.exists():
            return True, 0
        with self.path.open(encoding="utf-8", errors="replace") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    env = json.loads(line)
                except ValueError:
                    return False, n
                if not isinstance(env, dict):
                    return False, n
                claimed = env.get("chain_hash")
                body = {k: v for k, v in env.items() if k != "chain_hash"}
                if body.get("prev_hash") != prev or chain_hash(body, prev) != claimed:
                    return False, n
                prev, n = claimed, n + 1
        return True, n
