"""Signed approvals: once a workspace registers approvers, every decision must
be signed by a key that an agent calling Brevet's tools does not hold."""

import json
import os
import stat

import pytest
import yaml

from brevet.approvals import (
    apply_if_ready,
    create_key,
    load_key,
    register_from_chain,
    request_promotion,
    request_recall,
    request_register_add,
    request_release,
    request_threshold,
    sign_request,
    verify_approvals,
)
from brevet.canonical import Signer
from brevet.delta import dream_cycle
from brevet.evidence import harvest_override
from brevet.ledger import Ledger
from brevet.lifecycle import CapabilityStore, dawn_decide
from brevet.models import AgentManifest, AuthorityLayer

PASS = "correct horse battery"


@pytest.fixture
def ws(tmp_path):
    """A workspace with one candidate rule and three eval cases."""
    wd = tmp_path / ".brevet"
    ledger = Ledger(wd / "ledger.jsonl")
    store = CapabilityStore(wd / "capabilities.jsonl")
    for i in range(3):
        ov = harvest_override(task_id=f"t{i}", draft="severity: minor", final="severity: major",
                              participant="human:qa@x", rationale="seal wear",
                              tags=["vibration"], task_family="triage")
        ledger.append("brevet.override", ov.model_dump())
    dream_cycle(ledger, store)
    rule = next(c for c in store.all().values() if c.kind.value == "prompt_rule")
    return {"wd": wd, "ledger": ledger, "store": store, "rule": rule,
            "keys": tmp_path / "keys", "tmp": tmp_path}


def _register(ws, identity, groups=None, *, approver_key=None):
    key = create_key(identity, PASS, ws["keys"])
    req = request_register_add(ws["wd"], key, groups)
    if approver_key is not None:
        sign_request(ws["wd"], req["request_id"], approver_key)
    out = apply_if_ready(ws["wd"], req["request_id"])
    return key, out


def _signed_promotion(ws, key, capability_id, approver, **kw):
    req = request_promotion(ws["wd"], ws["store"], capability_id, "promote",
                            approver=approver, **kw)
    sign_request(ws["wd"], req["request_id"], key)
    return req, apply_if_ready(ws["wd"], req["request_id"])


# ------------------------------------------------------------- keys

def test_keys_are_encrypted_private_and_belong_to_people(ws):
    key = create_key("human:alice@example.com", PASS, ws["keys"])
    pem = next(ws["keys"].glob("*.pem"))
    assert b"ENCRYPTED PRIVATE KEY" in pem.read_bytes()
    if os.name == "posix":
        assert stat.S_IMODE(pem.stat().st_mode) == 0o600
    assert load_key("human:alice@example.com", PASS, ws["keys"]).public_key == key.public_key
    with pytest.raises(PermissionError):
        load_key("human:alice@example.com", "wrong passphrase", ws["keys"])
    with pytest.raises(ValueError):
        create_key("human:bob@example.com", "short", ws["keys"])
    with pytest.raises(ValueError):
        create_key("mission_group:quality", PASS, ws["keys"])


# ------------------------------------------------------------- decisions

def test_first_approver_turns_signing_on(ws):
    assert not register_from_chain(ws["ledger"]).enabled
    _, out = _register(ws, "human:alice@example.com")
    assert out["status"] == "applied" and register_from_chain(ws["ledger"]).enabled
    with pytest.raises(PermissionError, match="brevet approve"):
        dawn_decide(ws["store"], ws["ledger"], ws["rule"].capability_id, "promote",
                    approver="human:alice@example.com")


def test_request_sign_apply_and_no_reuse(ws):
    alice, _ = _register(ws, "human:alice@example.com")
    req, out = _signed_promotion(ws, alice, ws["rule"].capability_id, "human:alice@example.com")
    assert out["status"] == "applied"
    assert out["result"]["validation_state"] == "promoted_to_advisory"
    promotion = list(ws["ledger"].read("brevet.promotion"))[-1]["body"]
    assert promotion["approval"]["signatures"][0]["identity"] == "human:alice@example.com"
    assert apply_if_ready(ws["wd"], req["request_id"])["status"] == "applied"  # no second run
    with pytest.raises(PermissionError, match="already been applied"):
        dawn_decide(ws["store"], ws["ledger"], ws["rule"].capability_id, "promote",
                    approver="human:alice@example.com", approval=promotion["approval"])
    report = verify_approvals(ws["ledger"])
    assert report["signed"] == 1 and report["invalid"] == []


def test_forged_or_mismatched_approvals_are_refused(ws):
    alice, _ = _register(ws, "human:alice@example.com")
    mallory = create_key("human:mallory@example.com", PASS, ws["tmp"] / "other")
    req = request_promotion(ws["wd"], ws["store"], ws["rule"].capability_id, "promote",
                            approver="human:alice@example.com")
    sign_request(ws["wd"], req["request_id"], mallory)
    with pytest.raises(PermissionError, match="not an active approver"):
        apply_if_ready(ws["wd"], req["request_id"])

    case = next(c for c in ws["store"].all().values() if c.kind.value == "eval_case")
    good = request_promotion(ws["wd"], ws["store"], case.capability_id, "promote",
                             approver="human:alice@example.com")
    signed = sign_request(ws["wd"], good["request_id"], alice)
    approval = {k: signed[k] for k in ("payload", "payload_hash", "signatures")}
    with pytest.raises(PermissionError, match="does not match"):
        dawn_decide(ws["store"], ws["ledger"], ws["rule"].capability_id, "promote",
                    approver="human:alice@example.com", approval=approval)


def test_mission_group_threshold_needs_every_required_member(ws):
    group = "mission_group:quality"
    alice, _ = _register(ws, "human:alice@example.com", [group])
    bob, out = _register(ws, "human:bob@example.com", [group], approver_key=alice)
    assert out["status"] == "applied"
    threshold = request_threshold(ws["wd"], group, 2)
    sign_request(ws["wd"], threshold["request_id"], alice)
    assert apply_if_ready(ws["wd"], threshold["request_id"])["status"] == "applied"

    req = request_promotion(ws["wd"], ws["store"], ws["rule"].capability_id, "promote",
                            approver=group, to_layer="controlled")
    sign_request(ws["wd"], req["request_id"], alice)
    waiting = apply_if_ready(ws["wd"], req["request_id"])
    assert waiting["status"] == "pending" and "needs 2" in waiting["needs"][0]
    sign_request(ws["wd"], req["request_id"], bob)
    done = apply_if_ready(ws["wd"], req["request_id"])
    assert done["result"]["authority_layer"] == "controlled"


def test_release_is_refused_if_promotions_change_after_signing(ws):
    alice, _ = _register(ws, "human:alice@example.com")
    _signed_promotion(ws, alice, ws["rule"].capability_id, "human:alice@example.com")
    manifest_path = ws["tmp"] / "agent.yaml"
    manifest_path.write_text(yaml.safe_dump(AgentManifest(agent="a").model_dump()))
    rel = request_release(ws["wd"], AgentManifest(agent="a"), ws["store"], to_version="0.2.0",
                          channel="trial", approver="human:alice@example.com",
                          eval_summary={"delta_held_in": 0.5, "delta_held_out": 0.5},
                          manifest_path=manifest_path)
    sign_request(ws["wd"], rel["request_id"], alice)
    case = next(c for c in ws["store"].all().values() if c.kind.value == "eval_case")
    _signed_promotion(ws, alice, case.capability_id, "human:alice@example.com")
    with pytest.raises(PermissionError, match="lockfile_hash"):
        apply_if_ready(ws["wd"], rel["request_id"])
    from brevet.approvals import PendingRequests
    assert PendingRequests(ws["wd"]).get(rel["request_id"])["status"] == "failed"

    fresh = request_release(ws["wd"], AgentManifest(agent="a"), ws["store"], to_version="0.2.0",
                            channel="trial", approver="human:alice@example.com",
                            eval_summary={"delta_held_in": 0.5, "delta_held_out": 0.5},
                            manifest_path=manifest_path)
    sign_request(ws["wd"], fresh["request_id"], alice)
    out = apply_if_ready(ws["wd"], fresh["request_id"])
    assert out["result"]["to"] == "0.2.0" and out["result"]["locked"] == 2
    released = yaml.safe_load(manifest_path.read_text())
    assert released["version"] == "0.2.0" and released["signature"]["public_key"]


def test_signed_recall(ws):
    alice, _ = _register(ws, "human:alice@example.com")
    req = request_recall(ws["wd"], ws["store"], ws["rule"].capability_id,
                         reason="faulty sensor", issued_by="human:alice@example.com")
    sign_request(ws["wd"], req["request_id"], alice)
    assert apply_if_ready(ws["wd"], req["request_id"])["status"] == "applied"
    assert ws["store"].all()[ws["rule"].capability_id].revocation_status.value == "withdrawn"
    assert verify_approvals(ws["ledger"])["signed"] == 1


# ------------------------------------------------------------- register

def test_register_changes_need_another_approver(ws):
    _register(ws, "human:alice@example.com")
    _, out = _register(ws, "human:eve@example.com")  # signed only by her own new key
    assert out["status"] == "pending"
    assert "human:eve@example.com" not in register_from_chain(ws["ledger"]).active()


def test_a_forged_register_entry_is_ignored_and_reported(ws):
    _register(ws, "human:alice@example.com")
    eve = create_key("human:eve@example.com", PASS, ws["tmp"] / "eve")
    payload = {"kind": "brevet.approver", "change": "add", "identity": eve.identity,
               "public_key": eve.public_key, "groups": [], "request_id": "req_forged",
               "requested_at": "2026-10-06T00:00:00+00:00"}
    from brevet.canonical import object_sha256
    ws["ledger"].append("brevet.approver", {
        "payload": payload, "payload_hash": object_sha256(payload),
        "signatures": [{"identity": eve.identity, "public_key": eve.public_key,
                        "signature": eve.sign(payload), "signed_at": "now"}]})
    assert eve.identity not in register_from_chain(ws["ledger"]).active()
    report = verify_approvals(ws["ledger"])
    assert report["invalid"] and "another active approver" in report["invalid"][0]["problem"]


def test_the_last_approver_cannot_be_revoked(ws):
    alice, _ = _register(ws, "human:alice@example.com")
    from brevet.approvals import request_register_revoke
    req = request_register_revoke(ws["wd"], "human:alice@example.com")
    sign_request(ws["wd"], req["request_id"], alice)
    with pytest.raises(PermissionError, match="last active approver"):
        apply_if_ready(ws["wd"], req["request_id"])


def test_decisions_before_signing_stay_valid(ws):
    dawn_decide(ws["store"], ws["ledger"], ws["rule"].capability_id, "promote",
                approver="human:alice@example.com")
    _register(ws, "human:alice@example.com")
    report = verify_approvals(ws["ledger"])
    assert report["unsigned_before_signing"] == 1 and report["invalid"] == []


# ------------------------------------------------------------- MCP and CLI

def _call(server, name, args=None):
    import asyncio
    result = asyncio.run(server.call_tool(name, args or {}))
    content = getattr(result, "content", result)
    if isinstance(content, tuple):
        content = content[0]
    return json.loads(content[0].text)


def test_mcp_tools_return_a_request_when_signing_is_required(ws):
    pytest.importorskip("mcp")
    from brevet.mcp_server import build_server
    (ws["tmp"] / "agent.yaml").write_text(yaml.safe_dump({"agent": "t", "version": "0.1.0"}))
    server = build_server(str(ws["wd"]), str(ws["tmp"] / "agent.yaml"))
    alice, _ = _register(ws, "human:alice@example.com")
    out = _call(server, "brevet_dawn_decide", {"capability_id": ws["rule"].capability_id,
                                               "outcome": "promote",
                                               "approver": "human:alice@example.com"})
    assert out["status"] == "awaiting_signature" and "brevet approve" in out["command"]
    assert ws["store"].all()[ws["rule"].capability_id].validation_state.value == "captured"
    assert _call(server, "brevet_status")["signed_approvals"]["awaiting_signatures"] == 1
    sign_request(ws["wd"], out["request_id"], alice)
    assert apply_if_ready(ws["wd"], out["request_id"])["status"] == "applied"
    assert _call(server, "brevet_verify")["approvals"]["signed"] == 1


def test_brevet_active_reports_forged_decisions_and_refuses_a_forged_release(ws):
    pytest.importorskip("mcp")
    from brevet.mcp_server import build_server
    alice, _ = _register(ws, "human:alice@example.com")
    _signed_promotion(ws, alice, ws["rule"].capability_id, "human:alice@example.com")
    manifest_path = ws["tmp"] / "agent.yaml"
    manifest_path.write_text(yaml.safe_dump(AgentManifest(agent="t").model_dump()))
    rel = request_release(ws["wd"], AgentManifest(agent="t"), ws["store"], to_version="0.2.0",
                          channel="shadow", approver="human:alice@example.com",
                          manifest_path=manifest_path)
    sign_request(ws["wd"], rel["request_id"], alice)
    assert apply_if_ready(ws["wd"], rel["request_id"])["status"] == "applied"
    server = build_server(str(ws["wd"]), str(manifest_path))
    assert _call(server, "brevet_active")["count"] == 1

    ws["ledger"].append("brevet.promotion", {
        "capability_id": "cap_other", "outcome": "promote", "to_layer": "advisory",
        "approver": "human:alice@example.com", "notes": "",
        "content_hash": "sha256:" + "0" * 64})  # unsigned, after signing was required
    out = _call(server, "brevet_active")
    assert out["count"] == 1 and "approvals_warning" in out  # it grants nothing, but is shown

    forged = dict(list(ws["ledger"].read("brevet.release"))[-1]["body"])
    forged["approval"] = {**forged["approval"], "signatures": []}
    ws["ledger"].append("brevet.release", forged)  # the latest release, without valid signatures
    out = _call(server, "brevet_active")
    assert out["count"] == 0 and "invalid approval signatures" in out["error"]


def test_cli_approve_signs_with_the_passphrase(ws, monkeypatch, capsys):
    from brevet import cli
    monkeypatch.setenv("BREVET_APPROVER_DIR", str(ws["keys"]))
    monkeypatch.setattr("getpass.getpass", lambda prompt="": PASS)

    def run(*argv):
        monkeypatch.setattr("sys.argv", ["brevet", *argv])
        try:
            cli.main()
        except SystemExit as e:
            return e.code
        return 0

    wd = str(ws["wd"])
    assert run("approver", "add", "--identity", "human:alice@example.com", "--workdir", wd) in (0, None)
    assert run("dawn", "--workdir", wd, "--decide", f"{ws['rule'].capability_id}:promote",
               "--approver", "human:alice@example.com") in (0, None)
    assert "brevet approve" in capsys.readouterr().out
    assert run("approve", "--all", "--workdir", wd) in (0, None)
    assert "is now promoted_to_advisory" in capsys.readouterr().out
    assert run("verify", "--workdir", wd) in (0, None)
    assert "1 signed" in capsys.readouterr().out
    assert ws["store"].all()[ws["rule"].capability_id].authority_layer == AuthorityLayer.advisory
    assert Signer  # the workspace key and approver keys stay separate


def test_asking_twice_returns_the_pending_request(ws):
    _register(ws, "human:alice@example.com")
    first = request_promotion(ws["wd"], ws["store"], ws["rule"].capability_id, "promote",
                              approver="human:alice@example.com")
    again = request_promotion(ws["wd"], ws["store"], ws["rule"].capability_id, "promote",
                              approver="human:alice@example.com")
    assert again["request_id"] == first["request_id"]


# ------------------------------------------------------------- review fixes

def test_a_store_entry_without_a_promotion_on_the_chain_cannot_be_released(ws):
    from brevet.lifecycle import release
    from brevet.models import AuthorityLayer, ValidationState
    forged = ws["rule"].model_copy(update={
        "validation_state": ValidationState.promoted_to_advisory,
        "authority_layer": AuthorityLayer.advisory})
    forged.provenance.human_confirmed_by = "human:qa@x"
    ws["store"].add(forged)  # promoted in the store, never at dawn
    with pytest.raises(ValueError, match="no matching promotion"):
        release(AgentManifest(agent="a"), ws["store"], ws["ledger"],
                Signer(ws["tmp"] / "k.pem"), to_version="0.2.0", channel="shadow",
                approver="human:qa@x")


def test_a_replayed_registration_does_not_restore_a_revoked_approver(ws):
    from brevet.approvals import request_register_revoke
    alice, _ = _register(ws, "human:alice@example.com")
    _register(ws, "human:bob@example.com", approver_key=alice)
    bob_add = next(e for e in ws["ledger"].read("brevet.approver")
                   if e["body"]["payload"]["identity"] == "human:bob@example.com")
    revoke = request_register_revoke(ws["wd"], "human:bob@example.com")
    sign_request(ws["wd"], revoke["request_id"], alice)
    apply_if_ready(ws["wd"], revoke["request_id"])
    ws["ledger"].append("brevet.approver", bob_add["body"])  # replay the old registration
    assert "human:bob@example.com" not in register_from_chain(ws["ledger"]).active()


def test_group_changes_need_the_group_quorum(ws):
    group = "mission_group:quality"
    alice, _ = _register(ws, "human:alice@example.com", [group])
    _register(ws, "human:bob@example.com", [group], approver_key=alice)
    raise_to_two = request_threshold(ws["wd"], group, 2)
    sign_request(ws["wd"], raise_to_two["request_id"], alice)
    apply_if_ready(ws["wd"], raise_to_two["request_id"])
    lower = request_threshold(ws["wd"], group, 1)
    sign_request(ws["wd"], lower["request_id"], alice)
    assert apply_if_ready(ws["wd"], lower["request_id"])["status"] == "pending"  # bob too
    puppet = create_key("human:puppet@example.com", PASS, ws["keys"])
    join = request_register_add(ws["wd"], puppet, [group])
    sign_request(ws["wd"], join["request_id"], alice)
    assert apply_if_ready(ws["wd"], join["request_id"])["status"] == "pending"


def test_new_release_approvals_must_cover_the_manifest(ws):
    from brevet.canonical import object_sha256
    from brevet.lifecycle import release
    alice, _ = _register(ws, "human:alice@example.com")
    payload = {"kind": "brevet.release", "agent": "a", "from_version": "0.1.0",
               "to_version": "0.2.0", "channel": "shadow",
               "lockfile_hash": object_sha256([]), "delta_held_in": None,
               "delta_held_out": None, "rationale": "", "approver": "human:alice@example.com",
               "request_id": "req_old_style", "requested_at": "now"}  # no manifest_hash
    approval = {"payload": payload, "payload_hash": object_sha256(payload),
                "signatures": [{"identity": alice.identity, "public_key": alice.public_key,
                                "signature": alice.sign(payload), "signed_at": "now"}]}
    with pytest.raises(PermissionError, match="manifest_hash"):
        release(AgentManifest(agent="a"), ws["store"], ws["ledger"],
                Signer(ws["tmp"] / "k.pem"), to_version="0.2.0", channel="shadow",
                approver="human:alice@example.com", approval=approval)


def _old_style(ws, payload, *signers):
    """A register change as 0.3.0 wrote it (no group_quorum), appended as is."""
    from brevet.approvals import _approval, create_request
    req = create_request(ws["wd"], {"kind": "brevet.approver", **payload}, summary="0.3.0")
    for key in signers:
        req = sign_request(ws["wd"], req["request_id"], key)
    ws["ledger"].append("brevet.approver", _approval(req))


def _group_of_two(ws, group):
    alice = create_key("human:alice@example.com", PASS, ws["keys"])
    bob = create_key("human:bob@example.com", PASS, ws["keys"])
    _old_style(ws, {"change": "add", "identity": alice.identity,
                    "public_key": alice.public_key, "groups": [group]}, alice)
    _old_style(ws, {"change": "add", "identity": bob.identity,
                    "public_key": bob.public_key, "groups": [group]}, bob, alice)
    return alice, bob


def _written_by_0_3(ledger):
    """Rewrite the chain as 0.3.0 wrote it: no envelope names its runtime."""
    from brevet.canonical import chain_hash
    from brevet.ledger import GENESIS
    prev, lines = GENESIS, []
    for env in list(ledger.read()):
        env.pop("runtime", None)
        env.pop("chain_hash", None)
        env["prev_hash"] = prev
        env["chain_hash"] = prev = chain_hash(dict(env), prev)
        lines.append(json.dumps(env))
    ledger.path.write_text("\n".join(lines) + "\n")
    ledger._prev = prev


def test_register_changes_signed_under_0_3_0_still_replay(ws):
    group = "mission_group:quality"
    alice, _ = _group_of_two(ws, group)
    _old_style(ws, {"change": "threshold", "group": group, "threshold": 2}, alice)
    carol = create_key("human:carol@example.com", PASS, ws["keys"])
    _old_style(ws, {"change": "add", "identity": carol.identity, "public_key": carol.public_key,
                    "groups": [group]}, carol, alice)  # one member's signature sufficed then
    _written_by_0_3(ws["ledger"])
    assert ws["ledger"].verify()[0]
    assert verify_approvals(ws["ledger"])["invalid"] == []
    assert "human:carol@example.com" in register_from_chain(ws["ledger"]).active()


def test_an_old_style_change_cannot_be_slipped_in_after_the_upgrade(ws):
    group = "mission_group:quality"
    alice, _ = _group_of_two(ws, group)
    _written_by_0_3(ws["ledger"])
    raise_to_two = request_threshold(ws["wd"], group, 2)  # made under 0.4.0
    sign_request(ws["wd"], raise_to_two["request_id"], alice)
    assert apply_if_ready(ws["wd"], raise_to_two["request_id"])["status"] == "applied"
    mallory = create_key("human:mallory@example.com", PASS, ws["keys"])
    _old_style(ws, {"change": "add", "identity": mallory.identity,
                    "public_key": mallory.public_key, "groups": [group]}, mallory, alice)
    assert len(verify_approvals(ws["ledger"])["invalid"]) == 1
    assert "human:mallory@example.com" not in register_from_chain(ws["ledger"]).active()


def test_one_member_cannot_lower_a_quorum_with_an_old_style_record(ws):
    """A 0.3.0 chain whose group never changed under 0.4.0: once 0.4.0 has
    written anything, a threshold change in the old style needs the quorum."""
    group = "mission_group:quality"
    alice, _ = _group_of_two(ws, group)
    _old_style(ws, {"change": "threshold", "group": group, "threshold": 2}, alice)
    _written_by_0_3(ws["ledger"])
    ws["ledger"].append("brevet.task", {"task_id": "t_after_upgrade"})  # 0.4.0 writes
    _old_style(ws, {"change": "threshold", "group": group, "threshold": 1}, alice)
    report = verify_approvals(ws["ledger"])
    assert len(report["invalid"]) == 1 and "needs 2" in report["invalid"][0]["problem"]
    assert register_from_chain(ws["ledger"]).threshold(group) == 2


def _two_of_two(ws, group="mission_group:quality"):
    alice, _ = _register(ws, "human:alice@example.com", [group])
    bob, _ = _register(ws, "human:bob@example.com", [group], approver_key=alice)
    req = request_threshold(ws["wd"], group, 2)
    sign_request(ws["wd"], req["request_id"], alice)
    assert apply_if_ready(ws["wd"], req["request_id"])["status"] == "applied"
    return alice, bob


def test_one_member_cannot_revoke_another_to_shrink_a_quorum(ws):
    from brevet.approvals import request_register_revoke
    _alice, bob = _two_of_two(ws)
    req = request_register_revoke(ws["wd"], "human:alice@example.com")
    sign_request(ws["wd"], req["request_id"], bob)
    out = apply_if_ready(ws["wd"], req["request_id"])
    assert out["status"] == "pending" and "needs 2" in out["needs"][0]
    assert "human:alice@example.com" in register_from_chain(ws["ledger"]).active()


def test_one_member_cannot_replace_another_members_key(ws):
    alice, bob = _two_of_two(ws)
    stolen = create_key("human:alice@example.com", PASS, ws["tmp"] / "bobs-machine")
    req = request_register_add(ws["wd"], stolen, ["mission_group:quality"])
    sign_request(ws["wd"], req["request_id"], bob)
    out = apply_if_ready(ws["wd"], req["request_id"])
    assert out["status"] == "pending" and "needs 2" in out["needs"][0]
    register = register_from_chain(ws["ledger"])
    assert register.active()["human:alice@example.com"]["public_key"] == alice.public_key


def test_an_approver_can_always_withdraw_their_own_key(ws):
    from brevet.approvals import request_register_revoke
    alice, bob = _two_of_two(ws)
    req = request_register_revoke(ws["wd"], "human:bob@example.com")
    sign_request(ws["wd"], req["request_id"], bob)
    assert apply_if_ready(ws["wd"], req["request_id"])["status"] == "applied"
    register = register_from_chain(ws["ledger"])
    assert "human:bob@example.com" not in register.active()
    # the group keeps its threshold, so alice alone still cannot decide for it
    promo = request_promotion(ws["wd"], ws["store"], ws["rule"].capability_id, "promote",
                              approver="mission_group:quality", to_layer="controlled")
    sign_request(ws["wd"], promo["request_id"], alice)
    assert apply_if_ready(ws["wd"], promo["request_id"])["status"] == "pending"


def test_a_lost_key_outside_any_group_is_replaced_by_another_approver(ws):
    alice, _ = _register(ws, "human:alice@example.com")
    _register(ws, "human:bob@example.com", approver_key=alice)
    new_bob = create_key("human:bob@example.com", PASS, ws["tmp"] / "new-laptop")
    req = request_register_add(ws["wd"], new_bob)
    sign_request(ws["wd"], req["request_id"], alice)
    assert apply_if_ready(ws["wd"], req["request_id"])["status"] == "applied"
    register = register_from_chain(ws["ledger"])
    assert register.active()["human:bob@example.com"]["public_key"] == new_bob.public_key
    assert verify_approvals(ws["ledger"])["invalid"] == []


def test_an_unsigned_promotion_does_not_count_once_signing_is_on(ws):
    from brevet.lifecycle import build_lock
    from brevet.models import ValidationState
    _register(ws, "human:alice@example.com")
    ws["ledger"].append("brevet.promotion", {  # written to the chain without signatures
        "capability_id": ws["rule"].capability_id, "outcome": "promote",
        "approver": "human:alice@example.com", "to_layer": "advisory", "notes": "",
        "content_hash": ws["rule"].content_hash})
    forged = ws["rule"].model_copy(update={
        "validation_state": ValidationState.promoted_to_advisory,
        "authority_layer": AuthorityLayer.advisory})
    ws["store"].add(forged)
    with pytest.raises(ValueError, match="no matching promotion"):
        build_lock(AgentManifest(agent="a"), ws["store"], ledger=ws["ledger"])


def test_a_signed_register_change_applies_only_to_the_register_it_was_made_for(ws):
    from brevet.approvals import _approval, request_register_revoke
    group = "mission_group:quality"
    alice, _ = _register(ws, "human:alice@example.com", [group])
    early = sign_request(ws["wd"], request_register_revoke(
        ws["wd"], "human:alice@example.com")["request_id"], alice)
    with pytest.raises(PermissionError, match="last active approver"):
        apply_if_ready(ws["wd"], early["request_id"])
    _register(ws, "human:bob@example.com", [group], approver_key=alice)
    raise_to_two = request_threshold(ws["wd"], group, 2)
    sign_request(ws["wd"], raise_to_two["request_id"], alice)
    assert apply_if_ready(ws["wd"], raise_to_two["request_id"])["status"] == "applied"
    ws["ledger"].append("brevet.approver", _approval(early))  # the old signature, appended
    assert "human:alice@example.com" in register_from_chain(ws["ledger"]).active()
    assert "register changed" in verify_approvals(ws["ledger"])["invalid"][0]["problem"]


def test_a_register_change_cannot_be_carried_to_another_workspace(ws, tmp_path):
    from brevet.approvals import _approval, request_register_revoke
    alice, _ = _register(ws, "human:alice@example.com")
    _register(ws, "human:carol@example.com", approver_key=alice)
    other = tmp_path / "other" / ".brevet"
    for req in (request_register_add(other, alice),):
        assert apply_if_ready(other, req["request_id"])["status"] == "applied"
    bob = create_key("human:bob@example.com", PASS, ws["keys"])
    join = request_register_add(other, bob)
    sign_request(other, join["request_id"], alice)
    assert apply_if_ready(other, join["request_id"])["status"] == "applied"
    leave = sign_request(ws["wd"], request_register_revoke(
        ws["wd"], "human:alice@example.com")["request_id"], alice)
    assert apply_if_ready(ws["wd"], leave["request_id"])["status"] == "applied"
    Ledger(other / "ledger.jsonl").append("brevet.approver", _approval(leave))
    assert "human:alice@example.com" in register_from_chain(Ledger(other / "ledger.jsonl")).active()
