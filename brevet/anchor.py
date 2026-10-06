"""Anchoring: copies of the evidence chain's head held outside the workspace.

Replaying the chain detects any edit that breaks a hash link. A chain that
was cut short at the end, rewritten from some point on, or replaced
altogether replays cleanly, so the workspace alone cannot reveal it. An
anchor closes that gap: after every governing step (a dawn decision, a
release, a recall, a register change, an acknowledgement, a new candidate,
an eval run) and whenever ``brevet anchor`` runs, Brevet writes the chain's
head (how many envelopes, and the hash of the last one) to one or more
anchor targets outside the workspace. Verification then checks that the
chain still holds every anchored head.

Targets are written as references:

    file:<path>                 an append-only JSONL log outside the
                                workspace: another disk, a synced folder,
                                a share the agent cannot write
    chap:<workspace>@<url>      a CHAP coordinator, whose own append-only
                                audit log holds each head

They are read from the manifest (``runtime_safety.evidence.anchors``), from
``BREVET_ANCHORS`` (separated by commas) and from the user's own
configuration (``~/.config/brevet/anchors``, one reference per line, or the
folder ``BREVET_CONFIG_DIR`` names). The last two live outside the
workspace, so an agent that can rewrite the workspace cannot remove them.

Each anchor record names the workspace it belongs to, by an identity kept
in the user's configuration (``~/.config/brevet/workspaces.json``, keyed by
the workspace's path) rather than anything inside the workspace, and the
chain (the hash of its first envelope); it is signed with the workspace key.
A target holding anchors for the workspace from a different chain means the
chain was replaced, whatever the agent is now called. An unreachable target
is reported, never fatal; a head the chain no longer holds is.
"""

from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

import yaml

from brevet.canonical import Signer
from brevet.models import _now, new_id

#: Envelope kinds after which the head is anchored automatically.
ANCHORED_KINDS = frozenset({
    "brevet.promotion", "brevet.release", "brevet.recall", "brevet.approver",
    "brevet.recall_ack", "brevet.consent", "brevet.candidate", "brevet.eval_run"})
PARTICIPANT = "agent:brevet_runtime"
_DOWN: dict[str, float] = {}   # coordinators that just failed, skipped for a while
_DOWN_FOR = 300.0


def config_dir() -> Path:
    """The user's Brevet configuration folder, outside every workspace."""
    env = os.environ.get("BREVET_CONFIG_DIR")
    return Path(env).expanduser() if env else Path.home() / ".config" / "brevet"


def workspace_id(workdir: str | Path, *, create: bool) -> str | None:
    """This workspace's identity, kept outside it, keyed by its path."""
    registry = config_dir() / "workspaces.json"
    try:
        data = json.loads(registry.read_text(encoding="utf-8")) if registry.exists() else {}
    except (OSError, ValueError):
        data = {}
    # the path as named, not through symlinks: pointing the workspace at
    # another folder must not give it a fresh identity
    key = str(Path(os.path.abspath(os.path.expanduser(str(workdir)))))
    if isinstance(data.get(key), dict) and data[key].get("id"):
        return data[key]["id"]
    if not create:
        return None
    data[key] = {"id": new_id("ws"), "registered_at": _now()}
    registry.parent.mkdir(parents=True, exist_ok=True)
    tmp = registry.with_name(f".{registry.name}.{os.getpid()}.tmp")
    tmp.write_text(json.dumps(data, indent=2, sort_keys=True), encoding="utf-8")
    tmp.replace(registry)
    return data[key]["id"]


def _manifest_beside(workdir: Path) -> Any:
    from brevet.models import AgentManifest
    for path in (workdir.parent / "agent.yaml", workdir / "agent.yaml"):
        if path.exists():
            try:
                return AgentManifest(**(yaml.safe_load(path.read_text(encoding="utf-8")) or {}))
            except (OSError, ValueError, yaml.YAMLError):
                return None
    return None


def configured(workdir: str | Path, manifest: Any = None) -> list[str]:
    """Every anchor reference that applies to this workspace."""
    refs: list[str] = []
    m = manifest if manifest is not None else _manifest_beside(Path(workdir))
    if m is not None:
        declared = ((m.runtime_safety or {}).get("evidence") or {}).get("anchors") or []
        refs += [declared] if isinstance(declared, str) else [str(r) for r in declared]
    refs += [r for r in os.environ.get("BREVET_ANCHORS", "").split(",")]
    user = config_dir() / "anchors"
    if user.exists():
        refs += [line.split("#", 1)[0] for line in user.read_text(encoding="utf-8").splitlines()]
    out: list[str] = []
    for ref in (r.strip() for r in refs):
        if ref and ref not in out:
            out.append(ref)
    return out


# ------------------------------------------------------------- targets

class FileTarget:
    """An append-only JSONL anchor log outside the workspace."""

    def __init__(self, ref: str, workdir: Path):
        self.ref = ref
        raw = Path(os.path.expanduser(ref[5:]))
        # a relative path is read from the folder that holds the workspace
        self.path = (raw if raw.is_absolute() else workdir.resolve().parent / raw).resolve()
        inside = workdir.resolve()
        if self.path == inside or inside in self.path.parents:
            raise ValueError(f"anchor {ref} is inside the workspace it anchors; "
                             f"choose a place outside {inside}")

    def write(self, record: dict[str, Any]) -> bool:
        if not self.path.parent.is_dir():
            return False  # never create the folder: an unmounted volume must not become local
        try:
            with self.path.open("a", encoding="utf-8") as f:
                f.write(json.dumps(record, sort_keys=True) + "\n")
            return True
        except OSError:
            return False

    def read(self, workspace: str) -> list[dict[str, Any]] | None:
        if not self.path.parent.is_dir():
            return None
        if not self.path.exists():
            return []
        out = []
        try:
            for line in self.path.read_text(encoding="utf-8").splitlines():
                try:
                    rec = json.loads(line)
                except ValueError:
                    continue
                if isinstance(rec, dict) and rec.get("workspace") == workspace:
                    out.append(rec)
        except OSError:
            return None
        return out


class CHAPTarget:
    """A CHAP coordinator holding each head in its own append-only audit log."""

    def __init__(self, ref: str, workdir: Path):
        self.ref = ref
        spec = ref[5:]
        self.workspace, _, url = spec.partition("@")
        url = url or os.environ.get("BREVET_CHAP_URL", "")
        if not (self.workspace and url):
            raise ValueError(f"anchor {ref} needs a served coordinator, written "
                             f"chap:<workspace>@<url>: an embedded coordinator lives "
                             f"inside the workspace it would anchor")
        self.url = url.rstrip("/")
        self._ready = False

    def _post(self, method: str, params: dict[str, Any]) -> dict[str, Any] | None:
        call = {"jsonrpc": "2.0", "id": new_id("rpc"), "method": method, "params": params}
        req = urllib.request.Request(self.url, data=json.dumps(call).encode("utf-8"),
                                     headers={"Content-Type": "application/json"},
                                     method="POST")
        if _DOWN.get(self.ref, 0.0) > time.monotonic():
            return None
        try:
            with urllib.request.urlopen(req, timeout=3.0) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except (urllib.error.URLError, TimeoutError, ConnectionError, OSError, ValueError):
            _DOWN[self.ref] = time.monotonic() + _DOWN_FOR
            return None

    def _ensure(self) -> bool:
        if self._ready:
            return True
        for method, params in (
                ("workspace.create", {"workspace": self.workspace}),
                ("participant.join", {"workspace": self.workspace, "from": PARTICIPANT,
                                      "type": "agent", "role": "drafter"})):
            out = self._post(method, params)
            if out is None:
                return False
            error = out.get("error")
            if error and "already" not in str(error.get("message", "")).lower():
                return False
        self._ready = True
        return True

    def write(self, record: dict[str, Any]) -> bool:
        if not self._ensure():
            return False
        out = self._post("task.create", {"workspace": self.workspace, "from": PARTICIPANT,
                                         "kind": "brevet.anchor", "input": record,
                                         "assignee": PARTICIPANT})
        return out is not None and "error" not in out

    def read(self, workspace: str) -> list[dict[str, Any]] | None:
        out = self._post("audit.read", {"workspace": self.workspace, "from": PARTICIPANT,
                                        "filter": {"method": "task.create",
                                                   "from": PARTICIPANT}})
        if out is None or "error" in out:
            return None
        records = []
        for entry in (out.get("result") or {}).get("entries", []):
            params = (entry.get("envelope") or {}).get("params") or {}
            rec = params.get("input")
            if params.get("kind") == "brevet.anchor" and isinstance(rec, dict) \
                    and rec.get("workspace") == workspace:
                records.append(rec)
        return records


def target(ref: str, workdir: Path) -> FileTarget | CHAPTarget:
    if ref.startswith("file:"):
        return FileTarget(ref, workdir)
    if ref.startswith("chap:"):
        return CHAPTarget(ref, workdir)
    raise ValueError(f"unknown anchor {ref!r}: write file:<path> or chap:<workspace>@<url>")


# ------------------------------------------------------------- heads

def chain_hashes(ledger) -> list[str]:
    """The chain hash of every envelope, in order."""
    return [e.get("chain_hash", "") for e in ledger.read()]


def _unsigned(record: dict[str, Any]) -> dict[str, Any]:
    return {k: v for k, v in record.items() if k != "signature"}


def head_record(ledger, agent: str, signer: Signer, workspace: str) -> dict[str, Any] | None:
    hashes = chain_hashes(ledger)
    if not hashes:
        return None
    record = {"kind": "brevet.anchor", "workspace": workspace, "agent": agent,
              "chain_id": hashes[0], "count": len(hashes), "chain_hash": hashes[-1],
              "anchored_at": _now(), "public_key": signer.public_key_hex()}
    record["signature"] = signer.sign(_unsigned(record))
    return record


def _outbox(workdir: Path) -> Path:
    return workdir / "anchor_outbox.jsonl"


def anchor(ledger, workdir: str | Path, *, manifest: Any = None, agent: str | None = None,
           refs: list[str] | None = None) -> dict[str, Any]:
    """Write the chain's head to every configured target. Heads that cannot
    be delivered wait in ``anchor_outbox.jsonl`` and go with the next one."""
    wd = Path(workdir)
    refs = configured(wd, manifest) if refs is None else refs
    if not refs:
        return {"anchored": False, "targets": [], "note": "no anchor is configured"}
    m = manifest if manifest is not None else _manifest_beside(wd)
    name = agent or (m.agent if m is not None else "agent")
    record = head_record(ledger, name, Signer(wd / "keys" / "brevet_ed25519.pem"),
                         workspace_id(wd, create=True))
    if record is None:
        return {"anchored": False, "targets": [], "note": "the evidence chain is empty"}
    waiting: list[dict[str, Any]] = []
    box = _outbox(wd)
    if box.exists():
        for line in box.read_text(encoding="utf-8", errors="replace").splitlines():
            try:
                item = json.loads(line)
            except ValueError:
                continue
            if isinstance(item, dict) and isinstance(item.get("record"), dict) \
                    and isinstance(item.get("ref"), str):
                waiting.append(item)
    hashes = chain_hashes(ledger)

    def still_true(rec: dict[str, Any]) -> bool:
        """Queued records are re-checked before they leave: this workspace's,
        signed by its key, and still a head of this chain."""
        count = int(rec.get("count") or 0)
        return (rec.get("workspace") == record["workspace"]
                and rec.get("public_key") == record["public_key"]
                and Signer.verify(rec.get("public_key", ""), _unsigned(rec),
                                  rec.get("signature", ""))
                and rec.get("chain_id") == record["chain_id"]
                and 0 < count <= len(hashes) and hashes[count - 1] == rec.get("chain_hash"))

    waiting = [w for w in waiting if still_true(w["record"])]
    results, still = [], []
    for ref in refs:
        try:
            t = target(ref, wd)
        except ValueError as e:
            results.append({"ref": ref, "written": False, "error": str(e)})
            continue
        queued = [w["record"] for w in waiting if w.get("ref") == ref]
        delivered = all(t.write(r) for r in queued) if queued else True
        ok = delivered and t.write(record)
        if not ok:
            still += [{"ref": ref, "record": r} for r in queued] if not delivered else []
            still.append({"ref": ref, "record": record})
        results.append({"ref": ref, "written": ok})
    if still or box.exists():
        box.write_text("".join(json.dumps(w, sort_keys=True) + "\n" for w in still),
                       encoding="utf-8")
    return {"anchored": any(r["written"] for r in results), "count": record["count"],
            "chain_hash": record["chain_hash"], "targets": results}


def check(ledger, workdir: str | Path, *, manifest: Any = None, agent: str | None = None,
          refs: list[str] | None = None) -> dict[str, Any]:
    """Check the chain against every anchor that applies to it."""
    wd = Path(workdir)
    refs = configured(wd, manifest) if refs is None else refs
    envelopes = list(ledger.read())
    hashes = [e.get("chain_hash", "") for e in envelopes]
    chain_id = hashes[0] if hashes else None
    report: dict[str, Any] = {"configured": bool(refs), "targets": [], "problems": []}
    wid = workspace_id(wd, create=False)
    if wid is None:
        report["note"] = "not anchored yet"
    governing = [i + 1 for i, e in enumerate(envelopes) if e.get("kind") in ANCHORED_KINDS]
    latest_anchored = 0
    for ref in refs:
        try:
            t = target(ref, wd)
        except ValueError as e:
            report["problems"].append(str(e))
            continue
        records = t.read(wid or "")
        if records is None:
            report["targets"].append({"ref": ref, "reachable": False})
            continue
        entry: dict[str, Any] = {"ref": ref, "reachable": True, "anchors": len(records)}
        for rec in records:
            if not Signer.verify(rec.get("public_key", ""), _unsigned(rec),
                                 rec.get("signature", "")):
                report["problems"].append(f"{ref} holds an anchor whose signature does "
                                          f"not verify")
                continue
            if rec.get("chain_id") != chain_id:
                report["problems"].append(
                    f"{ref} anchors a different evidence chain for this workspace: the "
                    f"chain was replaced")
                break
            count = int(rec.get("count") or 0)
            if count > len(hashes):
                report["problems"].append(
                    f"{ref} anchored {count} envelopes, but the chain now holds "
                    f"{len(hashes)}: it was cut short")
            elif count and hashes[count - 1] != rec.get("chain_hash"):
                report["problems"].append(
                    f"{ref} anchored envelope {count} with a different hash: the chain "
                    f"was rewritten")
            entry["latest"] = max(entry.get("latest", 0), count)
        latest_anchored = max(latest_anchored, entry.get("latest", 0))
        report["targets"].append(entry)
    reachable = any(t.get("reachable") for t in report["targets"])
    unanchored = [n for n in governing if n > latest_anchored]
    if reachable and governing and latest_anchored == 0:
        # history exists but no anchor names this workspace: it was never anchored
        # from this path, or the anchors were lost
        report["problems"].append(
            "no anchor holds this workspace's chain although it records governing steps; "
            "if anchoring is new here, run brevet anchor once")
    elif reachable and unanchored:
        report["unanchored"] = len(unanchored)
    report["problems"] = sorted(set(report["problems"]))
    report["ok"] = not report["problems"]
    return report
