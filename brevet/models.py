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
        """Conditions-first matching, run before any similarity ranking.

        A rule's scope (task family, domain, model family, operating mode) is
        judged where the runtime states it, so a task described only by its
        family is matched on its family. A restriction (role, risk class,
        environment) and a trigger hold only where the runtime establishes
        them: a rule for high-risk work is not served to a task whose risk is
        unknown. The trigger and the exclusions are looked for in the task's
        own text (``runtime.trigger_context``), ignoring case, and any
        exclusion found there vetoes the rule."""
        for field in _SCOPE_FIELDS:
            want, have = getattr(self, field), getattr(runtime, field)
            if want is not None and have is not None and want != have:
                return False
        for field in _RESTRICTION_FIELDS:
            want = getattr(self, field)
            if want is not None and getattr(runtime, field) != want:
                return False
        text = (runtime.trigger_context or "").casefold()
        if self.trigger_context and self.trigger_context.casefold() not in text:
            return False
        return not any(e and e.casefold() in text for e in self.exclusion_conditions)

    @classmethod
    def of_task(cls, task: str | None = None, task_family: str | None = None,
                context: dict[str, Any] | None = None) -> ApplicabilityContext | None:
        """The runtime context of one task: its family, its text (where
        triggers and exclusions are looked for) and the condition fields the
        caller's context supplies (domain, model_family, operating_mode,
        role, risk_class, environment). None when nothing is known."""
        given = {k: str(v) for k, v in (context or {}).items() if k in RUNTIME_FIELDS and v}
        if task_family:
            given["task_family"] = task_family
        if task:
            given["trigger_context"] = task
        return cls(**given) if given else None

    def in_effect(self, at: datetime | None = None) -> bool:
        """False outside the validity window. A bound written as a date
        covers that whole day; a bound that cannot be read means not in
        effect, so a mistyped window withholds the rule rather than serving
        it indefinitely."""
        now = at or datetime.now(timezone.utc)
        for bound, end in ((self.valid_from, False), (self.valid_until, True)):
            if not bound:
                continue
            when = _parse_bound(bound, end_of_day=end)
            if when is None or (now < when if not end else now > when):
                return False
        return True

    def digest(self) -> str:
        """The digest a release locks for these conditions. Fields added
        after 0.1 count only when set, so adding a field to this model later
        does not change the digest of rules already released."""
        from brevet.canonical import object_sha256
        d = self.model_dump()
        return object_sha256({k: v for k, v in d.items()
                              if k in _CONDITION_FIELDS_0_1 or v not in (None, "", [], {})})


def _parse_bound(text: str, *, end_of_day: bool) -> datetime | None:
    try:
        if len(text) == 10:  # a date: the window covers the whole day
            day = datetime.fromisoformat(text).replace(tzinfo=timezone.utc)
            return day.replace(hour=23, minute=59, second=59, microsecond=999999) \
                if end_of_day else day
        when = datetime.fromisoformat(text.replace("Z", "+00:00"))
        return when if when.tzinfo else when.replace(tzinfo=timezone.utc)
    except ValueError:
        return None


_SCOPE_FIELDS = ("task_family", "domain", "model_family", "operating_mode")
_RESTRICTION_FIELDS = ("role", "risk_class", "environment")
RUNTIME_FIELDS = (*_SCOPE_FIELDS, *_RESTRICTION_FIELDS)

_CONDITION_FIELDS_0_1 = frozenset({
    "task_family", "domain", "model_family", "tool_scope", "role", "risk_class",
    "operating_mode", "environment", "trigger_context", "exclusion_conditions",
    "valid_from", "valid_until"})


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
    dream cycle treats them as soft and hard signals respectively.
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


class HarnessComponent(BaseModel):
    """One piece of the harness a release runs with: a file, a component of
    the live agent, or a library version. Only a digest and a short label
    are kept, never the content itself."""

    component_id: str          # e.g. file:prompts/system.md, agent:tool:search, env:langgraph
    kind: str                  # file | agent | env
    digest: str
    detail: str = ""


class CapabilitiesLock(BaseModel):
    agent: str
    agent_version: str
    generated_at: str = Field(default_factory=_now)
    lockfile_hash: str = ""
    resolved: list[LockedCapability] = Field(default_factory=list)
    harness: list[HarnessComponent] = Field(default_factory=list)
    harness_sources: list[str] = Field(default_factory=list)  # files | agent | env


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
    signer_public_key: str = ""
    approval: dict[str, Any] | None = None
    restores: str | None = None   # set when the release rolls back to an earlier one
    set_aside: list[str] | None = None  # harness files a rollback moved aside


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
    approval: dict[str, Any] | None = None
