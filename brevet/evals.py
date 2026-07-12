"""Override-compiled evals + the conservative acceptance gate.

This is the verifier-gap fix: regulated work has no benchmark oracle, but
every substituting override is a labelled failure with a human-decided
expected outcome, and every refining override is a labelled style/retrieval
miss. The regression suite is therefore *compiled from the Decision Ledger*
rather than commissioned. Eval cases are themselves capability objects
(kind=eval_case), so the test suite has provenance too.

The gate is Self-Harness's conservative acceptance rule:
    delta_held_in >= 0  AND  delta_held_out >= 0  AND  max(deltas) > 0
i.e. a candidate may not trade one split against the other, even if the
total improves. Machines check regressions; humans decide meaning.
"""

from __future__ import annotations

import json

from brevet.models import (
    ApplicabilityContext,
    CapabilityEvidence,
    CapabilityKind,
    CapabilityObject,
    OverrideRecord,
    Provenance,
    SourcePathway,
)


def compile_eval_case(override: OverrideRecord) -> CapabilityObject:
    payload = {
        "case_type": "regression" if not override.intent_preserved else "rubric",
        "task_id": override.task_id,
        "task_family": override.task_family,
        "given_draft": override.draft,
        "expected_final": override.final,
        "human_rationale": override.rationale,
        "tags": override.tags,
    }
    return CapabilityObject(
        title=f"eval[{payload['case_type']}] from {override.override_id}",
        kind=CapabilityKind.eval_case,
        content=json.dumps(payload, sort_keys=True, ensure_ascii=False),
        payload_schema="brevet/eval_case_payload",
        source_pathway=SourcePathway.exogenous,  # the label is a human judgment
        provenance=Provenance(
            source_overrides=[override.override_id],
            capture_method="eval_compiler/override",
            originating_participant=override.participant,
        ),
        conditions=ApplicabilityContext(task_family=override.task_family),
        evidence=CapabilityEvidence(
            recurrence_count=1,
            supporting_overrides=[override.override_id],
            evidence_strength="moderate",
        ),
        confidence=0.8,
    ).seal()


def compile_suite(overrides: list[OverrideRecord]) -> list[CapabilityObject]:
    return [compile_eval_case(o) for o in overrides]


def conservative_gate(delta_held_in: float, delta_held_out: float) -> bool:
    return delta_held_in >= 0 and delta_held_out >= 0 and max(delta_held_in, delta_held_out) > 0
