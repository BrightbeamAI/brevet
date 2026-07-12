"""Live CHAP dispatch.

Speaks the CHAP Core wire protocol (JSON-RPC 2.0 over HTTP) to a running
coordinator; the reference implementation is ``@chap/coordinator`` (npm),
from https://github.com/BrightbeamAI/chap. No Python dependency is needed,
so this module is stdlib only by design, not by reinvention: the mirrored
calls are CHAP's own ``task.create`` method, and brevet records land as
artefacts of kind ``brevet.*`` under the ``brevet/1.0`` profile.

When the manifest declares ``ledger: chap:<workspace_id>`` and a coordinator
URL is available (env ``BREVET_CHAP_URL`` or ``chap:<workspace>@<url>``),
every envelope appended to the local chain is mirrored to the coordinator.

The local hash chain remains the offline-verifiable source of truth. Dispatch
fails soft: unreachable coordinators queue envelopes to ``chap_outbox.jsonl``
and ``flush()`` retries, so field deployments never lose evidence to a
network blip. stdlib only; no new dependency.
"""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, Optional

from brevet.models import new_id


class CHAPDispatcher:
    def __init__(self, url: str, workspace_id: str, outbox: Path):
        self.url = url.rstrip("/")
        self.workspace_id = workspace_id
        self.outbox = outbox
        self.outbox.parent.mkdir(parents=True, exist_ok=True)

    def _rpc(self, method: str, params: dict[str, Any]) -> bool:
        body = json.dumps({"jsonrpc": "2.0", "id": new_id("rpc"),
                           "method": method, "params": params}).encode("utf-8")
        req = urllib.request.Request(self.url, data=body,
                                     headers={"Content-Type": "application/json"},
                                     method="POST")
        try:
            with urllib.request.urlopen(req, timeout=10.0) as resp:
                json.loads(resp.read().decode("utf-8"))
            return True
        except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError,
                ConnectionError, OSError, json.JSONDecodeError):
            return False

    def dispatch(self, envelope: dict[str, Any]) -> bool:
        params = {
            "workspace_id": self.workspace_id,
            "artefact": {
                "kind": envelope.get("kind", "brevet.artefact"),
                "content": envelope.get("body", {}),
                "refs": envelope.get("refs", []),
                "content_hash": envelope.get("chain_hash"),
            },
        }
        ok = self._rpc("task.create", params)
        if not ok:
            with self.outbox.open("a") as f:
                f.write(json.dumps(envelope, ensure_ascii=False) + "\n")
        return ok

    def flush(self) -> tuple[int, int]:
        """Retry queued envelopes. Returns (sent, remaining)."""
        if not self.outbox.exists():
            return 0, 0
        pending = [json.loads(line) for line in self.outbox.read_text().splitlines()
                   if line.strip()]
        remaining: list[dict[str, Any]] = []
        sent = 0
        for env in pending:
            params = {"workspace_id": self.workspace_id,
                      "artefact": {"kind": env.get("kind"), "content": env.get("body", {}),
                                   "refs": env.get("refs", []),
                                   "content_hash": env.get("chain_hash")}}
            if self._rpc("task.create", params):
                sent += 1
            else:
                remaining.append(env)
        self.outbox.write_text(
            "".join(json.dumps(e, ensure_ascii=False) + "\n" for e in remaining))
        return sent, len(remaining)


def dispatcher_from_ref(ledger_ref: str, workdir: Path) -> Optional[CHAPDispatcher]:
    """``file:...`` -> None. ``chap:<workspace>`` -> dispatcher using
    $BREVET_CHAP_URL. ``chap:<workspace>@<url>`` -> explicit URL."""
    if not ledger_ref.startswith("chap:"):
        return None
    spec = ledger_ref[5:]
    workspace, _, url = spec.partition("@")
    url = url or os.environ.get("BREVET_CHAP_URL", "")
    if not url:
        return None  # declared but unconfigured: local chain only
    return CHAPDispatcher(url, workspace, workdir / "chap_outbox.jsonl")
