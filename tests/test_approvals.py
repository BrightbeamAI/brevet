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


def test_brevet_active_refuses_when_an_approval_is_forged(ws):
    pytest.importorskip("mcp")
    from brevet.mcp_server import build_server
    (ws["tmp"] / "agent.yaml").write_text(yaml.safe_dump({"agent": "t", "version": "0.1.0"}))
    server = build_server(str(ws["wd"]), str(ws["tmp"] / "agent.yaml"))
    _register(ws, "human:alice@example.com")
    ws["ledger"].append("brevet.promotion", {
        "capability_id": ws["rule"].capability_id, "outcome": "promote",
        "approver": "human:alice@example.com", "to_layer": "advisory", "notes": "",
        "content_hash": ws["rule"].content_hash})  # unsigned, after signing was required
    assert "error" in _call(server, "brevet_active")


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
