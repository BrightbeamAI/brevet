"""Pydantic models mirroring the JSON Schemas in ``schemas/``.

Enum values are shared verbatim with Metis (authority layers, validation
states, revocation states, source pathways) so capability objects and tacit
fragments interoperate without translation.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from enum import Enum
from typing import Any

from pydantic import BaseModel, Field


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:12]}"


# ---------------------------------------------------------------- enums

class CapabilityKind(str, Enum):
    prompt_rule = "prompt_rule"
    loop_policy = "loop_policy"
    tool_binding = "tool_binding"
    skill = "skill"
    eval_case = "eval_case"
    escalation_rule = "escalation_rule"
    memory_fragment = "memory_fragment"


class SourcePathway(str, Enum):
    exogenous = "exogenous"
    endogenous = "endogenous"


class AuthorityLayer(str, Enum):
    evidence = "evidence"
    advisory = "advisory"
    controlled = "controlled"


class ValidationState(str, Enum):
    captured = "captured"
    worker_confirmed = "worker_confirmed"
    tier1_confirmed = "tier1_confirmed"
    tier2_pending = "tier2_pending"
    promoted_to_advisory = "promoted_to_advisory"
    promoted_to_controlled = "promoted_to_controlled"
    held = "held"
    rejected = "rejected"
    re_elicit = "re_elicit"
    withdrawn = "withdrawn"
    superseded = "superseded"
    expired = "expired"


class RevocationStatus(str, Enum):
    active = "active"
    withdrawn = "withdrawn"
    superseded = "superseded"
    rejected = "rejected"
    under_re_elicitation = "under_re_elicitation"
    retired = "retired"


class ReleaseChannel(str, Enum):
    shadow = "shadow"
    trial = "trial"
    production = "production"


class EvidenceStrength(str, Enum):
    none = "none"
    weak = "weak"
    moderate = "moderate"
    strong = "strong"


# ------------------------------------------------------ capability object

class Provenance(BaseModel):
    mined_by: str | None = None
    originating_participant: str | None = None
    source_traces: list[str] = Field(default_factory=list)
    source_overrides: list[str] = Field(default_factory=list)
    source_artefacts: list[str] = Field(default_factory=list)
    specified_refs: list[str] = Field(default_factory=list)
    capture_method: str | None = None
    timestamp: str | None = None
    human_confirmed_by: str | None = None
    mission_group_reviewed_by: str | None = None
    model_provider: str | None = None
    model_name: str | None = None
    model_prompt_template: str | None = None
    model_output_status: str | None = None
    model_assist_refs: list[str] = Field(default_factory=list)


class ApplicabilityContext(BaseModel):
    task_family: str | None = None
    domain: str | None = None
    model_family: str | None = None
    tool_scope: list[str] = Field(default_factory=list)
    role: str | None = None
    risk_class: str | None = None
    operating_mode: str | None = None
    environment: str | None = None
    trigger_context: str | None = None
    exclusion_conditions: list[str] = Field(default_factory=list)
    valid_from: str | None = None
    valid_until: str | None = None

    def matches(self, runtime: ApplicabilityContext) -> bool:
        """Conditions-first matching: every condition set here must be
        satisfied by the runtime context. Unset fields are unconstrained.
        Exclusions veto. This runs BEFORE any similarity ranking."""
        for field in ("task_family", "domain", "model_family", "role",
                      "operating_mode", "environment", "trigger_context"):
            want = getattr(self, field)
            have = getattr(runtime, field)
            if want is not None and want != have:
                return False
        if self.risk_class is not None and self.risk_class != runtime.risk_class:
            return False
        for excl in self.exclusion_conditions:
            if excl and excl in (runtime.trigger_context or ""):
                return False
        return True


class EvalResult(BaseModel):
    suite: str
    delta_held_in: float
    delta_held_out: float
    repeats: int = 1
    passed_gate: bool = False
    run_ref: str | None = None


class CapabilityEvidence(BaseModel):
    recurrence_count: int = 0
    supporting_traces: list[str] = Field(default_factory=list)
    supporting_overrides: list[str] = Field(default_factory=list)
    counterexamples: list[str] = Field(default_factory=list)
    eval_results: list[EvalResult] = Field(default_factory=list)
    comparison_baseline: str | None = None
    uncertainty: str | None = None
    review_notes: str | None = None
    evidence_strength: EvidenceStrength = EvidenceStrength.none


class CapabilityObject(BaseModel):
    """The unit of learned capability: the Metis fragment tuple + ``kind``."""

    capability_id: str = Field(default_factory=lambda: new_id("cap"))
    title: str
    kind: CapabilityKind
    content: str = ""
    content_hash: str = ""
    payload_schema: str | None = None
    fragment_ref: str | None = None
    source_pathway: SourcePathway = SourcePathway.endogenous
    provenance: Provenance = Field(default_factory=Provenance)
    conditions: ApplicabilityContext = Field(default_factory=ApplicabilityContext)
    evidence: CapabilityEvidence = Field(default_factory=CapabilityEvidence)
    confidence: float = 0.0
    authority_layer: AuthorityLayer = AuthorityLayer.evidence
    validation_state: ValidationState = ValidationState.captured
    revocation_status: RevocationStatus = RevocationStatus.active
    consent: dict[str, Any] | None = None
    lineage: list[str] = Field(default_factory=list)
    policy_refs: list[str] = Field(default_factory=list)
    use_constraints: list[str] = Field(default_factory=list)
    created_at: str = Field(default_factory=_now)
    updated_at: str = Field(default_factory=_now)
    review_due_at: str | None = None
    expiry_triggers: list[str] = Field(default_factory=list)

    def seal(self) -> CapabilityObject:
        from brevet.canonical import content_sha256
        self.content_hash = content_sha256(self.content)
        return self

    @property
    def releasable(self) -> bool:
        return (
            self.authority_layer in (AuthorityLayer.advisory, AuthorityLayer.controlled)
            and self.revocation_status == RevocationStatus.active
            and self.validation_state
            in (ValidationState.promoted_to_advisory, ValidationState.promoted_to_controlled)
        )


# ------------------------------------------------------------- overrides

class OverrideRecord(BaseModel):
    """A human judgment over an agent output, CHAP ``decide.override`` shaped.

    ``intent_preserved=True`` is a *refining* override (agreed with the
    decision, changed its expression); ``False`` is a *substituting* override
    (reached a different decision). They are different failure modes and the
    delta engine treats them as soft and hard labels respectively.
    """

    override_id: str = Field(default_factory=lambda: new_id("ovr"))
    task_id: str
    trace_ref: str | None = None
    participant: str = "human:unknown"
    intent_preserved: bool = True
    diff: list[dict[str, Any]] = Field(default_factory=list)
    draft: str | None = None
    final: str | None = None
    rationale: str = ""
    tags: list[str] = Field(default_factory=list)
    task_family: str | None = None
    created_at: str = Field(default_factory=_now)


# ------------------------------------------------------- manifest & lock

class AgentManifest(BaseModel):
    agent: str
    version: str = "0.1.0"
    description: str = ""
    identity_policy: dict[str, Any] = Field(default_factory=dict)
    prompt_architecture: dict[str, Any] = Field(default_factory=dict)
    cognitive_core: dict[str, Any] = Field(default_factory=dict)
    bindings: dict[str, Any] = Field(default_factory=dict)
    runtime_safety: dict[str, Any] = Field(default_factory=dict)
    release: dict[str, Any] = Field(default_factory=lambda: {"channel": "shadow"})
    signature: dict[str, Any] | None = None

    def unsigned_payload(self) -> dict[str, Any]:
        d = self.model_dump(exclude_none=False)
        d.pop("signature", None)
        return d


class LockedCapability(BaseModel):
    capability_id: str
    kind: str
    content_hash: str
    authority_layer: str
    conditions_digest: str | None = None
    approved_by: str
    approved_at: str
    promotion_ref: str | None = None
    evidence_refs: list[str] = Field(default_factory=list)
    revocation_status: str = "active"


class CapabilitiesLock(BaseModel):
    agent: str
    agent_version: str
    generated_at: str = Field(default_factory=_now)
    lockfile_hash: str = ""
    resolved: list[LockedCapability] = Field(default_factory=list)


# ---------------------------------------------------- release and recall

class ReleaseRecord(BaseModel):
    release_id: str = Field(default_factory=lambda: new_id("rel"))
    agent: str
    from_version: str | None = None
    to_version: str
    channel: ReleaseChannel = ReleaseChannel.shadow
    promoted_capabilities: list[str] = Field(default_factory=list)
    removed_capabilities: list[str] = Field(default_factory=list)
    manifest_hash: str = ""
    lockfile_hash: str = ""
    eval_summary: dict[str, Any] = Field(default_factory=dict)
    approved_by: str = ""
    approved_at: str = Field(default_factory=_now)
    decision_ref: str | None = None
    rollback_to: str | None = None
    rationale: str | None = None


class RecallNotice(BaseModel):
    recall_id: str = Field(default_factory=lambda: new_id("rcl"))
    capability_id: str
    content_hash: str | None = None
    reason: str
    reason_class: str = "other"
    severity: str = "medium"
    issued_by: str = ""
    issued_at: str = Field(default_factory=_now)
    action: str = "quarantine"
    affected_releases: list[dict[str, Any]] = Field(default_factory=list)
    control_refs: list[str] = Field(default_factory=list)
    completed_at: str | None = None
