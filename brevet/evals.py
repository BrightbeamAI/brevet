"""Override-compiled evals and the conservative gate.

Judgement work rarely has a ready-made test set. Overrides fill the gap:
each substituting override becomes an eval case whose expected answer is
the expert's final, and each refining override marks an expression the
agent got wrong. Eval cases are capabilities themselves (kind=eval_case),
so the suite has provenance and can be recalled like anything else.

The conservative gate follows the acceptance rule of Self-Harness:
    delta_held_in >= 0  AND  delta_held_out >= 0  AND  max(deltas) > 0
A change may not improve one half of the tests by making the other half
worse. It is not a guarantee of no regressions: gains and losses can still
cancel out inside one half."""

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
