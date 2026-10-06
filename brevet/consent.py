"""Consent: whose judgements the dream cycle may learn from.

The manifest declares a consent scope under ``runtime_safety.dream``:

    consent_scope: consented_sources_only    learn only from the workspace
                                             owner and the identities
                                             listed in ``consented``
    consent_scope: all_recorded              learn from every recorded
                                             override (the default when no
                                             scope is declared)

Either way, a participant who withdraws consent is excluded from every later
dream cycle, and each capability built on their overrides (candidates,
promoted rules and eval cases alike) is recalled with the reason
``consent_withdrawn``. Withdrawal is honoured by recall, never by silent
deletion: the overrides stay on the evidence chain as the record of what
happened, and simply stop counting.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from brevet.identity import require_identity

SCOPES = ("consented_sources_only", "all_recorded")


PLACEHOLDER_OWNERS = {"human:unset@local", "human:owner@example.com"}


def normalise(identity: str | None) -> str:
    """An identity as consent compares it: a bare email counts as human:, and
    case does not matter (emails are compared case-insensitively)."""
    who = (identity or "").strip()
    if who and ":" not in who and "@" in who:
        who = f"human:{who}"
    return who.casefold()


def withdrawn(ledger) -> set[str]:
    """Participants whose consent stands withdrawn on the evidence chain."""
    state: dict[str, bool] = {}
    for env in ledger.read("brevet.consent"):
        body = env.get("body") or {}
        if body.get("participant"):
            state[normalise(body["participant"])] = bool(body.get("withdrawn"))
    return {who for who, gone in state.items() if gone}


def allowed(manifest: Any, ledger) -> Callable[[str], bool]:
    """Whether the dream cycle may learn from one participant's overrides."""
    dream = ((manifest.runtime_safety or {}).get("dream") or {}) if manifest is not None else {}
    scope = dream.get("consent_scope", "all_recorded")
    if scope not in SCOPES:
        raise ValueError(f"runtime_safety.dream.consent_scope must be one of "
                         f"{', '.join(SCOPES)}; got {scope!r}")
    owner = ((manifest.identity_policy or {}).get("owner") if manifest is not None else None)
    listed = {normalise(x) for x in [owner, *(dream.get("consented") or [])] if x}
    listed -= PLACEHOLDER_OWNERS
    if scope == "consented_sources_only" and not listed:
        # a manifest still carrying the starter owner names nobody yet: learn from
        # every recording, as before the scope was enforced, until someone is named
        scope = "all_recorded"
    gone = withdrawn(ledger)

    def ok(participant: str) -> bool:
        who = normalise(participant)
        if who in gone:
            return False
        return scope == "all_recorded" or who in listed or "*" in listed
    return ok


def record_withdrawal(ledger, store, participant: str, *, issued_by: str,
                      reason: str = "") -> dict[str, Any]:
    """Record that a participant withdrew consent and return the capabilities
    built on their overrides, which must now be recalled."""
    from brevet.delta import load_overrides
    from brevet.models import RevocationStatus

    who = normalise(participant)
    if not who:
        raise ValueError("name the participant whose consent is withdrawn")
    by = require_identity(issued_by, role="consent withdrawal recorder")
    ledger.append("brevet.consent", {"participant": who, "withdrawn": True, "issued_by": by,
                                     "reason": reason or "consent withdrawn"})
    theirs = {o.override_id for o in load_overrides(ledger) if normalise(o.participant) == who}
    derived = sorted(c.capability_id for c in store.all().values()
                     if c.revocation_status == RevocationStatus.active
                     and theirs & set(c.provenance.source_overrides))
    return {"participant": who, "overrides": len(theirs), "derived": derived}
