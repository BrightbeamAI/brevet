"""What an agent may follow: the governed rules of a release, checked before
they are served.

``active_rules`` is what the MCP tool ``brevet_active`` returns, and what the
Claude Cowork example writes to its rules file when no MCP server is
attached; ``resolve`` is what a wrapped agent receives with each task. All
three apply the same checks:

- the evidence chain replays intact and holds every head anchored outside
  the workspace;
- the lock is the one the chain recorded for the latest release;
- that release's approval signatures are valid, and once approvers are
  registered it carries them (other invalid decisions are reported; they
  grant nothing, since locks and the register ignore them);
- the manifest is the one recorded and signed for that release;
- each rule's content and conditions match what was released; a rule that
  was recalled is listed as recalled, never served; a rule missing from the
  store, or switched off without a recall or decision on the chain, is
  withheld and reported;
- a rule is served only inside its validity window and, when the task's
  context is known, only where its conditions match (conditions first).

On a failed check nothing is served. Harness files changed since the release
are reported, never hidden.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import yaml

from brevet import anchor as anchors
from brevet.approvals import verify_approvals
from brevet.canonical import content_sha256, object_sha256
from brevet.harness import compare, inventory, lock_digest
from brevet.ledger import Ledger
from brevet.lifecycle import CapabilityStore, verify_manifest_signature
from brevet.models import AgentManifest, ApplicabilityContext, CapabilitiesLock

#: Capability kinds that govern live agent behaviour. Eval cases, tool
#: bindings and memory fragments stay in the workspace; they are never
#: served to a session as rules to follow.
GOVERNED_KINDS = {"prompt_rule", "skill", "escalation_rule", "loop_policy"}


def load_manifest(mpath: Path) -> AgentManifest | None:
    if not mpath.exists():
        return None
    return AgentManifest(**(yaml.safe_load(mpath.read_text(encoding="utf-8")) or {}))


def find_lock(wd: Path, mpath: Path) -> Path | None:
    """The released lock: beside the manifest, or in the working directory."""
    return next((p for p in (mpath.parent / "capabilities.lock", wd / "capabilities.lock")
                 if p.exists()), None)


def load_lock(wd: Path, mpath: Path) -> CapabilitiesLock | None:
    path = find_lock(wd, mpath)
    return CapabilitiesLock(**json.loads(path.read_text(encoding="utf-8"))) if path else None


def ledger_path(wd: Path, manifest: AgentManifest | None) -> Path:
    evidence = ((manifest.runtime_safety or {}).get("evidence", {}) if manifest else {}) or {}
    ref = evidence.get("ledger", "file:./ledger.jsonl")
    return wd / (ref[5:] if ref.startswith("file:") else "ledger.jsonl")


def file_drift(lock: CapabilitiesLock | None, manifest: AgentManifest | None,
               wd: Path, mpath: Path) -> list[dict[str, Any]]:
    """Harness files that changed since the release locked them."""
    if lock is None or manifest is None or "files" not in lock.harness_sources:
        return []
    live, _ = inventory(manifest, mpath, workdir=wd, sources=["files"])
    return compare([c for c in lock.harness if c.kind == "file"], live)


# ------------------------------------------------------------- the chain

def chain_facts(ledger: Ledger, invalid: set[str] | None = None) -> dict[str, Any]:
    """Recalls, decisions and acknowledgements on the chain, read once.
    Envelopes whose approvals are invalid are ignored."""
    return chain_facts_from(ledger.read(), invalid)


def chain_facts_from(envelopes, invalid: set[str] | None = None) -> dict[str, Any]:
    """``chain_facts`` over envelopes already read, in chain order."""
    invalid = invalid or set()
    recalls: dict[str, dict[str, Any]] = {}
    decisions: dict[str, str] = {}
    acks: list[dict[str, Any]] = []
    for env in envelopes:
        if env.get("envelope_id") in invalid:
            continue
        kind, body = env.get("kind"), env.get("body") or {}
        if kind == "brevet.recall":
            keys = [body.get("capability_id")]
            if body.get("reason_class") != "duplicate":  # a duplicate copy leaves the content
                keys.append(body.get("content_hash"))
            for key in keys:
                if key:
                    recalls.setdefault(key, body)
        elif kind == "brevet.promotion" and body.get("capability_id"):
            decisions[body["capability_id"]] = body.get("outcome", "")
        elif kind == "brevet.recall_ack":
            acks.append(body)
    return {"recalls": recalls,
            "decided_against": {cid for cid, outcome in decisions.items()
                                if outcome != "promote"},
            "acks": acks}


def acknowledged(facts: dict[str, Any], recall_id: str, agent: str,
                 serving_point: str | None = None) -> bool:
    from brevet.recalls import is_acknowledged
    return is_acknowledged(facts["acks"], recall_id, agent, serving_point)


def resolve(lock: CapabilitiesLock, caps: dict[str, Any], facts: dict[str, Any], *,
            context: ApplicabilityContext | None = None,
            at: datetime | None = None) -> dict[str, list]:
    """Sort a lock's governed entries into the rules to serve and the ones
    that are recalled, withheld, out of their validity window or not
    applicable to this context."""
    now = at or datetime.now(timezone.utc)
    out: dict[str, list] = {"rules": [], "recalled": [], "withheld": [], "expired": [],
                            "not_applicable": []}
    for entry in lock.resolved:
        if entry.kind not in GOVERNED_KINDS:
            continue
        recall = facts["recalls"].get(entry.capability_id) \
            or facts["recalls"].get(entry.content_hash)
        if recall is not None:
            out["recalled"].append({
                "capability_id": entry.capability_id, "recall_id": recall.get("recall_id"),
                "reason": recall.get("reason"), "severity": recall.get("severity"),
                "action": recall.get("action")})
            continue
        cap = caps.get(entry.capability_id)
        if cap is not None and cap.revocation_status.value != "active":
            if entry.capability_id not in facts["decided_against"]:
                out["withheld"].append(entry.capability_id)
            continue
        if (cap is None or content_sha256(cap.content) != entry.content_hash
                or (entry.conditions_digest
                    and cap.conditions.digest() != entry.conditions_digest)
                or not cap.releasable):
            out["withheld"].append(entry.capability_id)
            continue
        if not cap.conditions.in_effect(now):
            out["expired"].append(entry.capability_id)
            continue
        if context is not None and not cap.conditions.matches(context):
            out["not_applicable"].append(entry.capability_id)
            continue
        out["rules"].append({
            "capability_id": entry.capability_id,
            "kind": entry.kind,
            "authority_layer": entry.authority_layer,
            "approved_by": entry.approved_by,
            "content_hash": entry.content_hash,
            "content": cap.content.strip(),
            "conditions": {k: v for k, v in cap.conditions.model_dump().items() if v},
        })
    return out


def rules_text(rules: list[dict[str, Any]], version: str | None = None) -> str:
    """The governed rules as a plain block an agent can read."""
    if not rules:
        return ""
    head = f"Governed rules (Brevet release {version})" if version else "Governed rules"
    lines = [head + ", approved by people; follow each where its conditions apply:"]
    for r in rules:
        cond = ", ".join(f"{k}={v}" for k, v in r["conditions"].items())
        lines.append(f"- [{r['authority_layer']}] {r['content']}" + (f" ({cond})" if cond else ""))
    return "\n".join(lines)


def active_rules(wd: Path, mpath: Path, *, ledger: Ledger | None = None,
                 store: CapabilityStore | None = None,
                 context: ApplicabilityContext | None = None) -> dict[str, Any]:
    """The governed rules a session may follow now, or the reason there are none."""
    manifest = load_manifest(mpath)
    ledger = ledger or Ledger(ledger_path(wd, manifest), anchoring=False)
    ok, n = ledger.verify()
    if not ok:
        return {"count": 0, "active": [], "chain_ok": False,
                "error": f"the evidence chain is broken at envelope {n + 1}; follow no "
                         f"governed rules and tell the user to run brevet verify"}
    anchor_report = anchors.check(ledger, wd, manifest=manifest)
    if not anchor_report["ok"]:
        return {"count": 0, "active": [], "chain_ok": False,
                "error": "the evidence chain does not match its anchors ("
                         + "; ".join(anchor_report["problems"])
                         + "); follow no governed rules and tell the user"}
    approvals = verify_approvals(ledger)
    invalid = {i["envelope_id"] for i in approvals["invalid"]}
    lock = load_lock(wd, mpath)
    if lock is None:
        return {"count": 0, "active": [], "chain_ok": True, "note": "no signed release yet"}
    digest = lock_digest(lock)
    releases = [e for e in ledger.read("brevet.release")
                if e["body"].get("agent") == lock.agent]
    latest_env = releases[-1] if releases else None
    latest = latest_env["body"] if latest_env else None

    def refuse(reason: str) -> dict[str, Any]:
        return {"count": 0, "active": [], "chain_ok": True,
                "agent": lock.agent, "agent_version": lock.agent_version,
                "error": f"{reason}; follow no governed rules and tell the user"}

    if latest is None:
        return refuse("capabilities.lock has no release on the evidence chain")
    version = latest.get("to_version")
    if not (digest == lock.lockfile_hash == latest.get("lockfile_hash")):
        return refuse(f"capabilities.lock does not match the digest recorded for "
                      f"release {version}")
    if latest_env.get("envelope_id") in invalid:
        return refuse(f"release {version} carries invalid approval signatures; tell the "
                      f"user to run brevet verify")
    if approvals["signing_required"] and not latest.get("approval"):
        return refuse(f"approvers are registered, but release {version} carries no "
                      f"approver signatures; release again with their signatures")
    manifest_changed = bool(
        manifest is not None and latest.get("manifest_hash")
        and object_sha256(manifest.unsigned_payload()) != latest["manifest_hash"])
    signature = "not checked: this release predates recorded signing keys"
    if latest.get("signer_public_key"):
        if (manifest is None or manifest.version != version or manifest_changed
                or not verify_manifest_signature(manifest, latest["signer_public_key"])
                or (manifest.release or {}).get("lockfile_hash") != digest):
            return refuse(f"the manifest is not the one signed for release {version}")
        signature = "verified"
    if not manifest_changed and manifest is not None:
        from brevet.releases import ensure_archive
        ensure_archive(wd, manifest, lock, mpath, latest)

    caps = (store or CapabilityStore(wd / "capabilities.jsonl")).all()
    facts = chain_facts(ledger, invalid)
    sorted_ = resolve(lock, caps, facts, context=context)
    rules = sorted_["rules"]
    out: dict[str, Any] = {
        "agent": lock.agent,
        "agent_version": lock.agent_version,
        "lockfile_hash": lock.lockfile_hash,
        "chain_ok": True,
        "anchors": _anchor_summary(anchor_report),
        "signature": signature,
        "signed_approvals": ("required and verified" if approvals["signing_required"]
                             else "not required in this workspace"),
        "count": len(rules),
        "active": rules,
    }
    if sorted_["recalled"]:
        for item in sorted_["recalled"]:
            item["acknowledged"] = acknowledged(facts, item["recall_id"], lock.agent, "mcp")
        out["recalled"] = sorted_["recalled"]
        if not all(item["acknowledged"] for item in sorted_["recalled"]):
            out["recall_notice"] = (
                "these rules were recalled: stop applying them now, even if they were "
                "served earlier in this session, then call brevet_acknowledge with their "
                "recall ids")
    confirm = _recalls_to_confirm(facts, lock.agent,
                                  {item["recall_id"] for item in sorted_["recalled"]})
    if confirm:
        out["recalls_to_confirm"] = confirm
        out["confirm_notice"] = (
            "these recalls flagged earlier releases of this agent and are not in the release "
            "served here; call brevet_acknowledge with their recall ids to confirm")
    if sorted_["expired"]:
        out["expired"] = sorted_["expired"]
    if sorted_["not_applicable"]:
        out["not_applicable"] = sorted_["not_applicable"]
    if sorted_["withheld"]:
        out["withheld"] = sorted_["withheld"]
        out["warning"] = ("these capabilities are missing, switched off without a recall, or "
                          "their content or conditions no longer match the release, so they "
                          "were not served; tell the user")
    if invalid:
        out["approvals_warning"] = (f"{len(invalid)} other decision(s) on the evidence chain "
                                    f"carry invalid approval signatures and were ignored; tell "
                                    f"the user to run brevet verify")
    if manifest_changed:
        out["manifest_warning"] = (f"agent.yaml changed after release {version}; tell the "
                                   f"user the change is not under change control until they "
                                   f"release it")
    drift = file_drift(lock, manifest, wd, mpath)
    if drift:
        out["harness_drift"] = drift
        out["harness_warning"] = ("harness files changed since this release; tell the user "
                                  "they are not under change control until released")
    return out


def _recalls_to_confirm(facts: dict[str, Any], agent: str, served: set[str]) -> list[dict]:
    """Recalls that flagged this agent's releases, are not in the release
    served now and that no session has confirmed yet."""
    out, seen = [], set()
    for recall in facts["recalls"].values():
        rid = recall.get("recall_id")
        if not rid or rid in seen or rid in served:
            continue
        seen.add(rid)
        if not any(a.get("agent") == agent for a in recall.get("affected_releases") or []):
            continue
        if acknowledged(facts, rid, agent, "mcp"):
            continue
        out.append({"recall_id": rid, "capability_id": recall.get("capability_id"),
                    "reason": recall.get("reason")})
    return out


def _anchor_summary(report: dict[str, Any]) -> str:
    if not report["configured"]:
        return "none configured"
    reached = [t for t in report["targets"] if t.get("reachable")]
    missed = [t["ref"] for t in report["targets"] if not t.get("reachable")]
    text = f"verified against {len(reached)} of {len(report['targets'])} anchor(s)"
    if missed:
        text += f"; unreachable: {', '.join(missed)}"
    if report.get("unanchored"):
        text += (f"; {report['unanchored']} governing step(s) since the last anchor, "
                 f"waiting to be anchored (brevet anchor)")
    return text
