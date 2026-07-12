"""End-to-end demonstration of the few-lines API on synthetic data.

A deviation-triage agent drafts classifications; a quality reviewer overrides
some (same recurring reason); `dream()` mines the overrides; the dawn gate
promotes; `evaluate()` measures before/after; a signed release locks the
capability in; `recall()` withdraws it; the evidence chain verifies.

No model, no network, no GPU: the "agent" is a deterministic stub, because
the demo shows the governance loop, not model quality.
"""

from __future__ import annotations

from pathlib import Path
from typing import Callable

import yaml

import brevet
from brevet.models import AgentManifest
from brevet.runner import EvalRunner

REVIEWER = "human:quality.owner@example.com"
MISSION_GROUP = "mission_group:right_first_time"

CASES = [
    # (deviation, human_final_severity)
    ("DEV-4021 pump P-301 vibration high during CIP", "major"),
    ("DEV-4022 filter skid pressure drop above limit", "minor"),
    ("DEV-4023 pump P-114 vibration high during CIP", "major"),
    ("DEV-4024 label misprint on batch 88213", "minor"),
    ("DEV-4025 pump P-301 vibration high during ramp-up", "major"),
    ("DEV-4026 pump P-207 vibration high during CIP", "major"),
]


def _naive_stub(task: str, context: dict) -> str:
    return (f"Deviation triage draft\nitem: {task}\nseverity: minor\n"
            f"action: log and monitor")


def _evolved_stub(task: str, context: dict) -> str:
    # Simulates the promoted rule applied to the harness: vibration -> major.
    sev = "major" if "vibration" in task else "minor"
    action = "escalate to quality owner" if sev == "major" else "log and monitor"
    return f"Deviation triage draft\nitem: {task}\nseverity: {sev}\naction: {action}"


def _human_final(task: str, sev: str) -> str:
    action = "escalate to quality owner" if sev == "major" else "log and monitor"
    return f"Deviation triage draft\nitem: {task}\nseverity: {sev}\naction: {action}"


def run_demo(directory: Path, echo: Callable[[str], None] = print) -> dict:
    directory.mkdir(parents=True, exist_ok=True)
    if (directory / ".brevet" / "ledger.jsonl").exists():
        echo(f"demo: '{directory}' already holds a completed run "
             f"(the evidence chain is append-only, so the demo will not "
             f"overwrite it). Pass a fresh directory: brevet demo <new_dir>")
        raise SystemExit(1)

    manifest = AgentManifest(
        agent="deviation_triage_assistant",
        version="0.1.0",
        description="Demo: drafts deviation severity classifications (suggest-class only).",
        identity_policy={"agent_id": "deviation_triage_assistant", "owner": REVIEWER,
                         "mission_group": MISSION_GROUP,
                         "may": ["draft_triage"], "may_not": ["close_deviation"]},
        prompt_architecture={"system_prompt": "Classify deviation severity; draft only."},
        cognitive_core={"model_policy": {"local_default": "ollama:gemma4:12b",
                                         "allowed_remote": []}},
        bindings={"tools": {"read": ["deviation_log.search"], "suggest": ["draft_triage"],
                            "act": [], "controlled_act": []},
                  "capabilities_lock": "capabilities.lock"},
        runtime_safety={"loop": {"policy": "react", "max_iterations": 4},
                        "evidence": {"ledger": "file:./ledger.jsonl",
                                     "capture_overrides": True},
                        "evals": {"gate": "conservative"},
                        "dream": {"enabled": True}},
    )

    # [1] wrap + work: overrides are a side effect of Tuesday -----------------
    agent = brevet.wrap(_naive_stub, manifest=manifest, workdir=directory / ".brevet")
    agent.manifest_path = directory / "agent.yaml"
    n_overrides = 0
    for task, final_sev in CASES:
        r = agent.run(task, task_family="deviation_triage")
        ovr = agent.record_final(
            r.task_id, _human_final(task, final_sev), participant=REVIEWER,
            rationale=("Recurrent vibration on CIP-adjacent duty is a known "
                       "seal-wear precursor; treat as major." if final_sev == "major" else ""),
            tags=(["vibration-cip-underrated"] if final_sev == "major" else []))
        n_overrides += 1 if ovr else 0
    echo(f"[1] wrapped '{agent.adapter.name}' agent; ran {len(CASES)} tasks; "
         f"harvested {n_overrides} overrides")

    # [2] dream ----------------------------------------------------------------
    summary = agent.dream()
    echo(f"[2] dream: {summary['candidates']} candidate(s), {summary['eval_cases']} "
         f"eval case(s): all Evidence layer, zero authority")

    # [3] dawn -----------------------------------------------------------------
    pending = agent.dawn()
    cand_id = next(c.capability_id for c in pending
                   if c.kind.value == "prompt_rule")
    for cap in pending:
        agent.dawn(decide=(cap.capability_id, "promote"), approver=MISSION_GROUP)
    echo(f"[3] dawn: {len(pending)} promoted to advisory by {MISSION_GROUP}")

    # [4] evaluate before/after: the gate takes measured deltas -----------------
    before = agent.evaluate()
    agent.adapter.target = _evolved_stub  # simulate the promoted rule applied
    after = agent.evaluate()
    gate = EvalRunner.compare(before, after)
    echo(f"[4] evals: held-in {before['held_in_pass_rate']:.2f}->"
         f"{after['held_in_pass_rate']:.2f}, held-out {before['held_out_pass_rate']:.2f}->"
         f"{after['held_out_pass_rate']:.2f}, gate "
         f"{'PASS' if gate['passed_gate'] else 'FAIL'}")

    # [5] signed release ---------------------------------------------------------
    record = agent.release(to_version="0.2.0", channel="trial", approver=MISSION_GROUP,
                           delta_in=gate["delta_held_in"], delta_out=gate["delta_held_out"],
                           rationale="First evolved release: vibration/CIP severity rule.")
    lock_n = len(record.promoted_capabilities)
    echo(f"[5] release: 0.1.0 -> 0.2.0 [trial], {lock_n} capabilities locked, "
         f"manifest signed (ed25519)")

    # [6] recall ------------------------------------------------------------------
    notice = agent.recall(cand_id,
                          reason="Engineering confirmed vibration signature was a sensor "
                                 "artefact on the P-301 family; rule over-generalises.",
                          issued_by=MISSION_GROUP)
    echo(f"[6] recall: {cand_id} withdrawn; {len(notice.affected_releases)} release(s) "
         f"flagged for rollback")

    # [7] verify --------------------------------------------------------------------
    ok, n = agent.verify()
    echo(f"[7] verify: chain {'OK' if ok else 'BROKEN'} across {n} envelopes")
    echo("")
    echo("The loop, once around: work -> evidence -> dream -> dawn -> evals -> "
         "signed release -> recall -> replayable proof.")

    (directory / "agent.yaml").write_text(yaml.safe_dump(
        agent.manifest.model_dump(exclude_none=False), sort_keys=False))
    return {"overrides": n_overrides, "candidates": summary["candidates"],
            "eval_cases": summary["eval_cases"], "gate_passed": gate["passed_gate"],
            "locked": lock_n, "recalled": 1, "chain_ok": ok, "envelopes": n}
