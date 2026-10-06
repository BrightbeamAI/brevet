"""The dream cycle: mining recurring overrides into candidate capabilities.

This module reads the overrides experts recorded against the agent's
drafts and looks for ones that keep recurring. Each recurring group
becomes one candidate capability, with no authority until a human
promotes it at the dawn gate.

How overrides are grouped: two overrides belong together when they share
the same task family, the same override kind and the same first tag. The
kind is *substituting* (the expert reached a different decision; recorded
as ``hard_fail``) or *refining* (same decision, better expressed;
``soft_fail``). This grouping key follows the failure signature of
Self-Harness, phi = (cause, causal_status, mechanism). The paper calls the
comparison between what was actually done and what the signed harness
specified the difference signal, written enacted ⊖ specified.

Nothing here can promote anything: every candidate starts in the Evidence
layer with validation_state=captured.
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
            sig, rationales, assist=assist, ledger=ledger, count=len(members))
        kind_of = "substituting" if sig.causal_status == "hard_fail" else "refining"
        cand = CapabilityObject(
            title=(f"{sig.mechanism}: {len(members)} {kind_of} overrides "
                   f"in {_family_of(sig)}"),
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


def _family_of(sig: FailureSignature) -> str:
    """The task family a signature came from, for human-readable text."""
    return sig.cause.split(":", 1)[1] if sig.cause.startswith("override:") else sig.cause


def _draft_intervention(
    sig: FailureSignature, rationales: list[str], *, assist=None, ledger=None,
    count: int = 0,
) -> tuple[CapabilityKind, str, bool]:
    """Draft a candidate's text from a cluster of overrides.

    Deterministic template by default, written in plain language because a
    human reads it at the dawn gate. If a ModelAssist is provided (local
    Ollama by default, never a requirement), it may word the rule better; the
    call is logged as a brevet.model_assist envelope, its output is always a
    draft, and the template is the fallback on any failure.
    Returns (kind, content, assist_used).
    """
    unique = list(dict.fromkeys(r.strip().rstrip(".") for r in rationales if r.strip()))
    why = "; ".join(unique)[:400] or "no reason was recorded"
    family = _family_of(sig)
    times = f" {count} times" if count else " repeatedly"
    if sig.causal_status == "hard_fail":
        template = (
            f"In {family} work involving '{sig.mechanism}', reviewers changed the "
            f"agent's decision{times}. Their reason: {why}. "
            f"Proposed rule: when this situation applies, raise it explicitly and "
            f"follow the reviewers' decision."
        )
    else:
        template = (
            f"In {family} work involving '{sig.mechanism}', reviewers kept the "
            f"agent's decision but reworded it{times}. Their reason: {why}. "
            f"Proposed rule: change the wording or structure of the output to match."
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
