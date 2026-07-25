"""Core loop tests: ledger chain, override harvesting, delta mining, gate,
dawn authority rules, release/lock/recall invariants."""

import pytest

from brevet.canonical import Signer, content_sha256
from brevet.delta import mine
from brevet.evals import compile_eval_case, conservative_gate
from brevet.evidence import harvest_override
from brevet.ledger import Ledger
from brevet.lifecycle import CapabilityStore, build_lock, dawn_decide, recall, release
from brevet.models import (
    AgentManifest,
    AuthorityLayer,
    OverrideRecord,
    ReleaseChannel,
    ReleaseRecord,
)


def _override(i: int, substituting: bool = True) -> OverrideRecord:
    return OverrideRecord(
        task_id=f"tsk_{i}",
        intent_preserved=not substituting,
        draft="severity: minor",
        final="severity: major" if substituting else "severity : minor",
        rationale="known precursor",
        tags=["vibration-cip-underrated"],
        task_family="deviation_triage",
    )


def test_ledger_chain_verifies(tmp_path):
    led = Ledger(tmp_path / "ledger.jsonl")
    for i in range(5):
        led.append("brevet.task", {"i": i})
    ok, n = led.verify()
    assert ok and n == 5


def test_ledger_tamper_detected(tmp_path):
    led = Ledger(tmp_path / "ledger.jsonl")
    led.append("brevet.task", {"i": 0})
    led.append("brevet.task", {"i": 1})
    lines = (tmp_path / "ledger.jsonl").read_text().splitlines()
    (tmp_path / "ledger.jsonl").write_text(
        lines[0].replace('"i": 0', '"i": 9') + "\n" + lines[1] + "\n")
    ok, _ = Ledger(tmp_path / "ledger.jsonl").verify()
    assert not ok


def test_override_classification():
    sub = harvest_override(task_id="t", draft="severity: minor, log and monitor",
                           final="severity: major, escalate now")
    assert sub is not None and sub.intent_preserved is False
    ref = harvest_override(task_id="t", draft="The pump is fine. Monitor weekly.",
                           final="Pump looks fine, keep monitoring weekly.")
    assert ref is not None and ref.intent_preserved is True
    same = harvest_override(task_id="t", draft="x", final="x")
    assert same is None


def test_mine_requires_recurrence():
    assert mine([_override(i) for i in range(2)]) == []
    cands = mine([_override(i) for i in range(4)], model_family="gemma4")
    assert len(cands) == 1
    cand = cands[0]
    assert cand.authority_layer == AuthorityLayer.evidence
    assert cand.source_pathway.value == "endogenous"
    assert cand.evidence.recurrence_count == 4
    assert cand.content_hash == content_sha256(cand.content)
    assert not cand.releasable  # evidence layer can never be locked


def test_conservative_gate():
    assert conservative_gate(0.1, 0.0)
    assert conservative_gate(0.0, 0.2)
    assert not conservative_gate(0.5, -0.1)  # no trading splits
    assert not conservative_gate(0.0, 0.0)   # must strictly improve somewhere


def test_dawn_requires_human_approver(tmp_path):
    store = CapabilityStore(tmp_path / "caps.jsonl")
    led = Ledger(tmp_path / "ledger.jsonl")
    cand = mine([_override(i) for i in range(3)])[0]
    store.add(cand)
    with pytest.raises(PermissionError):
        dawn_decide(store, led, cand.capability_id, "promote", approver="dream:nightcycle")
    cap = dawn_decide(store, led, cand.capability_id, "promote",
                      approver="mission_group:rft", to_layer=AuthorityLayer.advisory)
    assert cap.releasable


def test_release_lock_and_recall(tmp_path):
    store = CapabilityStore(tmp_path / "caps.jsonl")
    led = Ledger(tmp_path / "ledger.jsonl")
    signer = Signer(tmp_path / "k.pem")
    cand = mine([_override(i) for i in range(3)])[0]
    store.add(cand)
    dawn_decide(store, led, cand.capability_id, "promote",
                approver="mission_group:rft", to_layer=AuthorityLayer.advisory)
    manifest = AgentManifest(agent="demo_agent", version="0.1.0")

    # non-shadow release without passing gate is blocked
    with pytest.raises(ValueError):
        release(manifest, store, led, signer, to_version="0.2.0",
                channel=ReleaseChannel.trial, approver="mission_group:rft",
                eval_summary={"delta_held_in": -0.1, "delta_held_out": 0.2})

    manifest, lock, _record = release(
        manifest, store, led, signer, to_version="0.2.0",
        channel=ReleaseChannel.trial, approver="mission_group:rft",
        eval_summary={"delta_held_in": 0.1, "delta_held_out": 0.0})
    assert manifest.signature and manifest.signature["algorithm"] == "ed25519"
    assert Signer.verify(signer.public_key_hex(), manifest.unsigned_payload(),
                         manifest.signature["signature"])
    assert [r.capability_id for r in lock.resolved] == [cand.capability_id]

    releases = [ReleaseRecord(**e["body"]) for e in led.read("brevet.release")]
    notice = recall(store, led, cand.capability_id, reason="wrong", reason_class="incorrect",
                    severity="high", issued_by="mission_group:rft", releases=releases)
    assert notice.affected_releases and notice.affected_releases[0]["version"] == "0.2.0"
    # after recall the capability can no longer be locked into a new release
    lock2 = build_lock(manifest, store)
    assert lock2.resolved == []
    ok, _ = led.verify()
    assert ok


def test_eval_case_carries_provenance():
    case = compile_eval_case(_override(1))
    assert case.kind.value == "eval_case"
    assert case.provenance.source_overrides
    assert '"expected_final"' in case.content


def test_demo_end_to_end(tmp_path):
    from brevet.demo import run_demo
    summary = run_demo(tmp_path / "demo", echo=lambda s: None)
    assert summary["chain_ok"]
    assert summary["candidates"] >= 1
    assert summary["locked"] >= 1
    assert summary["recalled"] == 1
