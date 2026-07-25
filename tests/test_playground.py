"""The playground drives a real workspace through the whole loop."""

from brevet.playground import STEPS, PlaygroundSession


def test_playground_full_loop(tmp_path):
    s = PlaygroundSession(tmp_path / "pg")

    # order is enforced
    out = s.run_step("release")
    assert not out["ok"] and "next step is 'work'" in out["error"]

    results = {}
    for name in STEPS:
        out = s.run_step(name)
        assert out["ok"], (name, out)
        results[name] = out

    # the self-promotion attempt was rejected and changed nothing
    assert results["block"]["artifact"]["held"] is True
    assert "dream:nightcycle" in results["block"]["artifact"]["attempted_approver"]

    # the gate passed on measured deltas and the release shipped signed
    assert results["evals"]["artifact"]["gate"]["passed_gate"] is True
    assert results["release"]["artifact"]["signature"]["algorithm"] == "ed25519"
    lock = results["release"]["artifact"]["capabilities_lock"]
    assert lock["resolved"] and all(
        c["approved_by"] == "mission_group:right_first_time"
        for c in lock["resolved"])

    # recall flagged the release; the chain replays clean
    assert results["recall"]["artifact"]["affected_releases"]
    assert results["verify"]["artifact"]["chain_ok"] is True
    assert results["verify"]["artifact"]["envelopes"] > 30

    # every step reported fresh envelopes except the blocked attempt
    assert results["block"]["new_envelopes"] == []
    assert results["work"]["new_envelopes"]


def test_playground_state_shape(tmp_path):
    s = PlaygroundSession(tmp_path / "pg2")
    st = s.state()
    assert st["steps"] == STEPS
    assert st["done"] == []
    assert st["status"]["version"] == "0.1.0"
