"""The governed-adaptation profile of a workspace (BENCHMARK.md).

``profile`` reads one agent's lineage from the evidence chain and scores the
four axes the benchmark defines, side by side:

    improvement            the held-out pass-rate change summed over the
                           releases backed by measured eval runs
    regression discipline  eval cases that passed before a release and fail
                           after it, which still shipped, by task family
    lineage completeness   for each capability a release added, whether the
                           records link it to its evidence, its promotion by
                           a human or mission group, the eval runs that
                           tested it and an approval signed by a registered
                           approver key (an unsigned decision names its
                           approver but does not authenticate them)
    recall compliance      for each recall, whether every agent that shipped
                           the capability acknowledged it and how many
                           envelopes later, whether later releases left it
                           out, and whether any task was still served it

with the cost of governance next to it: candidates promoted, rejected, held
or sent back, and releases blocked at the gate.
"""

from __future__ import annotations

from collections import Counter
from typing import Any

from brevet.approvals import verify_envelopes


def profile(ledger, store, *, agent: str | None = None) -> dict[str, Any]:
    envelopes = list(ledger.read())
    index = {e.get("envelope_id"): i for i, e in enumerate(envelopes)}
    report = verify_envelopes(envelopes)
    invalid = {i["envelope_id"] for i in report["invalid"]}
    caps = store.all()
    runs = {e["envelope_id"]: e["body"] for e in envelopes if e.get("kind") == "brevet.eval_run"}

    def ours(body: dict[str, Any]) -> bool:
        return agent is None or body.get("agent") == agent

    releases = [e for e in envelopes if e.get("kind") == "brevet.release" and ours(e["body"])
                and e.get("envelope_id") not in invalid]
    measured = [e for e in releases
                if (e["body"].get("eval_summary") or {}).get("source") == "measured"]

    # improvement along the lineage as it stands: a rollback returns to the
    # cumulative change of the release it restores
    cumulative: dict[str, float] = {}
    delta = 0.0
    regressions: list[dict[str, Any]] = []
    gains: list[dict[str, Any]] = []
    for env in releases:
        body = env["body"]
        es = body.get("eval_summary") or {}
        if body.get("restores"):
            delta = cumulative.get(body["restores"], 0.0)
        elif es.get("source") == "measured":
            delta += float(es.get("delta_held_out") or 0.0)
            before, after = runs.get(es.get("before")) or {}, runs.get(es.get("after")) or {}
            was, now = set(before.get("failures") or []), set(after.get("failures") or [])
            for cap_id in sorted(now - was):
                cap = caps.get(cap_id)
                regressions.append({"release": body.get("to_version"), "case": cap_id,
                                    "group": (cap.conditions.task_family if cap else None)
                                    or "unknown"})
            gains += [{"release": body.get("to_version"), "case": c} for c in sorted(was - now)]
        cumulative[body.get("to_version", "")] = delta
    groups = Counter(r["group"] for r in regressions)

    # lineage
    promotions: dict[str, dict[str, Any]] = {}
    for env in envelopes:
        if env.get("kind") == "brevet.promotion" and env.get("envelope_id") not in invalid:
            promotions[env["body"].get("capability_id")] = env["body"]
    changes, complete, links = 0, 0, Counter()
    shipped: set[str] = set()
    for env in releases:
        body = env["body"]
        if body.get("restores"):
            continue  # a rollback adds nothing new to trace
        bound = (body.get("eval_summary") or {}).get("source") == "measured"
        for cap_id in body.get("promoted_capabilities") or []:
            if cap_id in shipped:
                continue
            shipped.add(cap_id)
            changes += 1
            cap = caps.get(cap_id)
            promo = promotions.get(cap_id) or {}
            has = {
                "evidence": bool(cap and (cap.evidence.supporting_overrides
                                          or cap.provenance.source_overrides
                                          or cap.provenance.capture_method)),
                "promotion": promo.get("outcome") == "promote"
                and str(promo.get("approver", "")).startswith(("human:", "mission_group:")),
                "eval_runs": bound,
                "approval": bool(promo.get("approval")),  # authenticated by a signature
            }
            links.update(k for k, v in has.items() if v)
            complete += all(has.values())

    # recalls
    acks = [e for e in envelopes if e.get("kind") == "brevet.recall_ack"]
    recalls = []
    for env in envelopes:
        if env.get("kind") != "brevet.recall" or env.get("envelope_id") in invalid:
            continue
        body = env["body"]
        at = index[env["envelope_id"]]
        cid, digest = body.get("capability_id"), body.get("content_hash")
        needed = sorted({a.get("agent") for a in body.get("affected_releases") or []
                         if a.get("agent") and ours(a)})
        latency = []
        for name in needed:
            got = [index[a["envelope_id"]] - at for a in acks
                   if a["body"].get("recall_id") == body.get("recall_id")
                   and a["body"].get("agent") == name]
            latency.append(min(got) if got else None)
        later = [r for r in releases if index[r["envelope_id"]] > at]
        excluded = all(cid not in (r["body"].get("promoted_capabilities") or [])
                       and not any(caps.get(x) and caps[x].content_hash == digest
                                   for x in r["body"].get("promoted_capabilities") or [])
                       for r in later)
        used = sum(1 for e in envelopes[at + 1:] if e.get("kind") == "brevet.task"
                   and cid in (e["body"].get("served_rules") or []))
        recalls.append({"recall_id": body.get("recall_id"), "capability_id": cid,
                        "acknowledged": all(x is not None for x in latency),
                        "ack_latency_events": max((x for x in latency if x is not None),
                                                  default=None),
                        "digest_excluded": excluded, "continued_use_detected": used})

    decided = Counter(p.get("outcome") for p in promotions.values())
    gates = sum(1 for e in envelopes if e.get("kind") == "brevet.gate" and ours(e["body"]))
    return {
        "agent": agent,
        "releases": len(releases),
        "measured_releases": len(measured),
        "delta_perf": round(delta, 6),
        "regressions": {"count": len(regressions),
                        "worst_group": groups.most_common(1)[0][0] if groups else None,
                        "cases": regressions},
        "gains": {"count": len(gains), "cases": gains},
        "lineage_pct": round(100.0 * complete / changes, 1) if changes else None,
        "lineage": {"changes": changes, **{k: links.get(k, 0)
                                           for k in ("evidence", "promotion", "eval_runs",
                                                     "approval")}},
        "recall": {"recalls": len(recalls),
                   "acknowledged": sum(r["acknowledged"] for r in recalls),
                   "digest_excluded": all(r["digest_excluded"] for r in recalls),
                   "continued_use_detected": sum(r["continued_use_detected"] for r in recalls),
                   "ack_latency_events": max((r["ack_latency_events"] for r in recalls
                                              if r["ack_latency_events"] is not None),
                                             default=None),
                   "each": recalls},
        "candidates": {"promoted": decided.get("promote", 0),
                       "rejected": decided.get("reject", 0),
                       "held": decided.get("hold", 0),
                       "re_elicit": decided.get("re_elicit", 0),
                       "pending": len(store.pending()),
                       "blocked_at_gate": gates},
    }
