"""Override-compiled evals and the conservative gate.

Judgement work rarely has a ready-made test set. Overrides fill the gap:
each substituting override that carries the expert's final becomes an eval
case whose expected answer is that final. Refining overrides are not
compiled into cases in this version. Eval cases are capabilities themselves
(kind=eval_case), so the suite has provenance and can be recalled like
anything else.

The conservative gate follows the acceptance rule of Self-Harness:
    delta_held_in >= 0  AND  delta_held_out >= 0  AND  max(deltas) > 0
A change may not improve one half of the tests by making the other half
worse. It is not a guarantee of no regressions: gains and losses can still
cancel out inside one half.

A release is bound to the eval runs behind its numbers (``bind_evals``): the
'before' run evaluated the current release, the 'after' run exactly what the
release locks, on the same cases with the same scorer, and the deltas are
computed from the runs. Deltas supplied without runs are recorded as
attested by the approver."""

from __future__ import annotations

import json

from brevet.canonical import object_sha256
from brevet.models import (
    ApplicabilityContext,
    CapabilityEvidence,
    CapabilityKind,
    CapabilityObject,
    OverrideRecord,
    Provenance,
    SourcePathway,
)


def compile_eval_case(override: OverrideRecord) -> CapabilityObject:
    payload = {
        "case_type": "regression" if not override.intent_preserved else "rubric",
        "task_id": override.task_id,
        "task_family": override.task_family,
        "given_draft": override.draft,
        "expected_final": override.final,
        "human_rationale": override.rationale,
        "tags": override.tags,
    }
    return CapabilityObject(
        title=f"eval[{payload['case_type']}] from {override.override_id}",
        kind=CapabilityKind.eval_case,
        content=json.dumps(payload, sort_keys=True, ensure_ascii=False),
        payload_schema="brevet/eval_case_payload",
        source_pathway=SourcePathway.exogenous,  # the label is a human judgment
        provenance=Provenance(
            source_overrides=[override.override_id],
            capture_method="eval_compiler/override",
            originating_participant=override.participant,
        ),
        conditions=ApplicabilityContext(task_family=override.task_family),
        evidence=CapabilityEvidence(
            recurrence_count=1,
            supporting_overrides=[override.override_id],
            evidence_strength="moderate",
        ),
        confidence=0.8,
    ).seal()


def compile_suite(overrides: list[OverrideRecord]) -> list[CapabilityObject]:
    return [compile_eval_case(o) for o in overrides]


def conservative_gate(delta_held_in: float, delta_held_out: float) -> bool:
    return delta_held_in >= 0 and delta_held_out >= 0 and max(delta_held_in, delta_held_out) > 0


# ------------------------------------------------------------- binding

def capability_set_digest(entries) -> str:
    """The digest of a set of capabilities as a lock records them."""
    return object_sha256(sorted([e.capability_id, e.content_hash, e.authority_layer]
                                for e in entries))


def harness_digests(components, sources) -> dict[str, str]:
    """One digest per harness source (files, agent, env), so runs and locks
    taken with different sources can still be compared where they overlap."""
    kinds = {"files": "file", "agent": "agent", "env": "env"}
    out = {}
    for source in sources:
        picked = sorted([c.component_id, c.digest] for c in components
                        if c.kind == kinds.get(source))
        out[source] = object_sha256(picked)
    return out


def manifest_core(manifest) -> str:
    """The manifest's content without its version, release block and
    signature: what an evaluation and the release it backs must share."""
    payload = manifest.unsigned_payload()
    return object_sha256({k: v for k, v in payload.items() if k not in ("version", "release")})


def _different_harness(run: dict, components, sources) -> str | None:
    """The first harness source a run and a lock disagree on, if any."""
    recorded = run.get("harness") or {}
    for source, digest in harness_digests(components, sources).items():
        if source in recorded and recorded[source] != digest:
            return source
    return None


def _default_scorer_digest() -> str:
    from brevet.harness import _source
    from brevet.runner import decision_scorer
    return object_sha256(_source(decision_scorer))


def _check_run(run: dict, ref: str, tasks: dict, outputs: dict, store,
               first: dict[str, str]) -> None:
    """A run's results must stand up against the chain: its cases are real
    eval cases matching its cases digest, every task it cites is an
    evaluation task with a recorded output, cited by no earlier run and only
    once here, each task was given only the capabilities the run says
    it evaluated, its pass rates follow from its per-case results, and with
    the default scorer every recorded pass follows from the recorded output."""
    results = run.get("results")
    if not isinstance(results, list) or (not results and run.get("n_cases")):
        raise ValueError(f"eval run {ref} does not record its per-case results; evaluate again")
    cases = {}
    if store is not None:
        for cap in store.all().values():
            if cap.kind.value == "eval_case":
                cases[cap.capability_id] = cap
    if store is not None:
        named = [item.get("case") for item in results]
        if any(c not in cases for c in named) or object_sha256(sorted(
                [c, cases[c].content_hash] for c in named)) != run.get("cases_digest"):
            raise ValueError(f"eval run {ref} names cases that do not match its cases digest")
    rescore = store is not None and run.get("scorer_digest") == _default_scorer_digest()
    from brevet.runner import decision_scorer
    allowed = set(run.get("capabilities") or [])
    seen: set[str] = set()
    rates: dict[str, list[float]] = {"held_in": [], "held_out": []}
    for item in results:
        passes, ids = item.get("passes") or [], item.get("tasks") or []
        if not passes or len(passes) != len(ids):
            raise ValueError(f"eval run {ref} has a case without its tasks")
        expected = None
        if rescore:
            try:
                expected = json.loads(cases[item["case"]].content).get("expected_final")
            except (ValueError, KeyError):
                expected = None
        for task_id, passed in zip(ids, passes, strict=True):
            task = tasks.get(task_id) or {}
            if not task.get("evaluation"):
                raise ValueError(f"eval run {ref} names {task_id}, which is not an "
                                 f"evaluation task on the evidence chain")
            if task_id not in outputs:
                raise ValueError(f"eval run {ref} names {task_id}, which has no recorded output")
            if first.get(task_id, ref) != ref or task_id in seen:
                raise ValueError(f"eval run {ref} cites {task_id}, which an earlier run or "
                                 f"case already cites")
            seen.add(task_id)
            served = task.get("served_rules")
            if served is not None and not set(served) <= allowed:
                raise ValueError(f"eval run {ref} cites {task_id}, which was given capabilities "
                                 f"the run does not say it evaluated")
            if expected is not None and bool(passed) != bool(
                    decision_scorer(outputs[task_id], expected)):
                raise ValueError(f"eval run {ref} records a result its task's output does "
                                 f"not support")
        rates.setdefault(item.get("split"), []).append(sum(map(bool, passes)) / len(passes))
    for split in ("held_in", "held_out"):
        xs = rates.get(split) or []
        if abs((sum(xs) / len(xs) if xs else 0.0) - float(run.get(f"{split}_pass_rate", 0))) > 1e-9:
            raise ValueError(f"eval run {ref} reports a {split} pass rate its cases do not give")


def bind_evals(ledger, before: str | dict, after: str | dict, *, agent: str, lock,
               harness, manifest, previous_lock=None, store=None,
               first_release: bool = False) -> dict:
    """The eval summary of a release backed by two recorded eval runs: the
    'before' run must have evaluated the current release and the 'after' run
    exactly what this release locks (capabilities, harness and manifest),
    both on the same cases with the same scorer. The deltas come from the
    runs, never from the caller."""
    refs = [r.get("run_ref") if isinstance(r, dict) else r for r in (before, after)]
    envelopes = list(ledger.read())
    order = {e["envelope_id"]: (i, e["body"]) for i, e in enumerate(envelopes)
             if e.get("kind") == "brevet.eval_run"}
    for ref in refs:
        if ref not in order:
            raise ValueError(f"no eval run {ref!r} on the evidence chain")
    (bi, b), (ai, a) = order[refs[0]], order[refs[1]]
    tasks = {e["envelope_id"]: e["body"] for e in envelopes if e.get("kind") == "brevet.task"}
    outputs = {e["body"].get("task_id"): e["body"].get("output") for e in envelopes
               if e.get("kind") == "brevet.artefact" and "output" in (e.get("body") or {})}
    first: dict[str, str] = {}  # each task belongs to the first run that cites it
    for env in envelopes:
        if env.get("kind") == "brevet.eval_run":
            for item in env["body"].get("results") or []:
                for task_id in item.get("tasks") or []:
                    first.setdefault(task_id, env["envelope_id"])
    for run, ref in ((b, refs[0]), (a, refs[1])):
        _check_run(run, ref, tasks, outputs, store, first)
    if bi >= ai:
        raise ValueError("the 'before' run must have been recorded before the 'after' run")
    for run in (a, b):
        if run.get("agent") not in (None, agent):
            raise ValueError(f"eval run {run.get('run_ref')} belongs to {run.get('agent')}")
    for key in ("cases_digest", "scorer", "scorer_digest", "repeats"):
        if a.get(key) != b.get(key):
            raise ValueError(f"the two eval runs differ in {key.replace('_', ' ')}: compare "
                             f"runs over the same cases with the same scorer")
    if a.get("capability_set") != capability_set_digest(lock.resolved):
        raise ValueError("the 'after' run evaluated a different set of capabilities than this "
                         "release locks; evaluate the candidate again")
    if harness is not None and (source := _different_harness(a, harness[0], harness[1])):
        raise ValueError(f"the 'after' run evaluated a different harness ({source}) than this "
                         f"release locks; evaluate again, and release from Python if the "
                         f"agent's own code changed, so the running agent is inventoried")
    if a.get("manifest_core") != manifest_core(manifest):
        raise ValueError("the 'after' run evaluated a different agent.yaml; evaluate again")
    if previous_lock is None and first_release and (
            b.get("capability_set") != capability_set_digest([]) or not b.get("baseline")):
        raise ValueError("the 'before' run of a first release must evaluate the agent with no "
                         "released capabilities: run agent.evaluate(baseline=True)")
    if previous_lock is not None:
        if b.get("capability_set") != capability_set_digest(previous_lock.resolved):
            raise ValueError("the 'before' run did not evaluate the current release: run "
                             "agent.evaluate(baseline=True) before changing the agent")
        if previous_lock.harness and (source := _different_harness(
                b, previous_lock.harness, previous_lock.harness_sources)):
            raise ValueError(f"the 'before' run did not evaluate the current release (its "
                             f"{source} differs): run agent.evaluate(baseline=True) before "
                             f"changing the agent")
    return {"source": "measured", "before": refs[0], "after": refs[1],
            "delta_held_in": round(a["held_in_pass_rate"] - b["held_in_pass_rate"], 6),
            "delta_held_out": round(a["held_out_pass_rate"] - b["held_out_pass_rate"], 6),
            "cases_digest": a.get("cases_digest"), "scorer": a.get("scorer"),
            "repeats": a.get("repeats"), "gate": "conservative"}
