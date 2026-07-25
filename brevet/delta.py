"""The delta engine: enacted ⊖ specified.

One difference engine, two consumers. Reading the ledger's traces and
overrides against the current signed harness, it clusters recurring
divergences by failure signature and emits candidate capability objects:

- harness-edit candidates (prompt_rule, loop_policy, escalation_rule, ...)
  -> Self-Harness-style weakness mining, but override-grounded, so it works
     where no benchmark verifier exists;
- memory candidates (memory_fragment refs) -> the endogenous pathway of the
  Tacit Fragments paper, quarantined in the Evidence layer.

The failure signature follows Self-Harness: phi(r) = (cause, causal_status,
mechanism), with the cause vocabulary extended by override labels: a
substituting override is a hard fail, a refining override a soft one.
Candidates carry NO authority: everything lands in the Evidence layer with
validation_state=captured, and nothing here can promote anything.
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Iterable
from dataclasses import dataclass

from brevet.models import (
    ApplicabilityContext,
    CapabilityEvidence,
    CapabilityKind,
    CapabilityObject,
    OverrideRecord,
    Provenance,
    SourcePathway,
    new_id,
)

MIN_RECURRENCE = 3  # endogenous candidates need recurrence before proposal


@dataclass(frozen=True)
class FailureSignature:
    cause: str            # verifier- or override-grounded terminal cause
    causal_status: str    # hard_fail (substituting) | soft_fail (refining)
    mechanism: str        # abstract agent mechanism, from tags or trace shape


def signature_of(override: OverrideRecord) -> FailureSignature:
    mechanism = override.tags[0] if override.tags else "untagged"
    return FailureSignature(
        cause=f"override:{override.task_family}",
        causal_status="soft_fail" if override.intent_preserved else "hard_fail",
        mechanism=mechanism,
    )


def mine(
    overrides: Iterable[OverrideRecord],
    *,
    model_family: str | None = None,
    run_id: str | None = None,
    assist=None,
    ledger=None,
) -> list[CapabilityObject]:
    """Cluster overrides by exact signature agreement (deterministic,
    evaluator-grounded, no latent similarity search) and emit one candidate
    per cluster with recurrence >= MIN_RECURRENCE."""
    run_id = run_id or new_id("dream")
    clusters: dict[FailureSignature, list[OverrideRecord]] = defaultdict(list)
    for ovr in overrides:
        clusters[signature_of(ovr)].append(ovr)

    candidates: list[CapabilityObject] = []
    for sig, members in sorted(
        clusters.items(), key=lambda kv: len(kv[1]), reverse=True
    ):
        if len(members) < MIN_RECURRENCE:
            continue
        rationales = [m.rationale for m in members if m.rationale][:5]
        kind, content, assist_used = _draft_intervention(
            sig, rationales, assist=assist, ledger=ledger)
        cand = CapabilityObject(
            title=f"[candidate] {sig.mechanism}: {sig.causal_status} in {sig.cause}",
            kind=kind,
            content=content,
            source_pathway=SourcePathway.endogenous,
            provenance=Provenance(
                mined_by=run_id,
                source_overrides=[m.override_id for m in members],
                source_traces=[m.trace_ref for m in members if m.trace_ref],
                capture_method="delta_mining/override_clustering",
            ),
            conditions=ApplicabilityContext(
                task_family=members[0].task_family, model_family=model_family
            ),
            evidence=CapabilityEvidence(
                recurrence_count=len(members),
                supporting_overrides=[m.override_id for m in members],
                evidence_strength="weak" if len(members) < 6 else "moderate",
            ),
            confidence=min(0.2 + 0.05 * len(members), 0.6),
        )
        if assist_used:
            cand.provenance.model_provider = assist.provider
            cand.provenance.model_name = assist.model
            cand.provenance.model_output_status = "draft"
        cand.seal()
        candidates.append(cand)
    return candidates


def _draft_intervention(
    sig: FailureSignature, rationales: list[str], *, assist=None, ledger=None
) -> tuple[CapabilityKind, str, bool]:
    """Map a failure signature to a minimal draft intervention.

    Deterministic template by default. If a ModelAssist is provided (local
    Ollama by default, never a requirement), it may word the rule better;
    logged as a brevet.model_assist envelope, output always a draft, and the
    template used as fallback on any failure. Returns (kind, content, assist_used).
    """
    why = ("; ".join(rationales))[:400] or "recurring divergence, no rationale captured"
    if sig.causal_status == "hard_fail":
        template = (
            f"When task_family matches and context involves '{sig.mechanism}', "
            f"the drafted decision recurrently diverged from the human decision. "
            f"Recorded human rationales: {why}. "
            f"Draft rule: surface this consideration explicitly before deciding, "
            f"and if it applies, prefer the human-established resolution."
        )
    else:
        template = (
            f"Refining overrides recur on '{sig.mechanism}' "
            f"(expression corrected, decision kept). Human rationales: {why}. "
            f"Draft rule: adjust output style/structure accordingly."
        )

    if assist is not None and getattr(assist, "provider", "none") != "none":
        from brevet.assist import log_assist
        prompt = (
            "You draft ONE minimal harness rule for an LLM agent. Evidence:\n"
            f"- failure mechanism: {sig.mechanism} ({sig.causal_status})\n"
            f"- human rationales: {why}\n"
            "Reply with a single imperative rule (<=60 words), no preamble. "
            "The rule must only restate what the evidence supports."
        )
        drafted = assist.draft(prompt)
        if ledger is not None:
            log_assist(ledger, assist, purpose="draft_intervention",
                       prompt=prompt, output=drafted, used=bool(drafted))
        if drafted:
            return CapabilityKind.prompt_rule, f"{drafted}\n\n[evidence: {why}]", True

    return CapabilityKind.prompt_rule, template, False


def load_overrides(ledger) -> list[OverrideRecord]:
    return [OverrideRecord(**env["body"]) for env in ledger.read("brevet.override")]
