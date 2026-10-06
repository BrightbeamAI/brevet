"""Recall acknowledgements: proof that running agents stopped using a
recalled capability.

A recall is recorded once, on the evidence chain. Every place an agent
receives its governed rules then confirms, on the same chain, that the
recalled capability no longer reaches the agent:

    wrapped agent   Brevet serves the agent its rules with each task, so a
                    recalled rule is withheld from the next task on, and
                    that task acknowledges it. An agent that does not take
                    its rules from Brevet cannot be shown to have stopped:
                    outside the shadow channel it refuses to run until a
                    release without the capability (a new release or a
                    rollback), and the first task under that release
                    acknowledges it.
    MCP session     ``brevet_active`` lists the recalled rules and tells the
                    assistant to stop applying them; the assistant then calls
                    ``brevet_acknowledge``.

A recall is complete when every agent that shipped the capability has
acknowledged it. ``brevet recalls`` and ``brevet_status`` show what is
still open.
"""

from __future__ import annotations

from typing import Any

from brevet.models import CapabilitiesLock, _now


def recalls_on_chain(ledger, invalid: set[str] | None = None) -> list[dict[str, Any]]:
    """Every valid recall notice on the chain, in order."""
    invalid = invalid or set()
    return [e["body"] for e in ledger.read("brevet.recall")
            if e.get("envelope_id") not in invalid]


def acks_on_chain(ledger) -> list[dict[str, Any]]:
    return [e["body"] for e in ledger.read("brevet.recall_ack")]


def reaches_content(recall: dict[str, Any]) -> bool:
    """Whether a recall withdraws the content itself, wherever it appears,
    or only one copy of it: a recall of a byte-identical duplicate
    (``reason_class: duplicate``) leaves the content to the copy that stays."""
    return recall.get("reason_class") != "duplicate"


def affecting(lock: CapabilitiesLock | None, recalls: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """The recalls that hit a capability in this lock (by id, or by identical
    content unless the recall withdrew one duplicate copy)."""
    if lock is None:
        return []
    ids = {r.capability_id for r in lock.resolved}
    hashes = {r.content_hash for r in lock.resolved}
    return [r for r in recalls
            if r.get("capability_id") in ids
            or (reaches_content(r) and r.get("content_hash") in hashes)]


def acknowledge(ledger, recall: dict[str, Any], *, agent: str, release: str | None,
                serving_point: str, how: str, task_id: str | None = None) -> str:
    """Record that one serving point stopped giving the agent this capability."""
    body = {"recall_id": recall.get("recall_id"), "capability_id": recall.get("capability_id"),
            "content_hash": recall.get("content_hash"), "agent": agent, "release": release,
            "serving_point": serving_point, "how": how, "acknowledged_at": _now()}
    if task_id:
        body["task_id"] = task_id
    return ledger.append("brevet.recall_ack", body,
                         refs=[x for x in (task_id, recall.get("capability_id")) if x])


def is_acknowledged(acks: list[dict[str, Any]], recall_id: str, agent: str,
                    serving_point: str | None = None) -> bool:
    return any(a.get("recall_id") == recall_id and a.get("agent") == agent
               and (serving_point is None or a.get("serving_point") == serving_point)
               for a in acks)


def status(ledger, invalid: set[str] | None = None) -> list[dict[str, Any]]:
    """Each recall with the agents that shipped it, who has acknowledged it,
    and whether it is complete."""
    acks = acks_on_chain(ledger)
    out = []
    for r in recalls_on_chain(ledger, invalid):
        agents = sorted({a.get("agent") for a in r.get("affected_releases") or []
                         if a.get("agent")})
        confirmed = [a for a in acks if a.get("recall_id") == r.get("recall_id")]
        waiting = [name for name in agents
                   if not any(a.get("agent") == name for a in confirmed)]
        out.append({"recall_id": r.get("recall_id"), "capability_id": r.get("capability_id"),
                    "issued_at": r.get("issued_at"), "reason": r.get("reason"),
                    "action": r.get("action"),
                    "affected": [{"agent": a.get("agent"), "version": a.get("version")}
                                 for a in r.get("affected_releases") or []],
                    "acknowledged": [{"agent": a.get("agent"), "release": a.get("release"),
                                      "serving_point": a.get("serving_point"),
                                      "how": a.get("how"), "at": a.get("acknowledged_at")}
                                     for a in confirmed],
                    "waiting_for": waiting, "complete": not waiting})
    return out
