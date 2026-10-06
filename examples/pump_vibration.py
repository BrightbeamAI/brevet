"""A worked example: one capability through the governed evolution loop.

A quality reviewer at a pharmaceutical plant checks an agent's severity
rating for each equipment problem. She keeps overriding the same call:
pump vibration during cleaning is an early sign of seal wear, so it is
major, not minor. This script follows that override through the loop:

    1. work and override   the reviewer corrects the draft and says why
    2. dream               recurring overrides become a candidate rule
    3. dawn                a named mission group promotes the candidate and its
                           eval cases
    4. evals               the overrides replay as tests; the conservative gate
    5. release             0.2.0 ships, signed, with its capabilities.lock
    6. recall              the rule proves wrong and is recalled
    7. verify              the whole evidence chain is replayed

Run it from the repository root:

    python examples/pump_vibration.py

Everything is written to a fresh temporary folder. No model, network or
API key is needed: the agent here is a plain Python function.
"""

import tempfile
import textwrap
from pathlib import Path

import brevet
from brevet.runner import EvalRunner

REVIEWER = "human:qa.reviewer@example.com"
MISSION_GROUP = "mission_group:quality_team"


def triage_agent(report: str) -> str:
    """A stand-in for your real agent: it rates every problem as minor."""
    return "severity: minor"


def triage_agent_v2(report: str) -> str:
    """The agent after the team adds the promoted rule to its instructions."""
    if "vibration" in report and "cleaning" in report:
        return "severity: major"
    return "severity: minor"


REPORTS = [
    ("Pump P-301: vibration high during cleaning", "major"),
    ("Pump P-302: vibration high during cleaning", "major"),
    ("Batch 88213: label misprint", "minor"),
    ("Pump P-301: vibration high during cleaning, night shift", "major"),
    ("Pump P-305: vibration high during cleaning", "major"),
]
REASON = "Vibration during cleaning is an early sign of seal wear."


def main() -> None:
    folder = Path(tempfile.mkdtemp(prefix="brevet-example-"))
    agent = brevet.wrap(triage_agent, workdir=folder / ".brevet")

    # 1. Work and override. The agent drafts; the reviewer corrects the draft
    #    and gives a reason. Each correction is recorded as an override. A
    #    draft she accepts as it is records no override.
    overrides = 0
    for report, severity in REPORTS:
        result = agent.run(report, task_family="equipment_triage")
        override = agent.record_final(
            result.task_id, f"severity: {severity}",
            participant=REVIEWER,
            rationale=REASON if severity == "major" else "",
            tags=["vibration-during-cleaning"] if severity == "major" else [],
        )
        overrides += override is not None
    accepted = len(REPORTS) - overrides
    print(f"1. Work and override: {overrides} overrides recorded from {len(REPORTS)} "
          f"drafts ({accepted} draft{'s' if accepted != 1 else ''} accepted as "
          f"{'it was' if accepted == 1 else 'they were'}).")

    # 2. Dream. The same override four times becomes one candidate rule, plus
    #    eval cases compiled from the overrides. Candidates have no authority.
    agent.dream()
    candidates = agent.dawn()                   # the dawn queue
    rule = next(c for c in candidates if c.kind == "prompt_rule")
    print(f"2. Dream: 1 candidate capability, {rule.authority_layer.value.capitalize()} "
          f"layer (no authority yet):")
    print(textwrap.fill(rule.content, width=86,
                        initial_indent="   ", subsequent_indent="   "))

    # 3. Dawn. A machine identity cannot promote a candidate...
    try:
        agent.dawn(decide=(rule.capability_id, "promote"), approver="dream:nightly")
    except PermissionError:
        print("3. Dawn: rejected an approval from dream:nightly "
              "(machine identities cannot promote).")
    # ...but a named mission group can. It promotes the rule and the eval cases
    # compiled with it, so the release locks the tests it was measured on.
    promoted = agent.dawn(decide=(rule.capability_id, "promote"), approver=MISSION_GROUP)
    cases = [c for c in candidates if c.kind == "eval_case"]
    for case in cases:
        agent.dawn(decide=(case.capability_id, "promote"), approver=MISSION_GROUP)
    print(f"   Dawn: {MISSION_GROUP} promoted the rule and the {len(cases)} eval cases "
          f"compiled with it to {promoted.authority_layer.value.capitalize()}.")

    # 4. Evals. Replay the overrides as tests, before and after the change.
    #    In practice your agent loads the promoted rule into its instructions;
    #    here we swap in a version of the agent that follows it.
    before = agent.evaluate(baseline=True)
    agent.adapter.target = triage_agent_v2
    after = agent.evaluate()
    check = EvalRunner.compare(before, after)

    def passed(run: dict) -> str:
        return f"{run['n_cases'] - len(run['failures'])}/{run['n_cases']}"

    print(f"4. Evals: {passed(before)} passed before, {passed(after)} after. "
          f"Conservative gate: {'pass' if check['passed_gate'] else 'fail'}.")

    # 5. Release. 0.2.0 ships, signed, with its capability bill of materials.
    release = agent.release(
        to_version="0.2.0", channel="trial", approver=MISSION_GROUP,
        evals=(before, after),  # the release is bound to the runs behind its numbers
        rationale="Adds the vibration-during-cleaning rule.",
    )
    n = len(release.promoted_capabilities)
    print(f"5. Release: {release.to_version} signed; capabilities.lock lists {n} "
          f"promoted capabilit{'ies' if n != 1 else 'y'} and {'their' if n != 1 else 'its'} "
          f"approver.")

    # 6. Recall. Engineers find a faulty sensor; the rule is recalled and
    #    every release that shipped it is flagged.
    notice = agent.recall(rule.capability_id, issued_by=MISSION_GROUP,
                          reason="The vibration came from a faulty sensor.")
    flagged = ", ".join(r["version"] for r in notice.affected_releases)
    print(f"6. Recall: capability recalled; releases flagged: {flagged}.")

    # 7. Verify. Replay the hash-linked evidence chain.
    ok, _ = agent.verify()
    print(f"7. Verify: evidence chain {'intact' if ok else 'BROKEN'}.")
    print(f"   Records are in {folder}")


if __name__ == "__main__":
    main()
