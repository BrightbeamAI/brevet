"""Running the evals.

Runs the override-compiled eval cases against the wrapped agent. Cases are
sorted by content hash and dealt alternately into held-in and held-out
halves, so both halves have cases whenever there are two or more. The agent
replays each stored task, and a scorer compares its answer with the
expert's final.

Each case runs ``runtime_safety.evals.repeats`` times (default 1), and its
pass rate is the share of repeats that passed. Every run is recorded as a
``brevet.eval_run`` envelope with what it evaluated: the cases, the scorer,
the repeats, and the capability set, harness and manifest it ran with, so a
release can be bound to the runs behind its numbers. ``compare(before,
after)`` returns the deltas the conservative gate needs."""

from __future__ import annotations

import json
from collections.abc import Callable
from typing import Any

from brevet.evals import conservative_gate
from brevet.evidence import _decision_tokens
from brevet.models import CapabilityKind

Scorer = Callable[[str, str], bool]


def decision_scorer(output: str, expected: str) -> bool:
    """Pass iff the decision-bearing tokens agree. Deliberately blunt and
    deterministic; swap in a rubric or judge via EvalRunner(scorer=...)."""
    return _decision_tokens(output) == _decision_tokens(expected)


class EvalRunner:
    def __init__(self, agent: Any, *, scorer: Scorer | None = None,
                 repeats: int | None = None):
        self.agent = agent
        self.scorer = scorer or decision_scorer
        configured = ((getattr(getattr(agent, "manifest", None), "runtime_safety", None) or {})
                      .get("evals") or {}).get("repeats")
        self.repeats = max(1, int(repeats or configured or 1))

    def _cases(self) -> list[dict[str, Any]]:
        tasks = {e["envelope_id"]: e["body"] for e in self.agent.ledger.read("brevet.task")}
        cases = []
        for cap in self.agent.store.all().values():
            if cap.kind != CapabilityKind.eval_case:
                continue
            if cap.revocation_status.value != "active":
                continue
            try:
                payload = json.loads(cap.content)
            except ValueError:
                continue
            task_text = tasks.get(payload.get("task_id"), {}).get("task")
            expected = payload.get("expected_final")
            if not task_text or not expected:
                continue  # nothing to replay, or no expert final to compare with
            cases.append({
                "capability_id": cap.capability_id,
                "task": task_text,
                "task_family": payload.get("task_family"),
                "expected": expected,
                "content_hash": cap.content_hash,
            })
        cases.sort(key=lambda c: c["content_hash"])
        for i, case in enumerate(cases):
            case["split"] = "held_in" if i % 2 == 0 else "held_out"
        return cases

    def run(self, meta: dict[str, Any] | None = None) -> dict[str, Any]:
        from brevet.canonical import object_sha256
        from brevet.harness import _qualname, _source

        cases = self._cases()
        results = []
        run = getattr(self.agent, "_evaluation_run", None) or self.agent.run
        for case in cases:
            passes, tasks = [], []
            for _ in range(self.repeats):
                result = run(case["task"], task_family=case["task_family"])
                passes.append(bool(self.scorer(result.output, case["expected"])))
                tasks.append(getattr(result, "task_id", None))
            rate_ = sum(passes) / len(passes)
            results.append({**case, "pass_rate": rate_, "passed": rate_ >= 0.5,
                            "passes": passes, "tasks": tasks})

        def rate(split: str) -> float:
            xs = [r for r in results if r["split"] == split]
            return (sum(r["pass_rate"] for r in xs) / len(xs)) if xs else 0.0

        summary = {
            **(meta or {}),
            "n_cases": len(results),
            "n_held_in": sum(r["split"] == "held_in" for r in results),
            "n_held_out": sum(r["split"] == "held_out" for r in results),
            "held_in_pass_rate": rate("held_in"),
            "held_out_pass_rate": rate("held_out"),
            "failures": [r["capability_id"] for r in results if not r["passed"]],
            "repeats": self.repeats,
            "cases_digest": object_sha256(sorted([c["capability_id"], c["content_hash"]]
                                                 for c in cases)),
            "scorer": _qualname(self.scorer),
            "scorer_digest": object_sha256(_source(self.scorer)),
            "results": [{"case": r["capability_id"], "split": r["split"],
                         "passes": r["passes"], "tasks": r["tasks"]} for r in results],
        }
        summary["run_ref"] = self.agent.ledger.append("brevet.eval_run", summary)
        return summary

    @staticmethod
    def compare(before: dict[str, Any], after: dict[str, Any]) -> dict[str, Any]:
        d_in = after["held_in_pass_rate"] - before["held_in_pass_rate"]
        d_out = after["held_out_pass_rate"] - before["held_out_pass_rate"]
        return {"delta_held_in": round(d_in, 6), "delta_held_out": round(d_out, 6),
                "passed_gate": conservative_gate(d_in, d_out)}
