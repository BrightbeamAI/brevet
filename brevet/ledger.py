"""The evidence ledger.

CHAP-shaped envelopes in an append-only, hash-linked JSONL file. When the
manifest declares a ``chap:`` ledger, envelopes are additionally mirrored
through the official CHAP coordinator: embedded in-process via the
``chap-coordinator`` Python package, or over JSON-RPC to a served
coordinator (``chap:<workspace>@<url>``). The local chain remains the
offline-verifiable copy. Envelope kinds use the ``brevet.*``
namespace, declared by the ``brevet/1.0`` profile:

    brevet.task            one wrapped agent invocation
    brevet.artefact        an agent output attached to a task
    brevet.override        a human judgment (diff + rationale + tags)
    brevet.candidate       a capability object proposed by the delta engine
    brevet.promotion       a dawn-gate decision (promote/hold/reject/re_elicit)
    brevet.release         a signed harness version transition
    brevet.recall          a capability recall notice
    brevet.eval_run        a regression suite execution
    brevet.model_assist    a logged model-drafting event (drafts only)
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from brevet.canonical import chain_hash
from brevet.models import new_id

GENESIS = "sha256:" + "0" * 64


class Ledger:
    def __init__(self, path: Path | str, dispatcher: Any | None = None):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.dispatcher = dispatcher  # optional live CHAP mirror (fails soft)
        self._prev = self._tail_hash()

    def _tail_hash(self) -> str:
        if not self.path.exists():
            return GENESIS
        prev = GENESIS
        with self.path.open() as f:
            for line in f:
                line = line.strip()
                if line:
                    prev = json.loads(line).get("chain_hash", prev)
        return prev

    def append(self, kind: str, body: dict[str, Any], *, refs: list[str] | None = None) -> str:
        envelope = {
            "envelope_id": new_id("env"),
            "kind": kind,
            "created_at": datetime.now(timezone.utc).isoformat(),
            "refs": refs or [],
            "body": body,
            "prev_hash": self._prev,
        }
        envelope["chain_hash"] = chain_hash(
            {k: v for k, v in envelope.items() if k != "chain_hash"}, self._prev
        )
        with self.path.open("a") as f:
            f.write(json.dumps(envelope, ensure_ascii=False) + "\n")
        self._prev = envelope["chain_hash"]
        if self.dispatcher is not None:
            self.dispatcher.dispatch(envelope)
        return envelope["envelope_id"]

    def read(self, kind: str | None = None) -> Iterator[dict[str, Any]]:
        if not self.path.exists():
            return
        with self.path.open() as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                env = json.loads(line)
                if kind is None or env.get("kind") == kind:
                    yield env

    def verify(self) -> tuple[bool, int]:
        """Independent replay of the chain. Returns (ok, envelopes_checked)."""
        prev, n = GENESIS, 0
        for env in self.read():
            claimed = env.get("chain_hash")
            body = {k: v for k, v in env.items() if k != "chain_hash"}
            if body.get("prev_hash") != prev or chain_hash(body, prev) != claimed:
                return False, n
            prev, n = claimed, n + 1
        return True, n
