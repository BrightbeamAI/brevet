"""Eval runner, model assist, CHAP bridge, and MCP server surface."""

import json

import pytest

import brevet
from brevet.assist import NoModelAssist, OllamaAssist, from_name
from brevet.runner import EvalRunner


def _naive(task, context):
    return f"item: {task}\nseverity: minor\naction: log and monitor"


def _evolved(task, context):
    sev = "major" if "vibration" in task else "minor"
    act = "escalate to quality owner" if sev == "major" else "log and monitor"
    return f"item: {task}\nseverity: {sev}\naction: {act}"


def _workload(agent):
    tasks = [f"DEV-{i} pump vibration high during CIP" for i in range(4)]
    for t in tasks:
        r = agent.run(t, task_family="deviation_triage")
        agent.record_final(
            r.task_id,
            f"item: {t}\nseverity: major\naction: escalate to quality owner",
            participant="human:qa@x", rationale="seal-wear precursor",
            tags=["vibration-cip-underrated"])


def test_eval_runner_before_after_gate(tmp_path):
    agent = brevet.wrap(_naive, workdir=tmp_path / ".brevet")
    _workload(agent)
    summary = agent.dream()
    assert summary["candidates"] == 1 and summary["eval_cases"] == 4

    before = agent.evaluate()
    assert before["n_cases"] == 4
    assert before["held_in_pass_rate"] == 0.0 and before["held_out_pass_rate"] == 0.0

    agent.adapter.target = _evolved
    after = agent.evaluate()
    gate = EvalRunner.compare(before, after)
    assert gate["passed_gate"]
    assert any(True for _ in agent.ledger.read("brevet.eval_run"))


def test_full_lifecycle_via_agent_object(tmp_path):
    agent = brevet.wrap(_naive, workdir=tmp_path / ".brevet")
    _workload(agent)
    agent.dream()
    pending = agent.dawn()
    assert len(pending) == 5
    for cap in pending:
        agent.dawn(decide=(cap.capability_id, "promote"), approver="mission_group:rft")
    record = agent.release(to_version="0.2.0", channel="trial",
                           approver="mission_group:rft", delta_in=0.5, delta_out=0.0)
    assert record.to_version == "0.2.0" and len(record.promoted_capabilities) == 5
    # zero-config manifest on disk was updated and signed
    assert agent.manifest.signature is not None
    cand_id = record.promoted_capabilities[0]
    notice = agent.recall(cand_id, reason="wrong", issued_by="mission_group:rft")
    assert notice.affected_releases
    ok, _ = agent.verify()
    assert ok


def test_assist_none_and_ollama_fail_soft():
    assert isinstance(from_name("none"), NoModelAssist)
    bad = OllamaAssist(host="http://127.0.0.1:9", timeout=0.2)
    assert bad.draft("hello") is None  # no server: falls back, never raises


def test_assist_drafts_are_used_and_logged(tmp_path):
    class FakeAssist:
        provider, model = "fake", "fake-1"
        def draft(self, prompt):
            return "Always treat vibration during CIP as major."

    agent = brevet.wrap(_naive, workdir=tmp_path / ".brevet", assist=FakeAssist())
    _workload(agent)
    agent.dream()
    cand = next(c for c in agent.store.all().values()
                if c.kind.value == "prompt_rule")
    assert cand.content.startswith("Always treat vibration")
    assert cand.provenance.model_provider == "fake"
    assist_envs = list(agent.ledger.read("brevet.model_assist"))
    assert assist_envs and assist_envs[0]["body"]["human_review_required"] is True


def test_chap_bridge_queues_offline(tmp_path):
    from brevet.chap_bridge import CHAPDispatcher, dispatcher_from_ref
    assert dispatcher_from_ref("file:./x.jsonl", tmp_path) is None
    d = dispatcher_from_ref("chap:wsp_x@http://127.0.0.1:9", tmp_path)
    assert isinstance(d, CHAPDispatcher)
    ok = d.dispatch({"kind": "brevet.task", "body": {"a": 1}, "chain_hash": "sha256:00"})
    assert not ok and d.outbox.exists()
    sent, remaining = d.flush()
    assert sent == 0 and remaining == 1  # still offline, still queued


def test_chap_bridge_unconfigured_or_embedded(tmp_path):
    """chap:<ws> with no URL: embedded official coordinator when installed,
    otherwise local chain only."""
    from brevet.chap_bridge import EmbeddedCHAPDispatcher, dispatcher_from_ref
    d = dispatcher_from_ref("chap:wsp_x", tmp_path)
    try:
        import chap_coordinator  # noqa: F401
        assert isinstance(d, EmbeddedCHAPDispatcher)
    except ImportError:
        assert d is None


def test_chap_embedded_mirrors_through_official_coordinator(tmp_path):
    pytest.importorskip("chap_coordinator")
    from brevet.chap_bridge import EmbeddedCHAPDispatcher, _rpc
    d = EmbeddedCHAPDispatcher("wsp_brevet_test", tmp_path / "chap.db")
    ok = d.dispatch({"kind": "brevet.override", "refs": ["tsk_1"],
                     "body": {"rationale": "seal-wear precursor"},
                     "chain_hash": "sha256:0f"})
    assert ok
    audit = d.coordinator.dispatch(
        _rpc("audit.read", {"workspace": "wsp_brevet_test"}))
    entries = audit["result"]["entries"]
    # workspace.create + participant.join + task.create + task.complete
    assert len(entries) >= 4
    methods = [e["envelope"].get("method") for e in entries]
    assert "task.create" in methods and "task.complete" in methods


def test_mcp_server_exposes_lifecycle(tmp_path):
    pytest.importorskip("mcp")
    import asyncio

    from brevet.mcp_server import build_server
    server = build_server(str(tmp_path / ".brevet"), str(tmp_path / "agent.yaml"))
    tools = {t.name for t in asyncio.run(server.list_tools())}
    assert {"brevet_status", "brevet_dream", "brevet_dawn_pending", "brevet_dawn_decide",
            "brevet_release", "brevet_recall", "brevet_verify"} <= tools
    # status tool runs against an empty workdir without error
    result = asyncio.run(server.call_tool("brevet_verify", {}))
    # unwrap across SDK result shapes: 2.x CallToolResult.content,
    # 1.x (content, meta) tuples, or a bare content list
    content = getattr(result, "content", result)
    if isinstance(content, tuple):
        content = content[0]
    payload = json.loads(content[0].text)
    assert payload["chain_ok"] is True
