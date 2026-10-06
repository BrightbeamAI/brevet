"""The dawn gate, releases and recall.

This module holds the capability store and the three stages that change
what an agent may use: the dawn gate, releases and recall.

This is the circadian contract: a conforming deployment runs exactly one
signed release and changes it only between releases, and what the next
release contains is a human decision taken at the dawn gate. The wrapper
holds a running agent to that release: the harness drift check
(``brevet.harness``) stops it on any unreleased change to its files, its
code or its tools outside the shadow channel.

Nothing in this module can promote a capability by itself. Every decision
names the identity that took it, and only ``human:<who>`` and
``mission_group:<name>`` identities are accepted: ``agent:``, ``model:``,
``dream:`` and every other namespace are refused, so a candidate cannot be
promoted under a machine identity (the same rule Metis applies to tacit
fragments). Promotion to Controlled needs a mission group. Once a workspace
registers approvers, every decision must also carry their signatures (see
``brevet.approvals``); before that, the identity supplied is recorded as
given.

Each decision has a ``prepare_*`` step, which validates it and returns the
exact payload an approver signs, and an apply step, which checks the
approval and records the decision."""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from brevet.approvals import (
    enforce,
    evals_of,
    promotion_payload,
    recall_payload,
    release_payload,
)
from brevet.canonical import Signer, content_sha256, object_sha256
from brevet.evals import conservative_gate
from brevet.harness import lock_digest
from brevet.identity import require_identity
from brevet.models import (
    AgentManifest,
    AuthorityLayer,
    CapabilitiesLock,
    CapabilityObject,
    HarnessComponent,
    LockedCapability,
    RecallNotice,
    ReleaseChannel,
    ReleaseRecord,
    RevocationStatus,
    ValidationState,
    _now,
)
from brevet.workdir import file_lock

DAWN_OUTCOMES = ("promote", "hold", "reject", "re_elicit")
RECALL_REASONS = ("incorrect", "unsafe", "consent_withdrawn", "superseded", "stale",
                  "compliance", "duplicate", "other")
RECALL_SEVERITIES = ("low", "medium", "high", "critical")
RECALL_ACTIONS = ("quarantine", "rollback", "re_review")

_VERSION = re.compile(r"^(\d+)\.(\d+)\.(\d+)$")

__all__ = ["require_identity"]  # re-exported for existing imports


def _body(record) -> dict[str, Any]:
    """An envelope body for a release or recall; ``approval`` appears only
    when the decision was signed."""
    body = record.model_dump(mode="json")
    for key in ("approval", "restores", "set_aside"):
        if body.get(key) is None:
            body.pop(key, None)
    return body


def _version_tuple(version: str) -> tuple[int, int, int] | None:
    m = _VERSION.match(version or "")
    return (int(m[1]), int(m[2]), int(m[3])) if m else None


class CapabilityStore:
    """Flat JSONL store of capability objects (one line per version)."""

    def __init__(self, path: Path | str):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def add(self, cap: CapabilityObject) -> None:
        with file_lock(self.path), self.path.open("a", encoding="utf-8") as f:
            f.write(cap.model_dump_json() + "\n")

    def all(self) -> dict[str, CapabilityObject]:
        out: dict[str, CapabilityObject] = {}
        if self.path.exists():
            text = self.path.read_text(encoding="utf-8", errors="replace")
            for line in text.splitlines():
                if not line.strip():
                    continue
                try:
                    cap = CapabilityObject(**json.loads(line))
                except (ValueError, TypeError):
                    continue  # a damaged line is skipped; releases re-check hashes
                out[cap.capability_id] = cap  # last write wins
        return out

    def pending(self) -> list[CapabilityObject]:
        return [
            c for c in self.all().values()
            if c.validation_state in (ValidationState.captured, ValidationState.tier2_pending)
            and c.revocation_status == RevocationStatus.active
        ]


def prepare_promotion(
    store: CapabilityStore,
    capability_id: str,
    outcome: str,
    *,
    approver: str,
    to_layer: AuthorityLayer | str = AuthorityLayer.advisory,
    notes: str = "",
) -> tuple[CapabilityObject, AuthorityLayer, str, dict[str, Any]]:
    """Validate a dawn decision and return the capability, the target layer,
    the approver and the payload an approver signs."""
    if outcome not in DAWN_OUTCOMES:
        raise ValueError(f"outcome must be one of {', '.join(DAWN_OUTCOMES)}; got {outcome!r}")
    layer = AuthorityLayer(to_layer)
    if outcome == "promote" and layer == AuthorityLayer.evidence:
        raise ValueError("promotion raises a capability to advisory or controlled")
    who = require_identity(
        approver, role="dawn approver",
        mission_group=(outcome == "promote" and layer == AuthorityLayer.controlled))
    caps = store.all()
    if capability_id not in caps:
        raise KeyError(f"unknown capability '{capability_id}'")
    cap = caps[capability_id]
    if cap.revocation_status != RevocationStatus.active:
        raise ValueError(f"capability '{capability_id}' is {cap.revocation_status.value} "
                         f"and can no longer be decided at dawn")
    payload = promotion_payload(capability_id, outcome,
                                layer.value if outcome == "promote" else None,
                                cap.content_hash, who, notes)
    return cap, layer, who, payload


def dawn_decide(
    store: CapabilityStore,
    ledger,
    capability_id: str,
    outcome: str,
    *,
    approver: str,
    to_layer: AuthorityLayer | str = AuthorityLayer.advisory,
    notes: str = "",
    approval: dict[str, Any] | None = None,
) -> CapabilityObject:
    """Apply one dawn-gate decision. Rejection never means deletion: a
    rejected capability remains governed evidence of what was considered."""
    cap, layer, who, expected = prepare_promotion(
        store, capability_id, outcome, approver=approver, to_layer=to_layer, notes=notes)
    approval = enforce(ledger, expected, approval)

    if outcome == "promote":
        # Record the approver in provenance so the lockfile can answer
        # "who approved this": mission groups and humans land in their own
        # fields, and the first recorded identity wins.
        if who.startswith("mission_group:"):
            if not cap.provenance.mission_group_reviewed_by:
                cap.provenance.mission_group_reviewed_by = who
        elif not cap.provenance.human_confirmed_by:
            cap.provenance.human_confirmed_by = who
        cap.authority_layer = layer
        cap.validation_state = (
            ValidationState.promoted_to_controlled
            if layer == AuthorityLayer.controlled
            else ValidationState.promoted_to_advisory
        )
    elif outcome == "hold":
        cap.validation_state = ValidationState.held
    elif outcome == "reject":
        cap.validation_state = ValidationState.rejected
        cap.revocation_status = RevocationStatus.rejected
    elif outcome == "re_elicit":
        cap.validation_state = ValidationState.re_elicit

    cap.updated_at = _now()
    cap.lineage.append(f"dawn:{outcome}:{who}")
    store.add(cap)
    body = {"capability_id": capability_id, "outcome": outcome, "approver": who,
            "to_layer": expected["to_layer"], "notes": notes,
            "content_hash": cap.content_hash}
    if approval is not None:
        body["approval"] = approval
    ledger.append("brevet.promotion", body, refs=[capability_id])
    return cap


def _promotions(ledger) -> dict[str, dict[str, Any]]:
    """The latest dawn decision on the evidence chain per capability. Once
    approvers are registered, a decision without valid signatures does not
    count."""
    from brevet.approvals import verify_envelopes
    envelopes = list(ledger.read())
    invalid = {i["envelope_id"] for i in verify_envelopes(envelopes)["invalid"]}
    return {e["body"].get("capability_id"): e["body"] for e in envelopes
            if e.get("kind") == "brevet.promotion" and e.get("envelope_id") not in invalid}


def build_lock(manifest: AgentManifest, store: CapabilityStore,
               harness: tuple[list[HarnessComponent], list[str]] | None = None,
               ledger=None, only: set[str] | None = None) -> CapabilitiesLock:
    """Resolve the releasable capabilities into a lock, with the harness
    components the release runs with when ``harness`` is given.

    Only promoted, active capabilities qualify. A capability whose content
    matches a recalled one is left out, so recalled content cannot return
    under a new identifier. A capability whose stored content no longer
    matches its recorded hash stops the release, and so, when ``ledger`` is
    given, does one marked promoted in the store without a matching
    promotion on the evidence chain. ``only`` limits the lock to the
    capabilities named, as a rollback does."""
    lock = CapabilitiesLock(agent=manifest.agent, agent_version=manifest.version)
    if harness is not None:
        lock.harness, lock.harness_sources = list(harness[0]), sorted(harness[1])
    promotions = _promotions(ledger) if ledger is not None else None
    caps = list(store.all().values())
    duplicates: set[str] = set()   # copies recalled as duplicates leave their content
    if ledger is not None:
        from brevet.recalls import reaches_content, recalls_on_chain
        notices = recalls_on_chain(ledger)
        duplicates = {r.get("capability_id") for r in notices if not reaches_content(r)}
        recalled = {r["content_hash"] for r in notices
                    if reaches_content(r) and r.get("content_hash")}
    else:
        recalled = set()
    recalled |= {c.content_hash for c in caps
                 if c.revocation_status == RevocationStatus.withdrawn and c.content_hash
                 and c.capability_id not in duplicates}
    for cap in caps:
        if not cap.releasable or (only is not None and cap.capability_id not in only):
            continue  # evidence-layer and revoked material is never locked in
        if content_sha256(cap.content) != cap.content_hash:
            raise ValueError(f"capability '{cap.capability_id}' no longer matches its "
                             f"recorded content hash; the capability store was edited")
        if promotions is not None:
            decision = promotions.get(cap.capability_id) or {}
            if (decision.get("outcome") != "promote"
                    or decision.get("to_layer") != cap.authority_layer.value
                    or decision.get("content_hash", cap.content_hash) != cap.content_hash):
                raise ValueError(f"capability '{cap.capability_id}' is marked promoted in the "
                                 f"store, but the evidence chain has no matching promotion; "
                                 f"the capability store was edited")
        if cap.content_hash in recalled:
            continue
        approved_by = cap.provenance.mission_group_reviewed_by or cap.provenance.human_confirmed_by
        lock.resolved.append(
            LockedCapability(
                capability_id=cap.capability_id,
                kind=cap.kind.value,
                content_hash=cap.content_hash,
                authority_layer=cap.authority_layer.value,
                conditions_digest=cap.conditions.digest(),
                approved_by=approved_by or "unrecorded",
                approved_at=cap.updated_at,
                evidence_refs=cap.evidence.supporting_overrides[:20],
            )
        )
    lock.lockfile_hash = lock_digest(lock)
    return lock


def prepare_release(
    manifest: AgentManifest,
    store: CapabilityStore,
    *,
    to_version: str,
    channel: ReleaseChannel | str,
    approver: str,
    eval_summary: dict | None = None,
    rationale: str = "",
    harness: tuple[list[HarnessComponent], list[str]] | None = None,
    ledger=None,
    only: set[str] | None = None,
    restores: str | None = None,
    set_aside: list[str] | None = None,
) -> tuple[str, ReleaseChannel, CapabilitiesLock, dict[str, Any]]:
    """Validate a release and return the approver, the channel, the lock it
    would ship and the payload an approver signs. The payload names the lock
    (with its harness components), the manifest and the evidence behind the
    numbers exactly as they will be recorded, so an approval covers them all.

    The evidence is ``eval_summary["source"]``: ``measured`` (bound to eval
    runs by ``brevet.evals.bind_evals``), ``attested`` (deltas the approver
    vouches for; the default) or ``rollback`` (a return to an earlier
    release, which needs no gate). Production releases need measured runs
    unless the manifest sets ``runtime_safety.evals.allow_attested``."""
    who = require_identity(approver, role="release approver")
    channel = ReleaseChannel(channel)
    target = _version_tuple(to_version)
    if target is None:
        raise ValueError(f"to_version must be written like 1.2.3; got {to_version!r}")
    current = _version_tuple(manifest.version)
    if current is not None and target <= current:
        raise ValueError(f"to_version {to_version} must be higher than the current "
                         f"version {manifest.version}")
    es = {"source": "attested", **(eval_summary or {})}
    if eval_summary is not None:
        eval_summary.setdefault("source", es["source"])
    if es["source"] not in ("measured", "attested", "rollback"):
        raise ValueError(f"unknown eval source {es['source']!r}")
    if (es["source"] == "rollback") != (restores is not None):
        raise ValueError("a rollback names the release it restores, and only a rollback does")
    allow_attested = bool(((manifest.runtime_safety or {}).get("evals") or {})
                          .get("allow_attested"))
    if (channel == ReleaseChannel.production and es["source"] == "attested"
            and not allow_attested):
        raise ValueError(
            "release blocked: production releases need measured eval runs (pass the "
            "'before' and 'after' runs), or set runtime_safety.evals.allow_attested")
    if (channel != ReleaseChannel.shadow and es["source"] != "rollback"
            and not conservative_gate(es.get("delta_held_in", -1),
                                      es.get("delta_held_out", -1))):
        reason = ("release blocked: conservative gate not passed "
                  "(need delta_in >= 0, delta_out >= 0, max > 0)")
        if ledger is not None:  # the cost of governance stays on the record
            ledger.append("brevet.gate", {
                "agent": manifest.agent, "to_version": to_version, "channel": channel.value,
                "reason": reason, "delta_held_in": es.get("delta_held_in"),
                "delta_held_out": es.get("delta_held_out"), "source": es["source"]})
        raise ValueError(reason)
    lock = build_lock(manifest.model_copy(update={"version": to_version}), store, harness,
                      ledger=ledger, only=only)
    if es["source"] == "measured":
        # whatever path the summary took, it must follow from runs on the chain that
        # evaluated exactly this lock and manifest
        if ledger is None:
            raise ValueError("a measured release needs the evidence chain its runs are on")
        from brevet.evals import bind_evals
        bound = bind_evals(ledger, es.get("before"), es.get("after"), agent=manifest.agent,
                           lock=lock, harness=harness, manifest=manifest, store=store)
        for key in ("delta_held_in", "delta_held_out"):
            if abs(float(es.get(key) or 0.0) - bound[key]) > 1e-9:
                raise ValueError(f"the release's {key} does not follow from its eval runs")
    prospective = manifest.model_copy(deep=True, update={"version": to_version})
    prospective.release = {"channel": channel.value, "lockfile_hash": lock.lockfile_hash}
    payload = release_payload(manifest.agent, manifest.version, to_version, channel.value,
                              lock.lockfile_hash, es.get("delta_held_in"),
                              es.get("delta_held_out"), rationale, who,
                              object_sha256(prospective.unsigned_payload()),
                              evals_of(es), restores, set_aside or None)
    return who, channel, lock, payload


def release(
    manifest: AgentManifest,
    store: CapabilityStore,
    ledger,
    signer: Signer,
    *,
    to_version: str,
    channel: ReleaseChannel | str,
    approver: str,
    eval_summary: dict | None = None,
    rationale: str = "",
    approval: dict[str, Any] | None = None,
    harness: tuple[list[HarnessComponent], list[str]] | None = None,
    only: set[str] | None = None,
    restores: str | None = None,
    set_aside: list[str] | None = None,
) -> tuple[AgentManifest, CapabilitiesLock, ReleaseRecord]:
    """Produce the next signed harness version.

    Non-shadow channels require the conservative gate to pass on the deltas
    in ``eval_summary`` (see ``prepare_release`` for measured, attested and
    rollback evidence). The signature covers the manifest, including the
    digest of the new capabilities.lock. A signed approval must match the
    lock, the manifest and the evidence exactly: if any changed after it was
    signed, the release is refused. ``harness`` (from
    ``brevet.harness.inventory``) locks the harness components as well."""
    eval_summary = dict(eval_summary) if eval_summary is not None else {}
    who, channel, lock, expected = prepare_release(
        manifest, store, to_version=to_version, channel=channel, approver=approver,
        eval_summary=eval_summary, rationale=rationale, harness=harness, ledger=ledger,
        only=only, restores=restores, set_aside=set_aside)
    approval = enforce(ledger, expected, approval)

    from_version = manifest.version
    manifest.version = to_version
    manifest.release = {"channel": channel.value, "lockfile_hash": lock.lockfile_hash}

    payload = manifest.unsigned_payload()
    public_key = signer.public_key_hex()
    manifest.signature = {
        "content_hash": object_sha256(payload),
        "algorithm": "ed25519",
        "signed_by": who,
        "signed_at": _now(),
        "public_key": public_key,
        "signature": signer.sign(payload),
    }
    record = ReleaseRecord(
        agent=manifest.agent,
        from_version=from_version,
        to_version=to_version,
        channel=channel,
        promoted_capabilities=[r.capability_id for r in lock.resolved],
        manifest_hash=manifest.signature["content_hash"],
        lockfile_hash=lock.lockfile_hash,
        eval_summary=eval_summary or {},
        approved_by=who,
        rollback_to=from_version,
        rationale=rationale,
        signer_public_key=public_key,
        approval=approval,
        restores=restores,
        set_aside=set_aside or None,
    )
    ledger.append("brevet.release", _body(record))
    return manifest, lock, record


def verify_manifest_signature(manifest: AgentManifest, public_key: str | None = None) -> bool:
    """True if the manifest carries a signature that verifies against
    ``public_key`` (default: the key recorded in the signature)."""
    sig = manifest.signature or {}
    key = public_key or sig.get("public_key")
    if not key or not sig.get("signature"):
        return False
    return Signer.verify(key, manifest.unsigned_payload(), sig["signature"])


def prepare_recall(
    store: CapabilityStore,
    capability_id: str,
    *,
    reason: str,
    reason_class: str,
    severity: str,
    issued_by: str,
    action: str = "rollback",
) -> tuple[CapabilityObject, str, dict[str, Any]]:
    """Validate a recall and return the capability, the issuer and the
    payload an approver signs."""
    who = require_identity(issued_by, role="recall issuer")
    if reason_class not in RECALL_REASONS:
        raise ValueError(f"reason_class must be one of {', '.join(RECALL_REASONS)}")
    if severity not in RECALL_SEVERITIES:
        raise ValueError(f"severity must be one of {', '.join(RECALL_SEVERITIES)}")
    if action not in RECALL_ACTIONS:
        raise ValueError(f"action must be one of {', '.join(RECALL_ACTIONS)}")
    if not (reason or "").strip():
        raise ValueError("a recall needs a reason")
    caps = store.all()
    if capability_id not in caps:
        raise KeyError(f"unknown capability '{capability_id}'")
    cap = caps[capability_id]
    if cap.revocation_status == RevocationStatus.withdrawn:
        raise ValueError(f"capability '{capability_id}' has already been recalled")
    if reason_class == "duplicate" and not any(
            c.capability_id != capability_id and c.content_hash == cap.content_hash
            and c.revocation_status != RevocationStatus.withdrawn for c in caps.values()):
        raise ValueError(f"no other capability holds the content of '{capability_id}', so "
                         f"it is not a duplicate; recall it with another reason")
    payload = recall_payload(capability_id, cap.content_hash, reason, reason_class,
                             severity, action, who)
    return cap, who, payload


def recall(
    store: CapabilityStore,
    ledger,
    capability_id: str,
    *,
    reason: str,
    reason_class: str,
    severity: str,
    issued_by: str,
    releases: list[ReleaseRecord],
    action: str = "rollback",
    approval: dict[str, Any] | None = None,
) -> RecallNotice:
    """Withdraw one capability, flag every release whose lockfile contains
    it, and record the recall notice on the evidence chain."""
    cap, who, expected = prepare_recall(
        store, capability_id, reason=reason, reason_class=reason_class,
        severity=severity, issued_by=issued_by, action=action)
    approval = enforce(ledger, expected, approval)
    cap.revocation_status = RevocationStatus.withdrawn
    cap.validation_state = ValidationState.withdrawn
    cap.updated_at = _now()
    cap.lineage.append(f"recall:{who}")
    store.add(cap)

    affected = [
        {"agent": r.agent, "version": r.to_version,
         "resolved_action": action, "resolved_at": _now()}
        for r in releases
        if capability_id in r.promoted_capabilities
    ]
    notice = RecallNotice(
        capability_id=capability_id,
        content_hash=cap.content_hash,
        reason=reason,
        reason_class=reason_class,
        severity=severity,
        issued_by=who,
        action=action,
        affected_releases=affected,
        completed_at=_now(),
        approval=approval,
    )
    ledger.append("brevet.recall", _body(notice), refs=[capability_id])
    return notice
