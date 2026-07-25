"""Live CHAP mirroring through the official coordinator.

Brevet keeps its own append-only hash chain as the source of truth and can
mirror every envelope into a CHAP workspace. Both modes below run the real
protocol; nothing here reimplements CHAP:

- **Embedded** (``ledger: chap:<workspace>``): when the official Python
  reference implementation is installed (``chap-coordinator``, from
  ``packages/coordinator-py`` of https://github.com/BrightbeamAI/chap), a
  real ``chap_coordinator.Coordinator`` runs in-process with a SQLite
  store in the workdir. Each brevet envelope lands as a CHAP
  ``task.create`` + ``task.complete`` pair from ``agent:brevet_runtime``.
- **Remote** (``ledger: chap:<workspace>@<url>``, or ``BREVET_CHAP_URL``):
  the same CHAP Core JSON-RPC 2.0 calls are POSTed to a served
  coordinator. Unreachable coordinators queue envelopes to
  ``chap_outbox.jsonl``; ``flush()`` retries, so field deployments never
  lose evidence to a network blip.

Mirroring fails soft by design: a mirror problem must never break the
local chain.
"""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

from brevet.models import new_id

PARTICIPANT = "agent:brevet_runtime"
PROFILES = ["core/1.0", "review/1.0"]


def _rpc(method: str, params: dict[str, Any]) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": new_id("rpc"), "method": method, "params": params}


def _provision_calls(workspace: str) -> list[dict[str, Any]]:
    return [
        _rpc("workspace.create", {"workspace": workspace}),
        _rpc("participant.join", {"workspace": workspace, "from": PARTICIPANT,
                                  "type": "agent", "role": "drafter"}),
    ]


def _create_call(workspace: str, envelope: dict[str, Any]) -> dict[str, Any]:
    return _rpc("task.create", {
        "workspace": workspace,
        "from": PARTICIPANT,
        "kind": envelope.get("kind", "brevet.artefact"),
        "input": {"body": envelope.get("body", {}),
                  "refs": envelope.get("refs", []),
                  "chain_hash": envelope.get("chain_hash")},
        "assignee": PARTICIPANT,
    })


def _complete_call(workspace: str, task_id: str, envelope: dict[str, Any]) -> dict[str, Any]:
    return _rpc("task.complete", {
        "workspace": workspace,
        "task_id": task_id,
        "from": PARTICIPANT,
        "output": {"chain_hash": envelope.get("chain_hash")},
    })


def _benign(error: dict[str, Any] | None) -> bool:
    return error is not None and "already" in str(error.get("message", "")).lower()


class EmbeddedCHAPDispatcher:
    """Mirror through an in-process ``chap_coordinator.Coordinator``."""

    def __init__(self, workspace: str, db_path: Path):
        from chap_coordinator import Coordinator, CoordinatorOptions
        store = None
        try:
            from chap_coordinator.storage.sqlite import SqliteStore
            db_path.parent.mkdir(parents=True, exist_ok=True)
            store = SqliteStore(str(db_path))
        except Exception:  # noqa: BLE001 - any store failure must fail soft
            store = None  # in-memory coordinator still mirrors for this run
        self.coordinator = Coordinator(
            CoordinatorOptions(default_profiles=PROFILES, store=store))
        self.workspace = workspace
        self._ready = False

    def _ensure(self) -> None:
        if self._ready:
            return
        for call in _provision_calls(self.workspace):
            response = self.coordinator.dispatch(call)
            error = response.get("error")
            if error and not _benign(error):
                raise RuntimeError(f"chap provisioning failed: {error.get('message')}")
        self._ready = True

    def dispatch(self, envelope: dict[str, Any]) -> bool:
        try:
            self._ensure()
            created = self.coordinator.dispatch(_create_call(self.workspace, envelope))
            if "error" in created:
                return False
            task_id = created["result"]["task_id"]
            done = self.coordinator.dispatch(
                _complete_call(self.workspace, task_id, envelope))
            return "error" not in done
        except Exception:  # noqa: BLE001 - mirroring must never break the chain
            return False  # never let a mirror problem break the local chain


class CHAPDispatcher:
    """Mirror to a served coordinator over HTTP, with an offline outbox."""

    def __init__(self, url: str, workspace_id: str, outbox: Path):
        self.url = url.rstrip("/")
        self.workspace_id = workspace_id
        self.outbox = outbox
        self.outbox.parent.mkdir(parents=True, exist_ok=True)
        self._ready = False

    def _post(self, call: dict[str, Any]) -> dict[str, Any] | None:
        body = json.dumps(call).encode("utf-8")
        req = urllib.request.Request(self.url, data=body,
                                     headers={"Content-Type": "application/json"},
                                     method="POST")
        try:
            with urllib.request.urlopen(req, timeout=10.0) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError,
                ConnectionError, OSError, json.JSONDecodeError):
            return None

    def _ensure(self) -> bool:
        if self._ready:
            return True
        for call in _provision_calls(self.workspace_id):
            response = self._post(call)
            if response is None:
                return False
            if response.get("error") and not _benign(response["error"]):
                return False
        self._ready = True
        return True

    def _send(self, envelope: dict[str, Any]) -> bool:
        if not self._ensure():
            return False
        created = self._post(_create_call(self.workspace_id, envelope))
        if created is None or "error" in created:
            return False
        task_id = created["result"]["task_id"]
        done = self._post(_complete_call(self.workspace_id, task_id, envelope))
        return done is not None and "error" not in done

    def dispatch(self, envelope: dict[str, Any]) -> bool:
        ok = self._send(envelope)
        if not ok:
            with self.outbox.open("a") as f:
                f.write(json.dumps(envelope, ensure_ascii=False) + "\n")
        return ok

    def flush(self) -> tuple[int, int]:
        """Retry queued envelopes. Returns (sent, remaining)."""
        if not self.outbox.exists():
            return 0, 0
        pending = [json.loads(line)
                   for line in self.outbox.read_text().splitlines() if line.strip()]
        remaining: list[dict[str, Any]] = []
        sent = 0
        for envelope in pending:
            if self._send(envelope):
                sent += 1
            else:
                remaining.append(envelope)
        self.outbox.write_text(
            "".join(json.dumps(e, ensure_ascii=False) + "\n" for e in remaining))
        return sent, len(remaining)


def dispatcher_from_ref(ledger_ref: str, workdir: Path) -> Any | None:
    """``file:...`` -> None. ``chap:<workspace>@<url>`` (or $BREVET_CHAP_URL)
    -> remote dispatcher. ``chap:<workspace>`` with the official
    ``chap-coordinator`` package installed -> embedded coordinator with a
    SQLite store in the workdir. Declared but unconfigured -> local chain
    only."""
    if not ledger_ref.startswith("chap:"):
        return None
    spec = ledger_ref[5:]
    workspace, _, url = spec.partition("@")
    url = url or os.environ.get("BREVET_CHAP_URL", "")
    if url:
        return CHAPDispatcher(url, workspace, workdir / "chap_outbox.jsonl")
    try:
        import chap_coordinator  # noqa: F401  (the official reference implementation)
    except ImportError:
        return None
    return EmbeddedCHAPDispatcher(workspace, workdir / "chap.db")
