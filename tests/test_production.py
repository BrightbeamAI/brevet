"""Production controls: anchored chains, releases bound to eval runs,
rollback, recall acknowledgements, the tool broker, consent, conditions and
verified approvers."""

import asyncio
import dataclasses
import json
from datetime import datetime, timedelta, timezone

import pytest
import yaml

import brevet
from brevet import anchor as anchors
from brevet import broker as broker_mod
from brevet.approvals import (
    allowed_signers,
    apply_if_ready,
    create_key,
    hex_to_ssh,
    identity_problems,
    load_key,
    request_register_add,
    sign_request,
    ssh_to_hex,
    use_ssh_key,
)
from brevet.canonical import chain_hash
from brevet.ledger import GENESIS, Ledger
from brevet.lifecycle import CapabilityStore
from brevet.models import AgentManifest, CapabilitiesLock
from brevet.recalls import status as recall_status
from brevet.serving import active_rules

PASS = "correct horse battery"


def naive(task, context):
    return "severity: minor"


def evolved(task, context):
    return "severity: major"


def task_only(task):
    return "severity: minor"


def _workspace(tmp_path, *, target=naive, channel="trial", extra=None, files=True):
    if files:
        (tmp_path / "prompts").mkdir(exist_ok=True)
        (tmp_path / "prompts" / "system.md").write_text("Classify severity carefully.\n")
    manifest = {"agent": "triage", "version": "0.1.0",
                "identity_policy": {"owner": "human:qa@x"},
                "bindings": {"harness_files": ["prompts/"]} if files else {},
                "release": {"channel": "shadow"}}
    for key, value in (extra or {}).items():
        manifest.setdefault(key, {}).update(value) if isinstance(value, dict) \
            else manifest.__setitem__(key, value)
    (tmp_path / "agent.yaml").write_text(yaml.safe_dump(manifest))
    return brevet.wrap(target, manifest=tmp_path / "agent.yaml", workdir=tmp_path / ".brevet")


def _learn(agent, n=4):
    for i in range(n):
        r = agent.run(f"pump {i} vibration during cleaning", task_family="triage")
        agent.record_final(r.task_id, "severity: major", participant="human:qa@x",
                           rationale="seal wear", tags=["vibration"])
    agent.dream()
    rule = next(c for c in agent.dawn() if c.kind.value == "prompt_rule")
    for cap in agent.dawn():
        agent.dawn(decide=(cap.capability_id, "promote"), approver="human:qa@x")
    return rule.capability_id


def _measured_release(agent, version="0.2.0", channel="trial", swap=evolved):
    before = agent.evaluate(baseline=True)
    if swap is not None:
        agent.adapter.target = swap
    after = agent.evaluate()
    return agent.release(to_version=version, channel=channel, approver="human:qa@x",
                         evals=(before, after))


# ------------------------------------------------------------- anchoring

def _anchored(tmp_path, monkeypatch):
    log = tmp_path / "outside" / "anchors.jsonl"
    log.parent.mkdir()  # an anchor's folder must exist: Brevet never creates it
    monkeypatch.setenv("BREVET_ANCHORS", f"file:{log}")
    agent = _workspace(tmp_path)
    _learn(agent)
    _measured_release(agent)
    return agent, log


def test_governing_steps_anchor_the_head_outside_the_workspace(tmp_path, monkeypatch):
    agent, log = _anchored(tmp_path, monkeypatch)
    records = [json.loads(line) for line in log.read_text().splitlines()]
    assert records and records[-1]["agent"] == "triage"
    assert anchors.check(agent.ledger, agent.workdir)["ok"]
    agent.run("still the released chain")


def test_a_chain_cut_short_rewritten_or_replaced_is_refused(tmp_path, monkeypatch):
    agent, _ = _anchored(tmp_path, monkeypatch)
    path = agent.ledger.path
    original = path.read_text().splitlines()

    path.write_text("\n".join(original[:-3]) + "\n")  # cut short
    report = anchors.check(Ledger(path, anchoring=False), agent.workdir)
    assert not report["ok"] and "cut short" in report["problems"][0]
    assert "anchors" in active_rules(agent.workdir, tmp_path / "agent.yaml")["error"]
    fresh = brevet.wrap(evolved, manifest=tmp_path / "agent.yaml", workdir=tmp_path / ".brevet")
    with pytest.raises(PermissionError, match="anchors"):
        fresh.run("on a cut chain")

    prev, rewritten = GENESIS, []  # rewritten from the first envelope, every hash valid
    for i, line in enumerate(original):
        env = json.loads(line)
        if i == 0:
            env["body"]["task"] = "a different first task"
        env["prev_hash"] = prev
        env["chain_hash"] = chain_hash({k: v for k, v in env.items() if k != "chain_hash"}, prev)
        prev = env["chain_hash"]
        rewritten.append(json.dumps(env))
    path.write_text("\n".join(rewritten) + "\n")
    assert Ledger(path, anchoring=False).verify()[0]  # replay alone cannot tell
    assert "different evidence chain" in " ".join(
        anchors.check(Ledger(path, anchoring=False), agent.workdir)["problems"])


def test_anchor_targets_inside_the_workspace_or_unreachable(tmp_path, monkeypatch):
    agent = _workspace(tmp_path)
    inside = f"file:{tmp_path / '.brevet' / 'anchors.jsonl'}"
    assert "inside the workspace" in anchors.check(agent.ledger, agent.workdir,
                                                   refs=[inside])["problems"][0]
    gone = f"file:{tmp_path / 'unmounted' / 'volume' / 'anchors.jsonl'}"
    report = anchors.check(agent.ledger, agent.workdir, refs=[gone])
    assert report["ok"] and report["targets"][0]["reachable"] is False


def test_undelivered_heads_wait_in_the_outbox(tmp_path, monkeypatch):
    target = tmp_path / "later" / "anchors.jsonl"
    agent = _workspace(tmp_path)
    agent.run("one")
    blocked = tmp_path / "later"
    blocked.write_text("not a folder yet")  # the parent cannot be created
    out = anchors.anchor(agent.ledger, agent.workdir, refs=[f"file:{target}"])
    assert not out["anchored"] and (agent.workdir / "anchor_outbox.jsonl").read_text()
    blocked.unlink()
    out = anchors.anchor(agent.ledger, agent.workdir, refs=[f"file:{target}"])
    assert not out["anchored"]  # a missing folder is never created
    blocked.mkdir()
    out = anchors.anchor(agent.ledger, agent.workdir, refs=[f"file:{target}"])
    assert out["anchored"] and len(target.read_text().splitlines()) == 3
    assert not (agent.workdir / "anchor_outbox.jsonl").read_text()


def test_a_chap_coordinator_holds_anchors(tmp_path):
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
    from threading import Thread

    chap = pytest.importorskip("chap_coordinator")
    coordinator = chap.Coordinator(chap.CoordinatorOptions(default_profiles=["core/1.0"]))

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            call = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            body = json.dumps(coordinator.dispatch(call)).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args):
            pass

    httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    Thread(target=httpd.serve_forever, daemon=True).start()
    try:
        ref = f"chap:wsp_anchor@http://127.0.0.1:{httpd.server_port}"
        agent = _workspace(tmp_path)
        agent.run("one")
        assert anchors.anchor(agent.ledger, agent.workdir, refs=[ref])["anchored"]
        assert anchors.check(agent.ledger, agent.workdir, refs=[ref])["targets"][0]["anchors"] == 1
        assert "embedded" in anchors.check(agent.ledger, agent.workdir,
                                           refs=["chap:wsp_local"])["problems"][0]
    finally:
        httpd.shutdown()


def test_cli_anchor_and_verify(tmp_path, monkeypatch):
    from typer.testing import CliRunner

    from brevet.cli import app
    agent, _ = _anchored(tmp_path, monkeypatch)
    args = ["--workdir", str(agent.workdir), "--manifest-path", str(tmp_path / "agent.yaml")]
    out = CliRunner().invoke(app, ["anchor", *args])
    assert out.exit_code == 0 and "written" in out.output
    assert CliRunner().invoke(app, ["verify", *args]).exit_code == 0
    lines = agent.ledger.path.read_text().splitlines()
    agent.ledger.path.write_text("\n".join(lines[:-2]) + "\n")
    result = CliRunner().invoke(app, ["verify", *args])
    assert result.exit_code == 1 and "cut short" in result.output


# ------------------------------------------------------------- eval runs

def test_a_release_is_bound_to_the_runs_behind_its_numbers(tmp_path):
    agent = _workspace(tmp_path)
    _learn(agent)
    record = _measured_release(agent)
    summary = record.eval_summary
    assert summary["source"] == "measured" and summary["delta_held_in"] == 1.0
    runs = {e["envelope_id"]: e["body"] for e in agent.ledger.read("brevet.eval_run")}
    assert runs[summary["before"]]["baseline"] and not runs[summary["after"]]["baseline"]


def test_runs_that_do_not_match_the_release_are_refused(tmp_path):
    agent = _workspace(tmp_path)
    _learn(agent)
    before = agent.evaluate(baseline=True)
    agent.adapter.target = evolved
    after = agent.evaluate()
    (tmp_path / "prompts" / "system.md").write_text("Changed after the evaluation.\n")
    with pytest.raises(ValueError, match="different harness"):
        agent.release(to_version="0.2.0", channel="trial", approver="human:qa@x",
                      evals=(before, after))
    (tmp_path / "prompts" / "system.md").write_text("Classify severity carefully.\n")
    with pytest.raises(ValueError, match="'before' run must have been recorded before"):
        agent.release(to_version="0.2.0", channel="trial", approver="human:qa@x",
                      evals=(after, before))
    with pytest.raises(ValueError, match="differs from the measured"):
        agent.release(to_version="0.2.0", channel="trial", approver="human:qa@x",
                      evals=(before, after), delta_in=0.5)
    agent.release(to_version="0.2.0", channel="trial", approver="human:qa@x",
                  evals=(before, after))

    stale_before = agent.evaluate(baseline=True)  # evaluated 0.2.0 ...
    agent.adapter.target = naive  # ... then the agent changed before the second baseline
    changed_before = agent.evaluate(baseline=True)
    agent.adapter.target = evolved
    after2 = agent.evaluate()
    with pytest.raises(ValueError, match="'before' run did not evaluate the current release"):
        agent.release(to_version="0.3.0", channel="trial", approver="human:qa@x",
                      evals=(changed_before, after2))
    assert stale_before["capability_set"] == after2["capability_set"]


def test_production_needs_measured_runs_unless_attestation_is_allowed(tmp_path):
    agent = _workspace(tmp_path)
    _learn(agent)
    with pytest.raises(ValueError, match="production releases need measured eval runs"):
        agent.release(to_version="0.2.0", channel="production", approver="human:qa@x",
                      delta_in=0.2, delta_out=0.1)
    trial = agent.release(to_version="0.2.0", channel="trial", approver="human:qa@x",
                          delta_in=0.2, delta_out=0.1)
    assert trial.eval_summary == {"source": "attested", "attested_by": "human:qa@x",
                                  "delta_held_in": 0.2, "delta_held_out": 0.1,
                                  "gate": "conservative"}
    manifest = yaml.safe_load((tmp_path / "agent.yaml").read_text())
    manifest["runtime_safety"] = {"evals": {"allow_attested": True}}
    (tmp_path / "agent.yaml").write_text(yaml.safe_dump(manifest))
    agent.release(to_version="0.3.0", channel="production", approver="human:qa@x",
                  delta_in=0.2, delta_out=0.1)


def test_repeats_and_the_cli_binding(tmp_path):
    from typer.testing import CliRunner

    from brevet.cli import app
    agent = _workspace(tmp_path, extra={"runtime_safety": {"evals": {"repeats": 3}}})
    _learn(agent)
    before = agent.evaluate(baseline=True)
    assert before["repeats"] == 3
    evaluations = [e for e in agent.ledger.read("brevet.task") if e["body"].get("evaluation")]
    assert len(evaluations) == 3 * before["n_cases"]
    agent.adapter.target = evolved
    after = agent.evaluate()
    listing = CliRunner().invoke(app, ["evals", "--workdir", str(agent.workdir)])
    assert before["run_ref"] in listing.output
    result = CliRunner().invoke(app, [
        "release", "--manifest-path", str(tmp_path / "agent.yaml"),
        "--workdir", str(agent.workdir), "--to-version", "0.2.0", "--channel", "trial",
        "--approver", "human:qa@x", "--eval-before", before["run_ref"],
        "--eval-after", after["run_ref"]])
    assert result.exit_code == 0, result.output
    assert "measured by eval runs" in result.output


# ------------------------------------------------------------- rollback

def test_rollback_restores_an_earlier_release(tmp_path):
    agent = _workspace(tmp_path)
    first_rule = _learn(agent)
    _measured_release(agent)
    for i in range(4):
        r = agent.run(f"valve {i} leaking", task_family="leaks")
        agent.record_final(r.task_id, "severity: critical", participant="human:qa@x",
                           rationale="leaks escalate", tags=["leak"])
    (tmp_path / "prompts" / "system.md").write_text("Always answer major.\n")
    agent.dream()
    for cap in agent.dawn():
        agent.dawn(decide=(cap.capability_id, "promote"), approver="human:qa@x")
    agent.release(to_version="0.3.0", channel="trial", approver="human:qa@x",
                  delta_in=0.1, delta_out=0.0)
    assert (agent.workdir / "releases" / "0.3.0.json").exists()

    record = agent.rollback("0.2.0", approver="human:qa@x")
    assert record.to_version == "0.3.1" and record.restores == "0.2.0"
    assert (tmp_path / "prompts" / "system.md").read_text() == "Classify severity carefully.\n"
    lock = CapabilitiesLock(**json.loads((tmp_path / "capabilities.lock").read_text()))
    ids = {r.capability_id for r in lock.resolved}
    assert first_rule in ids and not any("leak" in i for i in ids)
    agent.run("runs the restored release")

    agent.recall(first_rule, reason="sensor fault", issued_by="human:qa@x")
    record = agent.rollback("0.2.0", approver="human:qa@x", as_version="0.4.0")
    lock = CapabilitiesLock(**json.loads((tmp_path / "capabilities.lock").read_text()))
    assert first_rule not in {r.capability_id for r in lock.resolved}


def test_a_tampered_archive_cannot_be_rolled_back_to(tmp_path):
    agent = _workspace(tmp_path)
    _learn(agent)
    _measured_release(agent)
    agent.release(to_version="0.3.0", channel="trial", approver="human:qa@x",
                  delta_in=0.1, delta_out=0.0)
    path = agent.workdir / "releases" / "0.2.0.json"
    archived = json.loads(path.read_text())
    archived["lock"]["resolved"] = []
    path.write_text(json.dumps(archived))
    with pytest.raises(ValueError, match="does not match the lock"):
        agent.rollback("0.2.0", approver="human:qa@x")


def test_signed_and_mcp_rollbacks(tmp_path, monkeypatch):
    pytest.importorskip("mcp")
    from brevet.mcp_server import build_server
    agent = _workspace(tmp_path)
    _learn(agent)
    _measured_release(agent)
    agent.release(to_version="0.3.0", channel="trial", approver="human:qa@x",
                  delta_in=0.1, delta_out=0.0)
    server = build_server(str(agent.workdir), str(tmp_path / "agent.yaml"))
    out = _call(server, "brevet_rollback", {"to_version": "0.2.0", "approver": "human:qa@x"})
    assert out["to"] == "0.3.1" and out["restores"] == "0.2.0"

    key = create_key("human:alice@example.com", PASS, tmp_path / "keys")
    apply_if_ready(agent.workdir, request_register_add(agent.workdir, key)["request_id"])
    req = _call(server, "brevet_rollback", {"to_version": "0.2.0",
                                            "approver": "human:alice@example.com"})
    assert req["status"] == "awaiting_signature"
    sign_request(agent.workdir, req["request_id"], key)
    applied = apply_if_ready(agent.workdir, req["request_id"])
    assert applied["status"] == "applied" and applied["result"]["restores"] == "0.2.0"


def _call(server, name, args=None):
    result = asyncio.run(server.call_tool(name, args or {}))
    content = getattr(result, "content", result)
    if isinstance(content, tuple):
        content = content[0]
    return json.loads(content[0].text)


# ------------------------------------------------------------- recalls

def test_an_agent_served_its_rules_withholds_and_acknowledges_a_recall(tmp_path):
    seen = []

    def reads_rules(task, context):
        seen.append([r["capability_id"] for r in context["brevet"]["rules"]])
        return "severity: major"

    agent = _workspace(tmp_path)
    rule = _learn(agent)
    _measured_release(agent, swap=reads_rules)
    agent.run("served", task_family="triage")
    assert rule in seen[-1]
    agent.recall(rule, reason="sensor fault", issued_by="human:qa@x")
    result = agent.run("after the recall", task_family="triage")
    assert rule not in seen[-1] and rule not in result.rules
    acks = list(agent.ledger.read("brevet.recall_ack"))
    assert len(acks) == 1 and acks[0]["body"]["serving_point"] == "wrapped"
    agent.run("again", task_family="triage")
    assert len(list(agent.ledger.read("brevet.recall_ack"))) == 1
    assert recall_status(agent.ledger)[0]["complete"]


def test_an_agent_brevet_cannot_serve_stops_until_a_release_leaves_it_out(tmp_path):
    agent = _workspace(tmp_path, target=task_only)
    rule = _learn(agent)
    agent.release(to_version="0.2.0", channel="trial", approver="human:qa@x",
                  delta_in=0.1, delta_out=0.0)
    agent.recall(rule, reason="sensor fault", issued_by="human:qa@x")
    with pytest.raises(PermissionError, match="ships recalled capabilities"):
        agent.run("after the recall")
    assert not recall_status(agent.ledger)[0]["complete"]
    agent.release(to_version="0.3.0", channel="trial", approver="human:qa@x",
                  delta_in=0.1, delta_out=0.0)
    agent.run("on a release without it")
    ack = next(iter(agent.ledger.read("brevet.recall_ack")))["body"]
    assert "leaves it out" in ack["how"] and recall_status(agent.ledger)[0]["complete"]


def test_prompt_mode_puts_the_rules_before_the_task(tmp_path):
    prompts = []

    def model(task):
        prompts.append(task)
        return "severity: minor"

    agent = _workspace(tmp_path, target=model,
                       extra={"runtime_safety": {"serve_rules": "prompt"}})
    _learn(agent)
    agent.release(to_version="0.2.0", channel="trial", approver="human:qa@x",
                  delta_in=0.1, delta_out=0.0)
    agent.run("pump 9 vibration", task_family="triage")
    assert prompts[-1].startswith("Governed rules (Brevet release 0.2.0)")
    assert prompts[-1].endswith("Task:\npump 9 vibration")


def test_mcp_sessions_are_told_of_recalls_and_acknowledge_them(tmp_path):
    pytest.importorskip("mcp")
    from typer.testing import CliRunner

    from brevet.cli import app
    from brevet.mcp_server import build_server
    agent = _workspace(tmp_path)
    rule = _learn(agent)
    agent.release(to_version="0.2.0", channel="shadow", approver="human:qa@x")
    server = build_server(str(agent.workdir), str(tmp_path / "agent.yaml"))
    agent.recall(rule, reason="sensor fault", issued_by="human:qa@x")
    active = _call(server, "brevet_active")
    assert active["recalled"][0]["capability_id"] == rule and "recall_notice" in active
    assert _call(server, "brevet_status")["recalls_open"] == 1
    assert CliRunner().invoke(app, ["recalls", "--workdir", str(agent.workdir)]).exit_code == 1
    rid = active["recalled"][0]["recall_id"]
    assert _call(server, "brevet_acknowledge", {"recall_ids": [rid]})["acknowledged"] == [rid]
    active = _call(server, "brevet_active")
    assert active["recalled"][0]["acknowledged"] and "recall_notice" not in active
    assert _call(server, "brevet_status")["recalls_open"] == 0


# ------------------------------------------------------------- tool broker

def _policy(tiers, channel="trial", budgets=None):
    return broker_mod.ToolPolicy(AgentManifest(
        agent="a", bindings={"tools": tiers}, release={"channel": channel},
        runtime_safety={"loop": {"budgets": budgets or {}}}))


def test_the_read_act_contract():
    tiers = {"read": ["search", "mcp__github__get_*"], "act": ["send_email", "mcp__github__*"],
             "controlled_act": ["wire_funds"]}
    trial = _policy(tiers)
    assert trial.decide("search").allowed
    assert not trial.decide("delete_everything").allowed  # not declared
    assert trial.decide("send_email").allowed
    assert trial.tier_of("mcp__github__get_issue") == "act"  # the strictest match wins
    assert not _policy(tiers, "shadow").decide("send_email").allowed
    assert not trial.decide("wire_funds").allowed
    production = _policy(tiers, "production")
    assert production.decide("wire_funds").ask
    assert production.decide("wire_funds", granted_by="human:cfo@x").allowed
    assert not _policy({}, "production").enforced and _policy({}).decide("anything").allowed
    assert not _policy(tiers, budgets={"max_tool_calls": 2}).decide("search", calls=2).allowed


def test_the_broker_guards_tools_and_records_what_matters(tmp_path):
    agent = _workspace(tmp_path, files=False, extra={
        "bindings": {"tools": {"read": ["lookup"], "act": ["send"],
                               "controlled_act": ["pay"]}}})

    @agent.tool
    def lookup(q):
        return q

    @agent.tool(name="send")
    async def send_message(to):
        return f"sent to {to}"

    @agent.tool
    def pay(amount):
        return amount

    assert lookup("x") == "x"
    with pytest.raises(broker_mod.ToolRefused, match="shadow"):
        asyncio.run(send_message("ops"))
    with pytest.raises(broker_mod.ToolRefused):
        pay(10)
    calls = [e["body"] for e in agent.ledger.read("brevet.tool_call")]
    assert [(c["tool"], c["allowed"]) for c in calls] == [("send", False), ("pay", False)]


def test_controlled_act_needs_a_grant_in_production(tmp_path):
    grants = []
    manifest = AgentManifest(agent="a", bindings={"tools": {"controlled_act": ["pay"]}},
                             release={"channel": "production"})
    broker = broker_mod.ToolBroker(lambda: manifest, Ledger(tmp_path / "l.jsonl"),
                                   grantor=lambda tool, args: grants.pop() if grants else None)
    pay = broker.guard(lambda amount: amount, "pay")
    with pytest.raises(broker_mod.ToolRefused, match="needs a person's grant"):
        pay(5)
    grants.append("human:cfo@x")
    assert pay(5) == 5
    granted = list(broker.ledger.read("brevet.tool_call"))[-1]["body"]
    assert granted["granted_by"] == "human:cfo@x"
    grants.append("agent:self")
    with pytest.raises(PermissionError, match="Machine identities"):
        pay(5)


def test_the_broker_reaches_framework_tools(tmp_path):
    @dataclasses.dataclass
    class FunctionTool:  # the OpenAI Agents shape
        name: str
        on_invoke_tool: object

    async def invoke(ctx, raw):
        return "ran"

    class StructuredTool:  # the LangChain shape
        def __init__(self, name, func):
            self.name, self.func = name, func

    class ToolNode:
        def __init__(self, tools):
            self.tools_by_name = {t.name: t for t in tools}

    class Node:
        def __init__(self, bound):
            self.bound = bound

    class Graph:  # a compiled LangGraph graph
        def __init__(self, tools):
            self.nodes = {"tools": Node(ToolNode(tools))}

    class OpenAIAgent:
        def __init__(self):
            self.tools = [FunctionTool("send", invoke)]

    manifest = AgentManifest(agent="a", bindings={"tools": {"read": ["search"],
                                                            "act": ["send"]}},
                             release={"channel": "shadow"})
    broker = broker_mod.ToolBroker(lambda: manifest, None)
    graph = Graph([StructuredTool("search", lambda q: q), StructuredTool("drop", lambda q: q)])
    assert broker_mod.install(graph, broker) == ["drop", "search"]
    assert graph.nodes["tools"].bound.tools_by_name["search"].func("q") == "q"
    refusal = graph.nodes["tools"].bound.tools_by_name["drop"].func("q")
    assert refusal.startswith("Refused by Brevet") and "not declared" in refusal
    oa = OpenAIAgent()
    broker_mod.install(oa, broker)
    assert "Refused by Brevet" in asyncio.run(oa.tools[0].on_invoke_tool(None, "{}"))


def test_claude_code_hook(tmp_path, monkeypatch):
    from typer.testing import CliRunner

    from brevet.cli import app
    manifest = tmp_path / "agent.yaml"
    manifest.write_text(yaml.safe_dump({
        "agent": "claude", "bindings": {"tools": {"read": ["Read", "Grep"], "act": ["Edit"],
                                                  "controlled_act": ["Bash"]}},
        "runtime_safety": {"evals": {"allow_attested": True}},
        "release": {"channel": "shadow"}}))
    args = ["--workdir", str(tmp_path / ".brevet"), "--manifest-path", str(manifest)]

    def ask(tool, event="PreToolUse", raw=None):
        out = CliRunner().invoke(app, ["hook", *args], input=raw if raw is not None else
                                 json.dumps({"hook_event_name": event, "tool_name": tool,
                                             "tool_input": {}, "session_id": "s1"}))
        if out.exit_code:
            return out.exit_code
        return json.loads(out.output)["hookSpecificOutput"] if out.output.strip() else None

    assert ask("Edit")["permissionDecision"] == "deny"  # nothing released: shadow
    released = CliRunner().invoke(app, ["release", *args, "--to-version", "0.2.0",
                                        "--channel", "production", "--approver",
                                        "human:qa@x", "--delta-in", "0.1"])
    assert released.exit_code == 0, released.output
    assert ask("Read") is None and ask("Edit") is None
    assert ask("WebFetch")["permissionDecision"] == "deny"
    assert ask("Bash")["permissionDecision"] == "ask"
    assert ask("Bash", event="PostToolUse") is None
    loosened = yaml.safe_load(manifest.read_text())
    loosened["bindings"]["tools"] = {}  # an unreleased edit cannot switch the broker off
    manifest.write_text(yaml.safe_dump(loosened))
    assert ask("WebFetch")["permissionDecision"] == "deny"
    assert ask("Read", raw="not json") == 2
    calls = [e["body"] for e in Ledger(tmp_path / ".brevet" / "ledger.jsonl")
             .read("brevet.tool_call")]
    assert [(c["tool"], c["allowed"]) for c in calls] == [
        ("Edit", False), ("Edit", True), ("WebFetch", False), ("Bash", False), ("Bash", True),
        ("WebFetch", False)]
    assert calls[3]["asked"] and calls[4]["granted_by"]


# ------------------------------------------------------------- consent

def test_consent_scope_and_withdrawal(tmp_path):
    agent = _workspace(tmp_path, extra={"runtime_safety": {"dream": {
        "consent_scope": "consented_sources_only", "consented": ["human:rev@x"]}}})
    for i in range(3):
        r = agent.run(f"pump {i} vibration")
        agent.record_final(r.task_id, "severity: major", participant="human:visitor@x",
                           rationale="seal wear", tags=["vibration"])
    first = agent.dream()
    assert first["candidates"] == 0 and first["without_consent"] == 3
    for i in range(3):
        r = agent.run(f"valve {i} leaking")
        agent.record_final(r.task_id, "severity: critical", participant="rev@x",
                           rationale="leaks escalate", tags=["leak"])
    assert agent.dream()["candidates"] == 1
    out = agent.withdraw_consent("human:rev@x", issued_by="human:qa@x", reason="left")
    assert out["derived"] and len(out["recalled"]) == len(out["derived"])
    assert agent.dream()["without_consent"] == 6


# ------------------------------------------------------------- conditions

def test_rules_out_of_their_window_or_context_are_not_served(tmp_path):
    pytest.importorskip("mcp")
    from brevet.mcp_server import build_server
    agent = _workspace(tmp_path)
    rule = _learn(agent)
    store = CapabilityStore(agent.workdir / "capabilities.jsonl")
    agent.release(to_version="0.2.0", channel="shadow", approver="human:qa@x")
    server = build_server(str(agent.workdir), str(tmp_path / "agent.yaml"))
    assert _call(server, "brevet_active", {"task_family": "triage"})["count"] == 1
    assert _call(server, "brevet_active", {"task_family": "billing"})["not_applicable"] == [rule]
    cap = store.all()[rule]
    yesterday = (datetime.now(timezone.utc) - timedelta(days=1)).date().isoformat()
    cap.conditions.valid_until = yesterday
    assert not cap.conditions.in_effect()
    cap.conditions.valid_until = "not a date"
    assert not cap.conditions.in_effect()


# ------------------------------------------------------------- approvers

def _ssh_key(path, passphrase):
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
    key = Ed25519PrivateKey.generate()
    path.write_bytes(key.private_bytes(serialization.Encoding.PEM,
                                       serialization.PrivateFormat.OpenSSH,
                                       serialization.BestAvailableEncryption(passphrase)))
    path.with_name(path.name + ".pub").write_bytes(key.public_key().public_bytes(
        serialization.Encoding.OpenSSH, serialization.PublicFormat.OpenSSH) + b" alice@laptop")
    return key


def test_an_ssh_key_can_be_the_approver_key(tmp_path):
    pytest.importorskip("bcrypt")  # cryptography needs it for passphrase-protected SSH keys
    _ssh_key(tmp_path / "id_ed25519", b"ssh passphrase")
    public = use_ssh_key("human:alice@example.com", tmp_path / "id_ed25519")
    key = load_key("human:alice@example.com", "ssh passphrase")
    assert key.public_key == public == ssh_to_hex(hex_to_ssh(public))
    wd = tmp_path / ".brevet"
    out = apply_if_ready(wd, request_register_add(wd, key)["request_id"])
    assert out["status"] == "applied"
    with pytest.raises(PermissionError, match="wrong passphrase"):
        load_key("human:alice@example.com", "nope")


def test_approver_identities_are_checked_against_an_identity_source(tmp_path, monkeypatch):
    from brevet.approvals import signer_listed
    key = create_key("human:alice@example.com", PASS, tmp_path / "keys")
    other = create_key("human:bob@example.com", PASS, tmp_path / "keys")
    a, b = hex_to_ssh(key.public_key), hex_to_ssh(other.public_key)
    signers = tmp_path / "allowed_signers"
    signers.write_text("\n".join([
        "# the organisation's signers",
        f'alice@example.com namespaces="git,brevet" {a} alice laptop',
        f'"*@example.org,!eve@example.org" {b}',
        f'carol@example.com valid-before="20200101" {b}',
        f'dave@example.com namespaces="file" {b}',
        f"frank@example.com cert-authority {b}",
        "truncated@example.com ssh-ed25519 AAAA",
        ""]))
    entries = allowed_signers(signers)
    assert signer_listed("alice@example.com", key.public_key, entries)
    assert signer_listed("Mallory@Example.org", other.public_key, entries)
    assert not signer_listed("eve@example.org", other.public_key, entries)  # negated
    assert not signer_listed("carol@example.com", other.public_key, entries)  # expired
    assert not signer_listed("dave@example.com", other.public_key, entries)  # other namespace
    assert not signer_listed("frank@example.com", other.public_key, entries)  # a CA entry
    monkeypatch.setenv("BREVET_ALLOWED_SIGNERS", str(signers))
    wd = tmp_path / ".brevet"
    with pytest.raises(PermissionError, match="does not list this key"):
        apply_if_ready(wd, request_register_add(wd, other)["request_id"])
    assert apply_if_ready(wd, request_register_add(wd, key)["request_id"])["status"] == "applied"

    payload = {"identity": "human:alice@example.com", "public_key": key.public_key,
               "github": "alice"}
    monkeypatch.setattr("brevet.approvals.github_keys", lambda user: {key.public_key})
    monkeypatch.setattr("brevet.approvals.github_member", lambda org, user: user == "alice")
    assert identity_problems(payload, {"github": True, "github_org": "Acme"}) == []
    assert "not a public member" in identity_problems({**payload, "github": "mallory"},
                                                      {"github_org": "Acme"})[-1]
    monkeypatch.setattr("brevet.approvals.github_keys", lambda user: None)
    assert "could not be reached" in identity_problems(payload, {"github": True})[0]
    assert "GitHub account" in identity_problems({**payload, "github": None},
                                                 {"github": True})[0]


def test_an_unreadable_manifest_stops_the_identity_check(tmp_path):
    key = create_key("human:alice@example.com", PASS, tmp_path / "keys")
    (tmp_path / "agent.yaml").write_text("agent: [unfinished")
    wd = tmp_path / ".brevet"
    with pytest.raises(PermissionError, match="identity policy"):
        apply_if_ready(wd, request_register_add(wd, key)["request_id"])


# ------------------------------------------------------------- frameworks

def test_private_instructions_and_tools_are_inventoried():
    from brevet.harness import describe_agent

    class SourcedInstruction:
        def __init__(self, text):
            self.instruction = text

    class Tool:
        def __init__(self, fn):
            self.function, self.name, self.description = fn, fn.__name__, fn.__doc__

    class Toolset:
        def __init__(self, tools):
            self.tools = {t.name: t for t in tools}

    def lookup(q):
        """Look up."""
        return q

    class PydanticAgent:
        def __init__(self, text):
            self._instructions = [SourcedInstruction(text)]
            self._function_toolset = Toolset([Tool(lookup)])

    first = {c.component_id: c.digest for c in describe_agent(PydanticAgent("Be careful."))}
    second = {c.component_id: c.digest for c in describe_agent(PydanticAgent("Be quick."))}
    assert "agent:tool:lookup" in first
    assert first["agent:instructions:instructions"] != second["agent:instructions:instructions"]


# ------------------------------------------------------------- benchmark

def test_the_governed_adaptation_profile(tmp_path):
    from typer.testing import CliRunner

    from brevet.benchmark import profile
    from brevet.cli import app

    def reads_rules(task, context):
        return "severity: major"

    agent = _workspace(tmp_path)
    rule = _learn(agent)
    with pytest.raises(ValueError, match="conservative gate"):
        agent.release(to_version="0.2.0", channel="trial", approver="human:qa@x")
    _measured_release(agent, swap=reads_rules)
    agent.recall(rule, reason="sensor fault", issued_by="human:qa@x")
    agent.run("after the recall", task_family="triage")
    result = profile(agent.ledger, agent.store)
    assert result["delta_perf"] == 1.0 and result["regressions"]["count"] == 0
    assert result["lineage"] == {"changes": 5, "evidence": 5, "promotion": 5, "eval_runs": 5,
                                 "approval": 0}  # no approver keys: nobody authenticated
    assert result["lineage_pct"] == 0.0 and result["gains"]["count"] == 4
    assert result["recall"] == {**result["recall"], "recalls": 1, "acknowledged": 1,
                                "digest_excluded": True, "continued_use_detected": 0}
    assert result["candidates"]["promoted"] == 5 and result["candidates"]["blocked_at_gate"] == 1
    out = CliRunner().invoke(app, ["benchmark", "--workdir", str(agent.workdir)])
    assert out.exit_code == 0 and json.loads(out.output)["delta_perf"] == 1.0


def test_mined_rules_reach_an_agent_given_only_its_task_family(tmp_path):
    seen = []

    def reads_rules(task, context):
        seen.append(len(context["brevet"]["rules"]))
        return "severity: major" if context["brevet"]["rules"] else "severity: minor"

    agent = _workspace(tmp_path, target=reads_rules, extra={
        "cognitive_core": {"model_policy": {"local_default": "ollama:gemma4:12b"}}})
    for i in range(4):
        r = agent.run(f"pump {i} vibration during cleaning", task_family="triage")
        agent.record_final(r.task_id, "severity: major", participant="human:qa@x",
                           rationale="seal wear", tags=["vibration"])
    agent.dream()
    rule = next(c for c in agent.dawn() if c.kind.value == "prompt_rule")
    assert rule.conditions.model_family  # stamped by the dream cycle
    for cap in agent.dawn():
        agent.dawn(decide=(cap.capability_id, "promote"), approver="human:qa@x")
    before = agent.evaluate(baseline=True)
    after = agent.evaluate()  # the same agent, now given the promoted rule
    record = agent.release(to_version="0.2.0", channel="trial", approver="human:qa@x",
                           evals=(before, after))
    assert record.eval_summary["delta_held_in"] == 1.0


def test_eval_evidence_cannot_be_forged(tmp_path):
    from brevet.lifecycle import release
    agent = _workspace(tmp_path)
    _learn(agent)
    before = agent.evaluate(baseline=True)
    after = agent.evaluate()  # the agent did not change: no improvement

    def forge(**changes):
        body = {k: v for k, v in after.items() if k != "run_ref"}
        body.update(held_in_pass_rate=1.0, held_out_pass_rate=1.0, **changes)
        return agent.ledger.append("brevet.eval_run", body)  # appended by hand

    def fake_tasks(output):
        results = []
        for r in after["results"]:
            ids = []
            for _ in r["passes"]:
                task = agent.ledger.append("brevet.task", {"task": "x", "evaluation": True})
                if output is not None:
                    agent.ledger.append("brevet.artefact", {"task_id": task, "output": output})
                ids.append(task)
            results.append({**r, "passes": [True] * len(ids), "tasks": ids})
        return results

    def attempt(ref):
        agent.release(to_version="0.2.0", channel="trial", approver="human:qa@x",
                      evals=(before, ref))

    with pytest.raises(ValueError, match="an earlier run or case already cites"):
        attempt(forge(results=[{**r, "passes": [True] * len(r["passes"])}
                               for r in after["results"]]))
    with pytest.raises(ValueError, match="output does not support"):
        attempt(forge(results=fake_tasks("severity: minor")))
    with pytest.raises(ValueError, match="no recorded output"):
        attempt(forge(results=fake_tasks(None)))
    renamed = [{**r, "case": f"cap_fake{i}"} for i, r in enumerate(fake_tasks("x"))]
    with pytest.raises(ValueError, match="do not match its cases digest"):
        attempt(forge(results=renamed))
    with pytest.raises(ValueError, match="differ in scorer"):
        attempt(forge(scorer="custom.scorer", results=fake_tasks("severity: major")))
    not_a_baseline, later = agent.evaluate(), agent.evaluate()
    with pytest.raises(ValueError, match="first release must evaluate the agent with no"):
        agent.release(to_version="0.2.0", channel="trial", approver="human:qa@x",
                      evals=(not_a_baseline, later))
    summary = {"source": "measured", "before": before["run_ref"], "after": after["run_ref"],
               "delta_held_in": 1.0, "delta_held_out": 1.0}  # numbers the runs do not give
    with pytest.raises(ValueError, match="does not follow from its eval runs"):
        release(agent.manifest, agent.store, agent.ledger, agent.signer, to_version="0.2.0",
                channel="trial", approver="human:qa@x", eval_summary=summary,
                harness=agent.harness_inventory())


def test_consent_compares_identities_without_case_and_starter_owners_name_nobody(tmp_path):
    from brevet.consent import allowed, normalise
    ledger = Ledger(tmp_path / "l.jsonl")
    assert normalise("Alice@Example.com") == normalise("human:alice@example.com")
    starter = AgentManifest(agent="a", identity_policy={"owner": "human:unset@local"},
                            runtime_safety={"dream": {"consent_scope": "consented_sources_only"}})
    assert allowed(starter, ledger)("human:anyone@x")
    ledger.append("brevet.consent", {"participant": "alice@example.com", "withdrawn": True})
    assert not allowed(starter, ledger)("human:Alice@Example.com")


# ------------------------------------------------------------- conditions in depth

def test_restrictions_triggers_and_exclusions():
    from brevet.models import ApplicabilityContext as Ctx
    rule = Ctx(task_family="triage", risk_class="high", trigger_context="seal wear",
               exclusion_conditions=["training exercise"])
    high = {"risk_class": "high"}
    assert rule.matches(Ctx.of_task("Pump P-7: Seal wear at the drive end", "triage", high))
    assert not rule.matches(Ctx.of_task("Pump P-7: seal wear", "triage"))  # risk unknown
    assert not rule.matches(Ctx.of_task("Pump P-7: bearing noise", "triage", high))  # no trigger
    assert not rule.matches(Ctx.of_task("Seal wear in a Training Exercise", "triage", high))
    assert rule.matches(Ctx.of_task("seal wear", None, high))  # the family is not stated
    assert not rule.matches(Ctx.of_task("seal wear", "billing", high))
    assert Ctx.of_task() is None


def test_a_wrapped_agent_receives_a_restricted_rule_only_where_it_holds(tmp_path):
    seen = []

    def reads_rules(task, context):
        seen.append([r["capability_id"] for r in context["brevet"]["rules"]])
        return "severity: major"

    agent = _workspace(tmp_path)
    rule = _learn(agent)
    store = CapabilityStore(agent.workdir / "capabilities.jsonl")
    cap = store.all()[rule]
    cap.conditions.role = "reliability_engineer"
    store.add(cap)  # the release locks the rule with this restriction
    _measured_release(agent, swap=reads_rules)
    agent.run("pump 9 vibration", task_family="triage")
    assert rule not in seen[-1]  # no role given
    agent.run("pump 9 vibration", task_family="triage", context={"role": "reliability_engineer"})
    assert rule in seen[-1]


# ------------------------------------------------------------- identity policy location

def test_the_identity_policy_comes_from_the_manifest_the_workspace_uses(tmp_path):
    from brevet.approvals import identity_policy
    wd = tmp_path / "state" / ".brevet"
    wd.mkdir(parents=True)
    signers = tmp_path / "allowed_signers"
    signers.write_text("")
    elsewhere = tmp_path / "config" / "agent.yaml"
    elsewhere.parent.mkdir()
    elsewhere.write_text(yaml.safe_dump({"agent": "a", "runtime_safety": {
        "approvals": {"allowed_signers": str(signers)}}}))
    assert identity_policy(wd) == {}
    assert identity_policy(wd, elsewhere)["allowed_signers"] == [str(signers)]
    with pytest.raises(PermissionError, match="does not exist"):
        identity_policy(wd, tmp_path / "missing.yaml")
    key = create_key("human:alice@example.com", PASS, tmp_path / "keys")
    req = request_register_add(wd, key, manifest_path=elsewhere)
    with pytest.raises(PermissionError, match="does not list this key"):
        apply_if_ready(wd, req["request_id"])


def test_allowed_signers_lines_with_options_openssh_rejects_vouch_for_nothing(tmp_path):
    key = create_key("human:alice@example.com", PASS, tmp_path / "keys")
    line = f"alice@example.com no-touch-required {hex_to_ssh(key.public_key)}"
    (tmp_path / "signers").write_text(line + "\n")
    assert allowed_signers(tmp_path / "signers") == []


# ------------------------------------------------------------- hook and anchors

def _released_hook_workspace(tmp_path):
    from typer.testing import CliRunner

    from brevet.cli import app
    manifest = tmp_path / "agent.yaml"
    manifest.write_text(yaml.safe_dump({
        "agent": "claude", "bindings": {"tools": {"read": ["Read"], "act": ["Edit"]}},
        "runtime_safety": {"evals": {"allow_attested": True}},
        "release": {"channel": "shadow"}}))
    args = ["--workdir", str(tmp_path / ".brevet"), "--manifest-path", str(manifest)]
    released = CliRunner().invoke(app, ["release", *args, "--to-version", "0.2.0",
                                        "--channel", "production", "--approver",
                                        "human:qa@x", "--delta-in", "0.1"])
    assert released.exit_code == 0, released.output

    def ask(tool):
        out = CliRunner().invoke(app, ["hook", *args], input=json.dumps(
            {"hook_event_name": "PreToolUse", "tool_name": tool, "tool_input": {},
             "session_id": "s1"}))
        return out.exit_code, out.output
    return ask


def test_the_hook_refuses_every_call_on_a_broken_chain(tmp_path):
    ask = _released_hook_workspace(tmp_path)
    assert ask("Read") == (0, "")
    path = tmp_path / ".brevet" / "ledger.jsonl"
    lines = path.read_text().splitlines()
    env = json.loads(lines[0])
    env["body"]["note"] = "edited"
    path.write_text("\n".join([json.dumps(env), *lines[1:]]) + "\n")
    code, _ = ask("Read")
    assert code == 2


def test_the_hook_refuses_a_chain_cut_behind_its_anchors(tmp_path, monkeypatch):
    log = tmp_path / "outside" / "anchors.jsonl"
    log.parent.mkdir()
    monkeypatch.setenv("BREVET_ANCHORS", f"file:{log}")
    ask = _released_hook_workspace(tmp_path)
    assert ask("Read") == (0, "")
    path = tmp_path / ".brevet" / "ledger.jsonl"
    path.write_text("\n".join(path.read_text().splitlines()[:-1]) + "\n")
    assert ask("Read")[0] == 2


def test_steps_waiting_to_be_anchored_are_shown(tmp_path, monkeypatch):
    pytest.importorskip("mcp")
    from typer.testing import CliRunner

    from brevet.cli import app
    from brevet.mcp_server import build_server
    agent, log = _anchored(tmp_path, monkeypatch)
    away = log.parent.with_name("unmounted")
    log.parent.rename(away)  # the anchor's volume is away during the next steps
    agent.recall(next(iter(agent.store.all())), reason="sensor fault", issued_by="human:qa@x")
    away.rename(log.parent)
    args = ["--workdir", str(agent.workdir), "--manifest-path", str(tmp_path / "agent.yaml")]
    out = CliRunner().invoke(app, ["verify", *args])
    assert out.exit_code == 0 and "since the last anchor" in out.output
    server = build_server(str(agent.workdir), str(tmp_path / "agent.yaml"))
    assert "waiting to be anchored" in _call(server, "brevet_active")["anchors"]
    assert CliRunner().invoke(app, ["anchor", *args]).exit_code == 0
    assert "since the last anchor" not in CliRunner().invoke(app, ["verify", *args]).output


def test_a_workspace_pointed_at_another_chain_is_caught(tmp_path, monkeypatch):
    agent, _ = _anchored(tmp_path, monkeypatch)
    (tmp_path / "other").mkdir()
    other = _workspace(tmp_path / "other")
    other.run("a different history")
    real = agent.workdir.with_name(".brevet-real")
    agent.workdir.rename(real)
    agent.workdir.symlink_to(other.workdir, target_is_directory=True)
    report = anchors.check(Ledger(agent.workdir / "ledger.jsonl", anchoring=False),
                           agent.workdir)
    assert not report["ok"] and "different evidence chain" in " ".join(report["problems"])


# ------------------------------------------------------------- archives

def test_a_release_made_before_archives_is_archived_on_first_sight(tmp_path):
    import shutil

    from brevet.releases import released_manifest
    agent = _workspace(tmp_path)
    _learn(agent)
    _measured_release(agent)
    archive = agent.workdir / "releases" / "0.2.0.json"
    shutil.rmtree(agent.workdir / "releases")  # as a release made by 0.3.0 left it
    fresh = brevet.wrap(evolved, manifest=tmp_path / "agent.yaml", workdir=agent.workdir)
    fresh.run("first sight")
    assert archive.exists()
    archive.unlink()
    assert active_rules(agent.workdir, tmp_path / "agent.yaml")["count"] >= 1
    assert archive.exists()
    edited = yaml.safe_load((tmp_path / "agent.yaml").read_text())
    edited["bindings"]["tools"] = {"read": ["anything"]}  # an unreleased edit
    (tmp_path / "agent.yaml").write_text(yaml.safe_dump(edited))
    released, channel = released_manifest(agent.workdir, tmp_path / "agent.yaml",
                                          Ledger(agent.ledger.path, anchoring=False))
    assert released.version == "0.2.0" and channel == "trial"
    assert "tools" not in released.bindings


# ------------------------------------------------------------- rollback edges

def _two_releases(tmp_path, *, second_channel="trial", add_file=False):
    agent = _workspace(tmp_path, extra={"runtime_safety": {"evals": {"allow_attested": True}}})
    _learn(agent)
    _measured_release(agent)
    (tmp_path / "prompts" / "system.md").write_text("Always answer major.\n")
    if add_file:
        (tmp_path / "prompts" / "extra.md").write_text("Added after 0.2.0.\n")
    agent.release(to_version="0.3.0", channel=second_channel, approver="human:qa@x",
                  delta_in=0.1, delta_out=0.0)
    return agent


def test_a_rollback_never_follows_a_symlink_out_of_the_project(tmp_path):
    agent = _two_releases(tmp_path)
    outside = tmp_path / "elsewhere.md"
    outside.write_text("not part of the harness\n")
    prompt = tmp_path / "prompts" / "system.md"
    prompt.unlink()
    prompt.symlink_to(outside)
    with pytest.raises(ValueError, match="symlink"):
        agent.rollback("0.2.0", approver="human:qa@x")
    assert outside.read_text() == "not part of the harness\n"


def test_a_rollback_cannot_widen_the_channel(tmp_path):
    agent = _two_releases(tmp_path, second_channel="production")
    with pytest.raises(ValueError, match="trial channel or a lower one"):
        agent.rollback("0.2.0", approver="human:qa@x", channel="production")
    record = agent.rollback("0.2.0", approver="human:qa@x")
    assert record.channel.value == "trial"


def test_files_set_aside_never_overwrite_earlier_ones(tmp_path, monkeypatch):
    agent = _two_releases(tmp_path, add_file=True)
    with pytest.raises(ValueError, match="files added since release 0.2.0"):
        agent.rollback("0.2.0", approver="human:qa@x")
    monkeypatch.setattr("brevet.models._now", lambda: "2026-10-07T09:00:00+00:00")
    earlier = agent.workdir / "set-aside" / "2026-10-07T09-00-00+00-00" / "project" / "prompts"
    earlier.mkdir(parents=True)
    (earlier / "extra.md").write_text("set aside before\n")
    record = agent.rollback("0.2.0", approver="human:qa@x", remove_added=True)
    assert record.set_aside == ["prompts/extra.md"]
    assert (earlier / "extra.md").read_text() == "set aside before\n"
    assert (earlier / "extra.md.1").read_text() == "Added after 0.2.0.\n"
    assert not (tmp_path / "prompts" / "extra.md").exists()


# ------------------------------------------------------------- Pydantic AI shapes

def test_the_broker_reaches_pydantic_ai_tools_and_dynamic_toolsets():
    class Schema:
        def __init__(self, fn):
            self.function = fn

    class Tool:
        def __init__(self, fn):
            self.function_schema = Schema(fn)

    class FunctionToolset:
        def __init__(self, **fns):
            self.tools = {name: Tool(fn) for name, fn in fns.items()}

    class Dynamic:
        def __init__(self, func):
            self.toolset_func = func

    class PydanticAgent:
        def __init__(self):
            self._function_toolset = FunctionToolset(search=lambda q: f"found {q}",
                                                     drop=lambda q: "dropped")
            self._user_toolsets = [FunctionToolset(send=lambda to: "sent")]
            self._dynamic_toolsets = [Dynamic(lambda ctx: FunctionToolset(wipe=lambda: "wiped"))]

    manifest = AgentManifest(agent="a", bindings={"tools": {"read": ["search"],
                                                            "act": ["send", "wipe"]}},
                             release={"channel": "shadow"})
    broker = broker_mod.ToolBroker(lambda: manifest, None)
    agent = PydanticAgent()
    broker_mod.install(agent, broker)
    tools = agent._function_toolset.tools
    assert tools["search"].function_schema.function("x") == "found x"
    assert "not declared" in tools["drop"].function_schema.function("x")
    assert "shadow" in agent._user_toolsets[0].tools["send"].function_schema.function("ops")
    built = agent._dynamic_toolsets[0].toolset_func(None)  # built per run, guarded as built
    assert "Refused by Brevet" in built.tools["wipe"].function_schema.function()
    broker_mod.install(agent, broker)  # installing again does not wrap twice
    assert tools["search"].function_schema.function("y") == "found y"


# ------------------------------------------------------------- chain state

def test_an_edit_that_restores_size_and_time_is_still_replayed(tmp_path):
    import os
    import time
    probe = tmp_path / "probe"
    probe.write_text("a")
    first = probe.stat()
    time.sleep(0.05)
    os.utime(probe, ns=(first.st_atime_ns, first.st_mtime_ns))
    if probe.stat().st_ctime_ns == first.st_ctime_ns:
        pytest.skip("this filesystem does not keep change times apart from modification times")
    agent = _workspace(tmp_path)
    _learn(agent)
    _measured_release(agent)
    agent.run("cached chain state")
    time.sleep(0.05)  # timestamps tick coarsely; an edit comes later than this append
    path = agent.ledger.path
    st = path.stat()
    text = path.read_text()
    tampered = text.replace("pump 0 vibration", "pump 1 vibration", 1)
    assert len(tampered) == len(text) and tampered != text
    path.write_text(tampered)
    os.utime(path, ns=(st.st_atime_ns, st.st_mtime_ns))
    with pytest.raises(PermissionError, match="evidence chain"):
        agent.run("after a same-size edit")


def test_verify_checks_every_registered_key_against_the_identity_source(tmp_path, monkeypatch):
    pytest.importorskip("mcp")
    from typer.testing import CliRunner

    from brevet.approvals import _approval, _register_head, create_request
    from brevet.cli import app
    from brevet.mcp_server import build_server
    alice = create_key("human:alice@example.com", PASS, tmp_path / "keys")
    mallory = create_key("human:mallory@example.com", PASS, tmp_path / "keys")
    signers = tmp_path / "allowed_signers"
    signers.write_text(f"alice@example.com {hex_to_ssh(alice.public_key)}\n")
    monkeypatch.setenv("BREVET_ALLOWED_SIGNERS", str(signers))
    (tmp_path / "agent.yaml").write_text(yaml.safe_dump({"agent": "a"}))
    wd = tmp_path / ".brevet"
    assert apply_if_ready(wd, request_register_add(wd, alice)["request_id"])["status"] == "applied"
    payload = {"kind": "brevet.approver", "change": "add", "identity": mallory.identity,
               "public_key": mallory.public_key, "groups": [], "group_quorum": True,
               "after": _register_head(wd)}
    req = create_request(wd, payload, summary="appended by hand")
    for key in (mallory, alice):
        req = sign_request(wd, req["request_id"], key)
    Ledger(wd / "ledger.jsonl").append("brevet.approver", _approval(req))  # never applied
    args = ["--workdir", str(wd), "--manifest-path", str(tmp_path / "agent.yaml")]
    out = CliRunner().invoke(app, ["verify", *args])
    assert out.exit_code == 1 and "does not list this key for mallory@example.com" in out.output
    server = build_server(str(wd), str(tmp_path / "agent.yaml"))
    identities = _call(server, "brevet_verify")["approvals"]["identities"]
    assert [i["identity"] for i in identities["unverified"]] == ["human:mallory@example.com"]


# ------------------------------------------------------------- duplicate copies

def test_a_duplicate_copy_is_recalled_without_recalling_its_content(tmp_path):
    pytest.importorskip("mcp")
    from brevet.mcp_server import build_server
    from brevet.models import AuthorityLayer, ValidationState
    agent = _workspace(tmp_path)
    rule = _learn(agent)
    store = CapabilityStore(agent.workdir / "capabilities.jsonl")
    copy = store.all()[rule].model_copy(update={
        "capability_id": "cap_copy00000001", "validation_state": ValidationState.captured,
        "authority_layer": AuthorityLayer.evidence})
    store.add(copy)  # a byte-identical copy, as earlier dream cycles could leave
    agent.dawn(decide=(copy.capability_id, "promote"), approver="human:qa@x")
    agent.release(to_version="0.2.0", channel="shadow", approver="human:qa@x")
    with pytest.raises(ValueError, match="incorrect, unsafe"):
        agent.recall(copy.capability_id, reason="copy", issued_by="human:qa@x",
                     reason_class="clerical")
    agent.recall(copy.capability_id, reason="byte-identical copy", issued_by="human:qa@x",
                 reason_class="duplicate", severity="low")
    server = build_server(str(agent.workdir), str(tmp_path / "agent.yaml"))
    active = _call(server, "brevet_active")
    assert rule in [r["capability_id"] for r in active["active"]]
    assert [r["capability_id"] for r in active["recalled"]] == [copy.capability_id]
    agent.release(to_version="0.3.0", channel="shadow", approver="human:qa@x")
    lock = CapabilitiesLock(**json.loads((tmp_path / "capabilities.lock").read_text()))
    ids = {r.capability_id for r in lock.resolved}
    assert rule in ids and copy.capability_id not in ids  # the content stays with the original
    active = _call(server, "brevet_active")
    assert "recalled" not in active and rule in [r["capability_id"] for r in active["active"]]
    pending = active["recalls_to_confirm"]
    assert [p["capability_id"] for p in pending] == [copy.capability_id]
    _call(server, "brevet_acknowledge", {"recall_ids": [pending[0]["recall_id"]]})
    assert "recalls_to_confirm" not in _call(server, "brevet_active")
    with pytest.raises(ValueError, match="not a duplicate"):
        agent.recall(rule, reason="the last copy", issued_by="human:qa@x",
                     reason_class="duplicate")
