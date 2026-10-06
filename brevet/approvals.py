"""Signed approvals: proof that a named person took each decision.

An approver holds an Ed25519 key that lives outside the workspace, encrypted
with a passphrase only they know. An agent that can call every Brevet tool
still cannot approve anything, because it cannot sign. Once a workspace
registers its first approver, every dawn decision, release and recall must
carry approver signatures:

    request   anyone, including an agent over MCP, asks for a decision; the
              exact decision is stored in the workspace as a pending request
    approve   the person reviews it in a terminal (``brevet approve``),
              unlocks their key with the passphrase and signs it
    apply     with enough signatures (the named human, or the threshold of a
              mission group's members), Brevet applies the decision and
              records the signatures on the evidence chain

The approver register lives on the evidence chain as ``brevet.approver``
envelopes. The first approver registers themselves; every later change must
be signed by an approver already registered, and a new key must also sign its
own registration, proving its holder has it. ``verify_approvals`` replays the
chain and checks every signature against the register as it stood then.

A signature proves that the holder of a registered key signed. Making sure
the right person holds that key (identity proofing, custody, loss) is the
deployment's job.
"""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from brevet.canonical import Signer, canonical_json, object_sha256
from brevet.identity import require_identity
from brevet.models import _now, new_id
from brevet.workdir import file_lock

DECISION_KINDS = ("brevet.promotion", "brevet.release", "brevet.recall")
MIN_PASSPHRASE = 8
_REQUEST_FIELDS = {"request_id", "requested_at"}


# ------------------------------------------------------------- keys

def key_dir(directory: str | Path | None = None) -> Path:
    """Where approver keys live: outside any workspace, by default
    ~/.config/brevet/approvers (override with BREVET_APPROVER_DIR)."""
    if directory:
        return Path(directory).expanduser()
    env = os.environ.get("BREVET_APPROVER_DIR")
    return Path(env).expanduser() if env else Path.home() / ".config" / "brevet" / "approvers"


def _stem(identity: str) -> str:
    return re.sub(r"[^A-Za-z0-9._@-]", "_", identity)


@dataclass
class ApproverKey:
    identity: str
    _key: Ed25519PrivateKey = field(repr=False)

    @property
    def public_key(self) -> str:
        return self._key.public_key().public_bytes(
            serialization.Encoding.Raw, serialization.PublicFormat.Raw).hex()

    def sign(self, payload: dict[str, Any]) -> str:
        return self._key.sign(canonical_json(payload).encode("utf-8")).hex()


def create_key(identity: str, passphrase: str,
               directory: str | Path | None = None) -> ApproverKey:
    """Create a passphrase-protected approver key for a person."""
    who = require_identity(identity, role="approver key holder")
    if not who.startswith("human:"):
        raise ValueError("approver keys belong to people: use a human:<who> identity, "
                         "and register it as a member of a mission group")
    if len(passphrase or "") < MIN_PASSPHRASE:
        raise ValueError(f"choose a passphrase of at least {MIN_PASSPHRASE} characters")
    d = key_dir(directory)
    if not d.exists():
        d.mkdir(parents=True)
        try:
            d.chmod(0o700)
        except OSError:  # pragma: no cover
            pass
    pem_path = d / f"{_stem(who)}.pem"
    if pem_path.exists():
        raise FileExistsError(f"an approver key for {who} already exists at {pem_path}")
    key = Ed25519PrivateKey.generate()
    pem = key.private_bytes(
        serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
        serialization.BestAvailableEncryption(passphrase.encode("utf-8")))
    fd = os.open(pem_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "wb") as f:
        f.write(pem)
    approver = ApproverKey(who, key)
    (d / f"{_stem(who)}.json").write_text(json.dumps(
        {"identity": who, "public_key": approver.public_key, "created_at": _now()},
        indent=2), encoding="utf-8")
    return approver


def load_key(identity: str, passphrase: str,
             directory: str | Path | None = None) -> ApproverKey:
    """Unlock a person's approver key with their passphrase."""
    who = require_identity(identity, role="approver key holder")
    pem_path = key_dir(directory) / f"{_stem(who)}.pem"
    if not pem_path.exists():
        raise FileNotFoundError(f"no approver key for {who} in {pem_path.parent}")
    try:
        key = serialization.load_pem_private_key(
            pem_path.read_bytes(), password=(passphrase or "").encode("utf-8"))
    except (ValueError, TypeError):
        raise PermissionError(f"wrong passphrase for {who}") from None
    if not isinstance(key, Ed25519PrivateKey):
        raise TypeError(f"{pem_path} is not an Ed25519 key")
    return ApproverKey(who, key)


def local_identities(directory: str | Path | None = None) -> list[str]:
    """Identities with an approver key on this machine."""
    d = key_dir(directory)
    if not d.exists():
        return []
    out = []
    for meta in d.glob("*.json"):
        if meta.with_suffix(".pem").exists():
            try:
                out.append(json.loads(meta.read_text(encoding="utf-8"))["identity"])
            except (ValueError, KeyError):
                continue
    return sorted(out)


# ------------------------------------------------------------- payloads

def promotion_payload(capability_id: str, outcome: str, to_layer: str | None,
                      content_hash: str, approver: str, notes: str) -> dict[str, Any]:
    return {"kind": "brevet.promotion", "capability_id": capability_id, "outcome": outcome,
            "to_layer": to_layer, "content_hash": content_hash, "approver": approver,
            "notes": notes}


def release_payload(agent: str, from_version: str | None, to_version: str, channel: str,
                    lockfile_hash: str, delta_held_in: Any, delta_held_out: Any,
                    rationale: str, approver: str) -> dict[str, Any]:
    return {"kind": "brevet.release", "agent": agent, "from_version": from_version,
            "to_version": to_version, "channel": channel, "lockfile_hash": lockfile_hash,
            "delta_held_in": delta_held_in, "delta_held_out": delta_held_out,
            "rationale": rationale, "approver": approver}


def recall_payload(capability_id: str, content_hash: str | None, reason: str,
                   reason_class: str, severity: str, action: str,
                   issued_by: str) -> dict[str, Any]:
    return {"kind": "brevet.recall", "capability_id": capability_id,
            "content_hash": content_hash, "reason": reason, "reason_class": reason_class,
            "severity": severity, "action": action, "issued_by": issued_by}


def expected_from_envelope(kind: str, body: dict[str, Any]) -> dict[str, Any]:
    """The payload a decision envelope's approval must have signed."""
    if kind == "brevet.promotion":
        return promotion_payload(body.get("capability_id"), body.get("outcome"),
                                 body.get("to_layer"), body.get("content_hash"),
                                 body.get("approver"), body.get("notes", ""))
    if kind == "brevet.release":
        es = body.get("eval_summary") or {}
        return release_payload(body.get("agent"), body.get("from_version"),
                               body.get("to_version"), body.get("channel"),
                               body.get("lockfile_hash"), es.get("delta_held_in"),
                               es.get("delta_held_out"), body.get("rationale") or "",
                               body.get("approved_by"))
    return recall_payload(body.get("capability_id"), body.get("content_hash"),
                          body.get("reason"), body.get("reason_class"), body.get("severity"),
                          body.get("action"), body.get("issued_by"))


def _decider(payload: dict[str, Any]) -> str:
    return payload.get("approver") or payload.get("issued_by") or ""


# ------------------------------------------------------------- register

@dataclass
class Register:
    """The approvers a workspace recognises, rebuilt from the evidence chain."""

    approvers: dict[str, dict[str, Any]] = field(default_factory=dict)
    thresholds: dict[str, int] = field(default_factory=dict)

    def active(self) -> dict[str, dict[str, Any]]:
        return {i: a for i, a in self.approvers.items() if a["active"]}

    @property
    def enabled(self) -> bool:
        """Signed approvals are required once any approver is registered."""
        return bool(self.active())

    def members(self, group: str) -> set[str]:
        return {i for i, a in self.active().items() if group in a["groups"]}

    def threshold(self, group: str) -> int:
        return self.thresholds.get(group, 1)

    def valid_signers(self, payload: dict[str, Any],
                      signatures: list[dict[str, Any]] | None) -> tuple[set[str], list[str]]:
        """Active approvers whose signatures over ``payload`` verify, and the
        problems with any signature that does not."""
        good: set[str] = set()
        problems: list[str] = []
        for sig in signatures or []:
            who = sig.get("identity")
            entry = self.active().get(who)
            if entry is None:
                problems.append(f"{who} is not an active approver")
            elif sig.get("public_key") != entry["public_key"]:
                problems.append(f"{who} signed with a key that is not registered")
            elif not Signer.verify(entry["public_key"], payload, sig.get("signature", "")):
                problems.append(f"the signature by {who} does not verify")
            else:
                good.add(who)
        return good, problems

    def change_problems(self, record: dict[str, Any]) -> list[str]:
        """Check a register change against the register as it stands."""
        payload = record.get("payload") or {}
        if payload.get("kind") != "brevet.approver":
            return ["not a register change"]
        if record.get("payload_hash") != object_sha256(payload):
            return ["the payload hash does not match the payload"]
        change, who = payload.get("change"), payload.get("identity")
        sigs = record.get("signatures") or []
        if change == "add":
            possession = any(
                s.get("identity") == who and s.get("public_key") == payload.get("public_key")
                and Signer.verify(payload.get("public_key", ""), payload, s.get("signature", ""))
                for s in sigs)
            if not possession:
                return [f"the new key for {who} must sign its own registration"]
            if not self.enabled:
                return []  # the first approver registers themselves
            existing = self.active().get(who)
            rotation = (existing is not None and
                        sorted(existing["groups"]) == sorted(payload.get("groups") or []))
            signers, _ = self.valid_signers(payload, sigs)
            if rotation and signers:
                return []
            if signers - {who}:
                return []
            return [f"registering {who} needs the signature of another active approver"]
        if change == "revoke":
            if who not in self.active():
                return [f"{who} is not an active approver"]
            if len(self.active()) == 1:
                return ["the last active approver cannot be revoked"]
            signers, problems = self.valid_signers(payload, sigs)
            return [] if signers else problems + ["a revocation needs an active approver's signature"]
        if change == "threshold":
            group, count = payload.get("group"), payload.get("threshold")
            members = self.members(group or "")
            if not isinstance(count, int) or count < 1 or count > len(members):
                return [(f"{group} has {len(members)} active member(s); "
                         f"its threshold must be between 1 and that number")]
            signers, problems = self.valid_signers(payload, sigs)
            if signers & members:
                return []
            return problems + [f"a threshold change needs the signature of a member of {group}"]
        return [f"unknown register change {change!r}"]

    def apply(self, record: dict[str, Any]) -> None:
        payload = record["payload"]
        if payload["change"] == "add":
            self.approvers[payload["identity"]] = {
                "public_key": payload["public_key"],
                "groups": sorted(payload.get("groups") or []), "active": True}
        elif payload["change"] == "revoke":
            self.approvers[payload["identity"]]["active"] = False
        elif payload["change"] == "threshold":
            self.thresholds[payload["group"]] = payload["threshold"]

    def approval_problems(self, approval: dict[str, Any] | None,
                          expected: dict[str, Any]) -> list[str]:
        """Why ``approval`` does not authorise the decision ``expected``."""
        if not isinstance(approval, dict):
            return ["the decision carries no approval"]
        payload = approval.get("payload") or {}
        problems = []
        if approval.get("payload_hash") != object_sha256(payload):
            problems.append("the payload hash does not match the payload")
        differs = sorted({k for k, v in expected.items() if payload.get(k) != v}
                         | (set(payload) - set(expected) - _REQUEST_FIELDS))
        if differs:
            problems.append("the signed request does not match this decision "
                            f"({', '.join(differs)})")
        signers, sig_problems = self.valid_signers(payload, approval.get("signatures"))
        problems += sig_problems
        who = _decider(expected)
        if who.startswith("human:"):
            allowed, need = ({who} if who in self.active() else set()), 1
        else:
            allowed, need = self.members(who), self.threshold(who)
        if not allowed:
            problems.append(f"{who} has no active registered approver")
        elif len(signers & allowed) < need:
            problems.append(f"{who} needs {need} signature(s) from {', '.join(sorted(allowed))}; "
                            f"it has {len(signers & allowed)}")
        return problems


def register_from_chain(ledger) -> Register:
    """The register as the evidence chain defines it now; changes that fail
    their checks are ignored here and reported by ``verify_approvals``."""
    register = Register()
    for env in ledger.read("brevet.approver"):
        body = env.get("body", {})
        if not register.change_problems(body):
            register.apply(body)
    return register


def used_request_ids(ledger) -> set[str]:
    used = set()
    for env in ledger.read():
        body = env.get("body") or {}
        if env.get("kind") in DECISION_KINDS:
            rid = ((body.get("approval") or {}).get("payload") or {}).get("request_id")
        elif env.get("kind") == "brevet.approver":
            rid = (body.get("payload") or {}).get("request_id")
        else:
            continue
        if rid:
            used.add(rid)
    return used


def enforce(ledger, expected: dict[str, Any],
            approval: dict[str, Any] | None) -> dict[str, Any] | None:
    """Check the approval for a decision about to be applied.

    Without registered approvers, decisions stay unsigned. With them, a valid
    approval is required, and each signed request can be applied only once."""
    register = register_from_chain(ledger)
    if not register.enabled:
        if approval is not None:
            raise ValueError("no approvers are registered in this workspace, so it does "
                             "not take signed approvals; register one with "
                             "'brevet approver add'")
        return None
    if approval is None:
        raise PermissionError(
            "this workspace requires signed approvals: request the decision, then sign "
            "it in a terminal with 'brevet approve'")
    problems = register.approval_problems(approval, expected)
    rid = (approval.get("payload") or {}).get("request_id")
    if not rid:
        problems.append("the signed request has no request id")
    elif rid in used_request_ids(ledger):
        problems.append(f"request {rid} has already been applied")
    if problems:
        raise PermissionError("signed approval rejected: " + "; ".join(problems))
    return approval


def verify_approvals(ledger) -> dict[str, Any]:
    """Replay the chain and check every register change and every decision.

    Decisions taken before the first approver was registered count as
    unsigned; after that, each needs a valid approval used only once."""
    register = Register()
    used: set[str] = set()
    signed = unsigned = changes = 0
    invalid: list[dict[str, str]] = []
    for env in ledger.read():
        kind, body = env.get("kind"), env.get("body") or {}
        if kind == "brevet.approver":
            problems = register.change_problems(body)
            rid = (body.get("payload") or {}).get("request_id")
            if rid in used:
                problems = [*problems, f"request {rid} was applied twice"]
            if problems:
                invalid.append({"envelope_id": env.get("envelope_id", ""),
                                "kind": kind, "problem": "; ".join(problems)})
                continue
            used.add(rid)
            register.apply(body)
            changes += 1
        elif kind in DECISION_KINDS:
            approval = body.get("approval")
            if not register.enabled:
                if approval:
                    invalid.append({"envelope_id": env.get("envelope_id", ""), "kind": kind,
                                    "problem": "an approval recorded before any approver "
                                               "was registered"})
                else:
                    unsigned += 1
                continue
            problems = register.approval_problems(approval, expected_from_envelope(kind, body))
            rid = ((approval or {}).get("payload") or {}).get("request_id")
            if rid and rid in used:
                problems.append(f"request {rid} was applied twice")
            if problems:
                invalid.append({"envelope_id": env.get("envelope_id", ""), "kind": kind,
                                "problem": "; ".join(problems)})
                continue
            used.add(rid)
            signed += 1
    return {"signing_required": register.enabled, "approvers": len(register.active()),
            "register_changes": changes, "signed": signed,
            "unsigned_before_signing": unsigned, "invalid": invalid}


# ------------------------------------------------------------- requests

class PendingRequests:
    """Requests awaiting signatures, one JSON line per version (latest wins)."""

    def __init__(self, workdir: str | Path):
        self.path = Path(workdir) / "approvals.jsonl"

    def _all(self) -> dict[str, dict[str, Any]]:
        out: dict[str, dict[str, Any]] = {}
        if self.path.exists():
            for line in self.path.read_text(encoding="utf-8", errors="replace").splitlines():
                try:
                    req = json.loads(line)
                except ValueError:
                    continue
                if isinstance(req, dict) and req.get("request_id"):
                    out[req["request_id"]] = req
        return out

    def save(self, request: dict[str, Any]) -> None:
        with file_lock(self.path), self.path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(request, ensure_ascii=False) + "\n")

    def get(self, request_id: str) -> dict[str, Any]:
        req = self._all().get(request_id)
        if req is None:
            raise KeyError(f"no approval request '{request_id}'")
        return req

    def pending(self) -> list[dict[str, Any]]:
        return [r for r in self._all().values() if r.get("status") == "pending"]


def create_request(workdir: str | Path, payload: dict[str, Any], *, summary: str,
                   manifest_path: str | Path | None = None) -> dict[str, Any]:
    """Store a decision awaiting signatures. Asking again for the same
    decision returns the request already pending rather than a duplicate."""
    for existing in PendingRequests(workdir).pending():
        if {k: v for k, v in existing["payload"].items() if k not in _REQUEST_FIELDS} == payload:
            return existing
    rid = new_id("req")
    payload = {**payload, "request_id": rid, "requested_at": _now()}
    request = {"request_id": rid, "payload": payload, "payload_hash": object_sha256(payload),
               "signatures": [], "status": "pending", "summary": summary,
               "manifest_path": str(manifest_path) if manifest_path else None,
               "created_at": payload["requested_at"]}
    PendingRequests(workdir).save(request)
    return request


def sign_request(workdir: str | Path, request_id: str, key: ApproverKey) -> dict[str, Any]:
    """Add one person's signature to a pending request."""
    store = PendingRequests(workdir)
    req = store.get(request_id)
    if req.get("status") != "pending":
        raise ValueError(f"request {request_id} is {req.get('status')}, not pending")
    if any(s.get("identity") == key.identity for s in req["signatures"]):
        raise ValueError(f"{key.identity} has already signed request {request_id}")
    req["signatures"].append({"identity": key.identity, "public_key": key.public_key,
                              "signature": key.sign(req["payload"]), "signed_at": _now()})
    store.save(req)
    return req


def _approval(req: dict[str, Any]) -> dict[str, Any]:
    return {"payload": req["payload"], "payload_hash": req["payload_hash"],
            "signatures": req["signatures"]}


def request_register_add(workdir: str | Path, key: ApproverKey,
                         groups: list[str] | None = None) -> dict[str, Any]:
    """Ask to register (or rotate) an approver key; the key signs its own
    registration, so whoever approves it knows its holder has it."""
    groups = sorted({require_identity(g, role="group", mission_group=True)
                     for g in (groups or [])})
    req = create_request(workdir, {"kind": "brevet.approver", "change": "add",
                                   "identity": key.identity, "public_key": key.public_key,
                                   "groups": groups},
                         summary=f"register {key.identity}"
                                 + (f" in {', '.join(groups)}" if groups else ""))
    return sign_request(workdir, req["request_id"], key)


def request_register_revoke(workdir: str | Path, identity: str) -> dict[str, Any]:
    who = require_identity(identity, role="approver")
    return create_request(workdir, {"kind": "brevet.approver", "change": "revoke",
                                    "identity": who}, summary=f"revoke {who}")


def request_threshold(workdir: str | Path, group: str, count: int) -> dict[str, Any]:
    g = require_identity(group, role="group", mission_group=True)
    return create_request(workdir, {"kind": "brevet.approver", "change": "threshold",
                                    "group": g, "threshold": int(count)},
                          summary=f"require {count} signature(s) for {g}")


def request_promotion(workdir: str | Path, store, capability_id: str, outcome: str, *,
                      approver: str, to_layer: str = "advisory", notes: str = "",
                      manifest_path: str | Path | None = None) -> dict[str, Any]:
    from brevet.lifecycle import prepare_promotion
    cap, layer, who, payload = prepare_promotion(
        store, capability_id, outcome, approver=approver, to_layer=to_layer, notes=notes)
    detail = f" to {layer.value}" if outcome == "promote" else ""
    return create_request(workdir, payload, manifest_path=manifest_path,
                          summary=f"{outcome} {capability_id}{detail} as {who}: {cap.title}")


def request_release(workdir: str | Path, manifest, store, *, to_version: str, channel: str,
                    approver: str, eval_summary: dict[str, Any] | None = None,
                    rationale: str = "",
                    manifest_path: str | Path | None = None) -> dict[str, Any]:
    from brevet.lifecycle import prepare_release
    who, chan, lock, payload = prepare_release(
        manifest, store, to_version=to_version, channel=channel, approver=approver,
        eval_summary=eval_summary, rationale=rationale)
    return create_request(
        workdir, payload, manifest_path=manifest_path,
        summary=f"release {manifest.agent} {manifest.version} -> {to_version} on the "
                f"{chan.value} channel as {who}, locking {len(lock.resolved)} capabilities")


def request_recall(workdir: str | Path, store, capability_id: str, *, reason: str,
                   issued_by: str, reason_class: str = "incorrect", severity: str = "high",
                   action: str = "rollback",
                   manifest_path: str | Path | None = None) -> dict[str, Any]:
    from brevet.lifecycle import prepare_recall
    cap, who, payload = prepare_recall(
        store, capability_id, reason=reason, reason_class=reason_class, severity=severity,
        issued_by=issued_by, action=action)
    return create_request(workdir, payload, manifest_path=manifest_path,
                          summary=f"recall {capability_id} as {who}: {cap.title}")


def apply_if_ready(workdir: str | Path, request_id: str, *,
                   manifest_path: str | Path | None = None) -> dict[str, Any]:
    """Apply a request once its signatures suffice; otherwise say what it needs."""
    wd = Path(workdir)
    store_reqs = PendingRequests(wd)
    req = store_reqs.get(request_id)
    if req.get("status") != "pending":
        return {"request_id": request_id, "status": req.get("status"),
                "result": req.get("result")}
    try:
        return _apply(wd, store_reqs, req, manifest_path)
    except (PermissionError, ValueError, KeyError, FileNotFoundError) as exc:
        # A request that cannot be applied as signed (its decision changed,
        # or a signature is bad) is closed, so it does not linger as pending.
        req["status"], req["error"] = "failed", str(exc.args[0] if exc.args else exc)
        store_reqs.save(req)
        raise


def _apply(wd: Path, store_reqs: PendingRequests, req: dict[str, Any],
           manifest_path: str | Path | None) -> dict[str, Any]:
    import yaml

    from brevet import lifecycle
    from brevet.ledger import Ledger
    from brevet.models import AgentManifest, ReleaseRecord

    request_id = req["request_id"]
    ledger = Ledger(wd / "ledger.jsonl")
    register = register_from_chain(ledger)
    payload, approval = req["payload"], _approval(req)
    kind = payload.get("kind")

    if kind == "brevet.approver":
        problems = register.change_problems(approval)
        if problems:
            if any("needs the signature" in p or "needs an active" in p for p in problems):
                return {"request_id": request_id, "status": "pending", "needs": problems}
            raise PermissionError("register change rejected: " + "; ".join(problems))
        ledger.append("brevet.approver", approval)
        result: dict[str, Any] = {"change": payload["change"],
                                  "identity": payload.get("identity") or payload.get("group")}
    else:
        expected = {k: v for k, v in payload.items() if k not in _REQUEST_FIELDS}
        problems = register.approval_problems(approval, expected)
        if problems:
            if all("needs" in p and "signature(s)" in p for p in problems):
                return {"request_id": request_id, "status": "pending", "needs": problems}
            raise PermissionError("signed approval rejected: " + "; ".join(problems))
        store = lifecycle.CapabilityStore(wd / "capabilities.jsonl")
        if kind == "brevet.promotion":
            cap = lifecycle.dawn_decide(
                store, ledger, payload["capability_id"], payload["outcome"],
                approver=payload["approver"], to_layer=payload.get("to_layer") or "advisory",
                notes=payload.get("notes", ""), approval=approval)
            result = {"capability_id": cap.capability_id,
                      "validation_state": cap.validation_state.value,
                      "authority_layer": cap.authority_layer.value}
        elif kind == "brevet.release":
            mpath = Path(manifest_path or req.get("manifest_path") or "agent.yaml")
            manifest = AgentManifest(**yaml.safe_load(mpath.read_text(encoding="utf-8")))
            manifest, lock, record = lifecycle.release(
                manifest, store, ledger, Signer(wd / "keys" / "brevet_ed25519.pem"),
                to_version=payload["to_version"], channel=payload["channel"],
                approver=payload["approver"],
                eval_summary={"delta_held_in": payload["delta_held_in"],
                              "delta_held_out": payload["delta_held_out"],
                              "gate": "conservative"},
                rationale=payload.get("rationale", ""), approval=approval)
            mpath.write_text(yaml.safe_dump(manifest.model_dump(exclude_none=False),
                                            sort_keys=False), encoding="utf-8")
            (mpath.parent / "capabilities.lock").write_text(lock.model_dump_json(indent=2),
                                                             encoding="utf-8")
            result = {"from": record.from_version, "to": record.to_version,
                      "channel": record.channel.value, "locked": len(lock.resolved)}
        elif kind == "brevet.recall":
            releases = [ReleaseRecord(**e["body"]) for e in ledger.read("brevet.release")]
            notice = lifecycle.recall(
                store, ledger, payload["capability_id"], reason=payload["reason"],
                reason_class=payload["reason_class"], severity=payload["severity"],
                issued_by=payload["issued_by"], releases=releases,
                action=payload["action"], approval=approval)
            result = {"recall_id": notice.recall_id,
                      "affected_releases": [r["version"] for r in notice.affected_releases]}
        else:
            raise ValueError(f"unknown request kind {kind!r}")
    req["status"], req["result"] = "applied", result
    store_reqs.save(req)
    return {"request_id": request_id, "status": "applied", "result": result}
