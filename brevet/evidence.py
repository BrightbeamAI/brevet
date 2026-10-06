"""Harvesting overrides.

An expert corrects the agent's draft and uses their own version, the
final. The difference between draft and final, with the expert's reason,
is the override, so if the workflow already keeps both, nobody fills in a
separate form. A line-level diff and a check on decision-bearing words
classify it as *refining* (decision kept, expression changed;
``intent_preserved`` true) or *substituting* (a different decision;
``intent_preserved`` false), using CHAP's field. The dream cycle treats
substituting overrides as hard signals and refining ones as soft."""

from __future__ import annotations

import difflib

from brevet.models import OverrideRecord

# Words that, when introduced/removed between draft and final, usually flip a
# decision rather than rephrase it. Deliberately crude in v0; the classifier
# is a pluggable seam (local model assist may draft, a human may re-tag).
_DECISION_MARKERS = {
    "approve", "approved", "reject", "rejected", "escalate", "escalated",
    "critical", "major", "minor", "pass", "fail", "hold", "release",
    "recall", "close", "reopen", "not", "no", "never", "must",
}


def _decision_tokens(text: str) -> set[str]:
    return {t.strip(".,;:()!?").lower() for t in text.split()} & _DECISION_MARKERS


def harvest_override(
    *,
    task_id: str,
    draft: str,
    final: str,
    participant: str = "human:unknown",
    rationale: str = "",
    tags: list[str] | None = None,
    task_family: str = "default",
) -> OverrideRecord | None:
    """Return an OverrideRecord if final differs from draft, else None."""
    if draft.strip() == final.strip():
        return None

    diff_ops = []
    sm = difflib.SequenceMatcher(a=draft.splitlines(), b=final.splitlines())
    for op, a0, a1, b0, b1 in sm.get_opcodes():
        if op == "equal":
            continue
        diff_ops.append(
            {
                "op": op,
                "draft_lines": draft.splitlines()[a0:a1],
                "final_lines": final.splitlines()[b0:b1],
            }
        )

    # Refining vs substituting: if the set of decision-bearing tokens changed,
    # the human likely reached a different decision.
    intent_preserved = _decision_tokens(draft) == _decision_tokens(final)

    return OverrideRecord(
        task_id=task_id,
        participant=participant,
        intent_preserved=intent_preserved,
        diff=diff_ops,
        draft=draft,
        final=final,
        rationale=rationale,
        tags=tags or [],
        task_family=task_family,
    )
