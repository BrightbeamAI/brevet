"""Dawn queue, releases, and recall.

The circadian contract: the waking agent is immutable (it executes exactly
one signed release); adaptation happens offline; what the agent wakes up
knowing is a human decision, taken at the dawn gate, recorded as evidence.

Nothing in this module can promote a capability by itself. The promotion
functions REQUIRE an approver identity, and endogenous candidates can never
be promoted by the same automated process that proposed them (mirrors the
Metis rule: endogenous fragments never self-promote).
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Optional

from brevet.canonical import Signer, object_sha256
from brevet.evals import conservative_gate
from brevet.models import (
    AgentManifest,
    AuthorityLayer,
    CapabilitiesLock,
    CapabilityObject,
    LockedCapability,
    RecallNotice,
    ReleaseChannel,
    ReleaseRecord,
    RevocationStatus,
    ValidationState,
    _now,
)

DAWN_OUTCOMES = ("promote", "hold", "reject", "re_elicit")


class CapabilityStore:
    """Flat JSONL store of capability objects (one line per version)."""

    def __init__(self, path: Path | str):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def add(self, cap: CapabilityObject) -> None:
        with self.path.open("a") as f:
            f.write(cap.model_dump_json() + "\n")

    def all(self) -> dict[str, CapabilityObject]:
        out: dict[str, CapabilityObject] = {}
        if self.path.exists():
            for line in self.path.read_text().splitlines():
                if line.strip():
                    cap = CapabilityObject(**json.loads(line))
                    out[cap.capability_id] = cap  # last write wins
        return out

    def pending(self) -> list[CapabilityObject]:
        return [
            c for c in self.all().values()
            if c.validation_state in (ValidationState.captured, ValidationState.tier2_pending)
            and c.revocation_status == RevocationStatus.active
        ]


def dawn_decide(
    store: CapabilityStore,
    ledger,
    capability_id: str,
    outcome: str,
    *,
    approver: str,
    to_layer: AuthorityLayer = AuthorityLayer.advisory,
    notes: str = "",
) -> CapabilityObject:
    """Apply one dawn-gate decision. Rejection never means deletion: a
    rejected capability remains governed evidence of what was considered."""
    if outcome not in DAWN_OUTCOMES:
        raise ValueError(f"outcome must be one of {DAWN_OUTCOMES}")
    if not approver or approver.startswith(("agent:", "model:", "dream:")):
        raise PermissionError("dawn decisions require a human/mission-group approver identity")

    caps = store.all()
    if capability_id not in caps:
        raise KeyError(f"unknown capability '{capability_id}'")
    cap = caps[capability_id]

    if outcome == "promote":
        if to_layer == AuthorityLayer.controlled and not cap.provenance.mission_group_reviewed_by:
            cap.provenance.mission_group_reviewed_by = approver
        cap.authority_layer = to_layer
        cap.validation_state = (
            ValidationState.promoted_to_controlled
            if to_layer == AuthorityLayer.controlled
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
    cap.lineage.append(f"dawn:{outcome}:{approver}")
    store.add(cap)
    ledger.append(
        "brevet.promotion",
        {"capability_id": capability_id, "outcome": outcome, "approver": approver,
         "to_layer": to_layer.value if outcome == "promote" else None, "notes": notes},
        refs=[capability_id],
    )
    return cap


def build_lock(manifest: AgentManifest, store: CapabilityStore) -> CapabilitiesLock:
    lock = CapabilitiesLock(agent=manifest.agent, agent_version=manifest.version)
    for cap in store.all().values():
        if not cap.releasable:
            continue  # evidence-layer / revoked material can never be locked in
        approved_by = cap.provenance.mission_group_reviewed_by or cap.provenance.human_confirmed_by
        lock.resolved.append(
            LockedCapability(
                capability_id=cap.capability_id,
                kind=cap.kind.value,
                content_hash=cap.content_hash,
                authority_layer=cap.authority_layer.value,
                conditions_digest=object_sha256(cap.conditions.model_dump()),
                approved_by=approved_by or "unrecorded",
                approved_at=cap.updated_at,
                evidence_refs=cap.evidence.supporting_overrides[:20],
            )
        )
    lock.lockfile_hash = object_sha256([r.model_dump() for r in lock.resolved])
    return lock


def release(
    manifest: AgentManifest,
    store: CapabilityStore,
    ledger,
    signer: Signer,
    *,
    to_version: str,
    channel: ReleaseChannel,
    approver: str,
    eval_summary: Optional[dict] = None,
    rationale: str = "",
) -> tuple[AgentManifest, CapabilitiesLock, ReleaseRecord]:
    """Produce the next signed harness version. Non-shadow channels require a
    passing conservative gate in eval_summary."""
    if channel != ReleaseChannel.shadow:
        es = eval_summary or {}
        if not conservative_gate(es.get("delta_held_in", -1), es.get("delta_held_out", -1)):
            raise ValueError(
                "release blocked: conservative gate not passed "
                "(need delta_in >= 0, delta_out >= 0, max > 0)"
            )
    from_version = manifest.version
    manifest.version = to_version
    manifest.release = {"channel": channel.value}
    lock = build_lock(manifest, store)

    payload = manifest.unsigned_payload()
    manifest.signature = {
        "content_hash": object_sha256(payload),
        "algorithm": "ed25519",
        "signed_by": approver,
        "signed_at": _now(),
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
        approved_by=approver,
        rollback_to=from_version,
        rationale=rationale,
    )
    ledger.append("brevet.release", record.model_dump())
    return manifest, lock, record


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
) -> RecallNotice:
    """Un-learn: revoke one capability, flag every release whose lockfile
    contains it, and record the whole event as chained evidence."""
    caps = store.all()
    if capability_id not in caps:
        raise KeyError(f"unknown capability '{capability_id}'")
    cap = caps[capability_id]
    cap.revocation_status = RevocationStatus.withdrawn
    cap.validation_state = ValidationState.withdrawn
    cap.updated_at = _now()
    cap.lineage.append(f"recall:{issued_by}")
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
        issued_by=issued_by,
        action=action,
        affected_releases=affected,
        completed_at=_now(),
    )
    ledger.append("brevet.recall", notice.model_dump(), refs=[capability_id])
    return notice
