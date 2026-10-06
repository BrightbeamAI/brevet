"""The dawn gate, releases and recall.

This module holds the capability store and the three stages that change
what an agent may use: the dawn gate, releases and recall.

This is the circadian contract: a conforming deployment runs exactly one
signed release and changes it only between releases, and what the next
release contains is a human decision taken at the dawn gate. The generic wrapper
records versions but does not by itself stop a host framework from
persisting other changes; see the paper's implementation table.

Nothing in this module can promote a capability by itself. Every decision
names the identity that took it, and only ``human:<who>`` and
``mission_group:<name>`` identities are accepted: ``agent:``, ``model:``,
``dream:`` and every other namespace are refused, so a candidate cannot be
promoted under a machine identity (the same rule Metis applies to tacit
fragments). Promotion to Controlled needs a mission group. The check is on
the identity supplied; proving who supplied it is the deployment's job."""

from __future__ import annotations

import json
import re
from pathlib import Path

from brevet.canonical import Signer, content_sha256, object_sha256
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
from brevet.workdir import file_lock

DAWN_OUTCOMES = ("promote", "hold", "reject", "re_elicit")
RECALL_REASONS = ("incorrect", "unsafe", "consent_withdrawn", "superseded", "stale",
                  "compliance", "other")
RECALL_SEVERITIES = ("low", "medium", "high", "critical")
RECALL_ACTIONS = ("quarantine", "rollback", "re_review")

_IDENTITY = re.compile(r"^(human|mission_group):[^\s:]\S*$")
_VERSION = re.compile(r"^(\d+)\.(\d+)\.(\d+)$")


def require_identity(identity: str | None, *, role: str = "approver",
                     mission_group: bool = False) -> str:
    """Return the identity, stripped, if it names a human or a mission group.

    Refuses empty identities, machine namespaces (agent:, model:, dream:)
    and anything not written ``human:<who>`` or ``mission_group:<name>``.
    With ``mission_group=True`` only a mission group is accepted."""
    who = (identity or "").strip()
    if not _IDENTITY.match(who):
        raise PermissionError(
            f"the {role} must be a human or a mission group, written human:<who> or "
            f"mission_group:<name>; got {identity!r}. Machine identities such as "
            f"agent:, model: and dream: cannot take this decision.")
    if mission_group and not who.startswith("mission_group:"):
        raise PermissionError(
            f"the {role} must be a mission group (mission_group:<name>) for this "
            f"decision; got {who!r}")
    return who


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


def dawn_decide(
    store: CapabilityStore,
    ledger,
    capability_id: str,
    outcome: str,
    *,
    approver: str,
    to_layer: AuthorityLayer | str = AuthorityLayer.advisory,
    notes: str = "",
) -> CapabilityObject:
    """Apply one dawn-gate decision. Rejection never means deletion: a
    rejected capability remains governed evidence of what was considered."""
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
    ledger.append(
        "brevet.promotion",
        {"capability_id": capability_id, "outcome": outcome, "approver": who,
         "to_layer": layer.value if outcome == "promote" else None, "notes": notes},
        refs=[capability_id],
    )
    return cap


def build_lock(manifest: AgentManifest, store: CapabilityStore) -> CapabilitiesLock:
    """Resolve the releasable capabilities into a lock.

    Only promoted, active capabilities qualify. A capability whose content
    matches a recalled one is left out, so recalled content cannot return
    under a new identifier. A capability whose stored content no longer
    matches its recorded hash stops the release."""
    lock = CapabilitiesLock(agent=manifest.agent, agent_version=manifest.version)
    caps = list(store.all().values())
    recalled = {c.content_hash for c in caps
                if c.revocation_status == RevocationStatus.withdrawn and c.content_hash}
    for cap in caps:
        if not cap.releasable:
            continue  # evidence-layer and revoked material is never locked in
        if content_sha256(cap.content) != cap.content_hash:
            raise ValueError(f"capability '{cap.capability_id}' no longer matches its "
                             f"recorded content hash; the capability store was edited")
        if cap.content_hash in recalled:
            continue
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
    channel: ReleaseChannel | str,
    approver: str,
    eval_summary: dict | None = None,
    rationale: str = "",
) -> tuple[AgentManifest, CapabilitiesLock, ReleaseRecord]:
    """Produce the next signed harness version.

    Non-shadow channels require the conservative gate to pass on the deltas
    in ``eval_summary``, which the caller supplies. The signature covers the
    manifest, including the digest of the new capabilities.lock."""
    who = require_identity(approver, role="release approver")
    channel = ReleaseChannel(channel)
    target = _version_tuple(to_version)
    if target is None:
        raise ValueError(f"to_version must be written like 1.2.3; got {to_version!r}")
    current = _version_tuple(manifest.version)
    if current is not None and target <= current:
        raise ValueError(f"to_version {to_version} must be higher than the current "
                         f"version {manifest.version}")
    if channel != ReleaseChannel.shadow:
        es = eval_summary or {}
        if not conservative_gate(es.get("delta_held_in", -1), es.get("delta_held_out", -1)):
            raise ValueError(
                "release blocked: conservative gate not passed "
                "(need delta_in >= 0, delta_out >= 0, max > 0)"
            )

    lock = build_lock(manifest.model_copy(update={"version": to_version}), store)
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
    )
    ledger.append("brevet.release", record.model_dump())
    return manifest, lock, record


def verify_manifest_signature(manifest: AgentManifest, public_key: str | None = None) -> bool:
    """True if the manifest carries a signature that verifies against
    ``public_key`` (default: the key recorded in the signature)."""
    sig = manifest.signature or {}
    key = public_key or sig.get("public_key")
    if not key or not sig.get("signature"):
        return False
    return Signer.verify(key, manifest.unsigned_payload(), sig["signature"])


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
    """Withdraw one capability, flag every release whose lockfile contains
    it, and record the recall notice on the evidence chain."""
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
    )
    ledger.append("brevet.recall", notice.model_dump(), refs=[capability_id])
    return notice
