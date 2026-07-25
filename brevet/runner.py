"""Eval execution.

Runs the compiled regression suite (eval-case capability objects) against the
live wrapped agent. Cases are split deterministically into held-in and
held-out halves (by content hash), the agent replays each stored task, and a
scorer compares its output to the human-decided expectation.

Two runs make a gate decision: ``compare(before, after)`` yields the deltas
the conservative gate consumes. Machines check regressions; humans decide
meaning. Every run is recorded as a ``brevet.eval_run`` envelope.
"""

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
    def __init__(self, agent: Any, *, scorer: Scorer | None = None):
        self.agent = agent
        self.scorer = scorer or decision_scorer

    def _cases(self) -> list[dict[str, Any]]:
        tasks = {e["envelope_id"]: e["body"] for e in self.agent.ledger.read("brevet.task")}
        cases = []
        for cap in self.agent.store.all().values():
            if cap.kind != CapabilityKind.eval_case:
                continue
            if cap.revocation_status.value != "active":
                continue
            payload = json.loads(cap.content)
            task_body = tasks.get(payload.get("task_id"), {})
            task_text = task_body.get("task")
            if not task_text:
                continue
            cases.append({
                "capability_id": cap.capability_id,
                "task": task_text,
                "task_family": payload.get("task_family"),
                "expected": payload.get("expected_final", ""),
                "split": "held_out" if int(cap.content_hash[-1], 16) % 2 else "held_in",
            })
        return cases

    def run(self) -> dict[str, Any]:
        cases = self._cases()
        results = []
        for case in cases:
            out = self.agent.run(case["task"], task_family=case["task_family"]).output
            results.append({**case, "passed": self.scorer(out, case["expected"])})

        def rate(split: str) -> float:
            xs = [r for r in results if r["split"] == split]
            return (sum(r["passed"] for r in xs) / len(xs)) if xs else 0.0

        summary = {
            "n_cases": len(results),
            "held_in_pass_rate": rate("held_in"),
            "held_out_pass_rate": rate("held_out"),
            "failures": [r["capability_id"] for r in results if not r["passed"]],
        }
        summary["run_ref"] = self.agent.ledger.append("brevet.eval_run", summary)
        return summary

    @staticmethod
    def compare(before: dict[str, Any], after: dict[str, Any]) -> dict[str, Any]:
        d_in = after["held_in_pass_rate"] - before["held_in_pass_rate"]
        d_out = after["held_out_pass_rate"] - before["held_out_pass_rate"]
        return {"delta_held_in": round(d_in, 6), "delta_held_out": round(d_out, 6),
                "passed_gate": conservative_gate(d_in, d_out)}
