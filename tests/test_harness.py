"""The harness bill of materials: releases lock the harness the agent runs
with, and an unreleased change to it is caught before the next run."""

import json

import pytest
import yaml

import brevet
from brevet import harness as harness_mod
from brevet.approvals import (
    apply_if_ready,
    create_key,
    request_register_add,
    request_release,
    sign_request,
)
from brevet.canonical import object_sha256
from brevet.harness import compare, describe_agent, lock_digest
from brevet.lifecycle import CapabilityStore
from brevet.models import AgentManifest, CapabilitiesLock, LockedCapability

PASS = "correct horse battery"


def core(changes):
    """A drift record without its digests."""
    return [{k: c[k] for k in ("component_id", "kind", "change")} for c in changes]


def stub(task, context):
    return "severity: minor"


def other_stub(task, context):
    return "severity: major"


def _workspace(tmp_path, *, channel="trial", files=("prompts/*.md",), policy=None):
    (tmp_path / "prompts").mkdir(parents=True)
    (tmp_path / "prompts" / "system.md").write_text("Classify severity carefully.\n")
    manifest = {"agent": "triage", "version": "0.1.0",
                "bindings": {"harness_files": list(files)}}
    if policy:
        manifest["runtime_safety"] = {"harness": policy}
    (tmp_path / "agent.yaml").write_text(yaml.safe_dump(manifest))
    agent = brevet.wrap(stub, manifest=tmp_path / "agent.yaml", workdir=tmp_path / ".brevet")
    agent.release(to_version="0.2.0", channel=channel, approver="human:qa@x",
                  delta_in=0.1, delta_out=0.0)
    return agent


def test_release_locks_files_agent_code_and_library_versions(tmp_path):
    agent = _workspace(tmp_path)
    lock = CapabilitiesLock(**json.loads((tmp_path / "capabilities.lock").read_text()))
    ids = {c.component_id for c in lock.harness}
    assert {"file:prompts/system.md", "agent:code", "env:brevet", "env:python"} <= ids
    assert lock.harness_sources == ["agent", "env", "files"]
    release = list(agent.ledger.read("brevet.release"))[-1]["body"]
    assert release["lockfile_hash"] == lock.lockfile_hash == lock_digest(lock)


def test_a_lock_without_a_harness_keeps_its_old_digest():
    lock = CapabilitiesLock(agent="a", agent_version="0.1.0", resolved=[LockedCapability(
        capability_id="cap_1", kind="prompt_rule", content_hash="sha256:" + "0" * 64,
        authority_layer="advisory", approved_by="human:qa@x", approved_at="now")])
    assert lock_digest(lock) == object_sha256([r.model_dump() for r in lock.resolved])


def test_an_unreleased_file_change_stops_the_run_outside_shadow(tmp_path):
    agent = _workspace(tmp_path)
    agent.run("before any change")
    (tmp_path / "prompts" / "system.md").write_text("Always answer minor.\n")
    for _ in range(2):
        with pytest.raises(PermissionError, match="file:prompts/system.md"):
            agent.run("after the change")
    drifts = list(agent.ledger.read("brevet.drift"))
    assert len(drifts) == 1  # recorded once, not on every attempt
    assert core(drifts[0]["body"]["changes"]) == [
        {"component_id": "file:prompts/system.md", "kind": "file", "change": "changed"}]
    (tmp_path / "prompts" / "system.md").write_text("Always answer major.\n")
    with pytest.raises(PermissionError):
        agent.run("after a second, different change")
    assert len(list(agent.ledger.read("brevet.drift"))) == 2  # a new state is a new record


def test_a_new_file_and_new_code_count_as_drift(tmp_path):
    agent = _workspace(tmp_path)
    (tmp_path / "prompts" / "extra.md").write_text("a new instruction\n")
    agent.adapter.target = other_stub
    changes = {c["component_id"]: c["change"] for c in agent.harness_drift()}
    assert changes == {"file:prompts/extra.md": "added", "agent:code": "changed"}


def test_shadow_records_drift_without_stopping(tmp_path):
    agent = _workspace(tmp_path, channel="shadow")
    (tmp_path / "prompts" / "system.md").write_text("changed\n")
    agent.run("still runs in shadow")
    assert len(list(agent.ledger.read("brevet.drift"))) == 1


def test_library_drift_is_recorded_unless_the_policy_blocks_it(tmp_path, monkeypatch):
    lenient = _workspace(tmp_path / "lenient")
    strict = _workspace(tmp_path / "strict", policy={"env": "block"})
    upgraded = [c.model_copy(update={"digest": object_sha256("9.9.9"), "detail": "9.9.9"})
                if c.component_id == "env:brevet" else c
                for c in harness_mod.env_components("callable")]
    monkeypatch.setattr(harness_mod, "env_components", lambda framework=None: upgraded)
    lenient.run("library upgrades are recorded, not blocked")
    assert list(lenient.ledger.read("brevet.drift"))
    with pytest.raises(PermissionError, match="env:brevet"):
        strict.run("blocked by policy")


def test_agent_inventory_covers_instructions_model_tools_and_mcp():
    class Tool:
        def __init__(self, name, description):
            self.name, self.description = name, description
            self.params_json_schema = {"type": "object", "properties": {"q": {"type": "string"}}}

    class Agent:
        def __init__(self, tool_description):
            self.instructions = "You triage deviations."
            self.model = "gpt-5"
            self.tools = [Tool("search", tool_description)]
            self.mcp_servers = {"files": {"command": "mcp-files", "env": {"TOKEN": "secret"}}}
            self.handoffs = []

    before = {c.component_id: c for c in describe_agent(Agent("Search the deviation log"))}
    assert {"agent:instructions", "agent:model", "agent:tool:search", "agent:mcp:files"} <= set(before)
    assert all("secret" not in c.detail for c in before.values())
    after = describe_agent(Agent("Search the deviation log and close items"))
    assert core(compare(list(before.values()), after)) == [
        {"component_id": "agent:tool:search", "kind": "agent", "change": "changed"}]


# ------------------------------------------------------------- with signed approvals

def _signing_workspace(tmp_path):
    (tmp_path / "prompts").mkdir()
    (tmp_path / "prompts" / "system.md").write_text("v1\n")
    mpath = tmp_path / "agent.yaml"
    mpath.write_text(yaml.safe_dump({"agent": "triage", "version": "0.1.0",
                                     "bindings": {"harness_files": ["prompts/*.md"]}}))
    wd = tmp_path / ".brevet"
    key = create_key("human:alice@example.com", PASS, tmp_path / "keys")
    apply_if_ready(wd, request_register_add(wd, key)["request_id"])
    return mpath, wd, key


def _request(mpath, wd):
    manifest = AgentManifest(**yaml.safe_load(mpath.read_text()))
    return request_release(wd, manifest, CapabilityStore(wd / "capabilities.jsonl"),
                           to_version="0.2.0", channel="shadow",
                           approver="human:alice@example.com", manifest_path=mpath,
                           harness=harness_mod.inventory(manifest, mpath))


def test_a_signed_release_covers_the_manifest(tmp_path):
    mpath, wd, key = _signing_workspace(tmp_path)
    req = _request(mpath, wd)
    assert "manifest_hash" in req["payload"]
    sign_request(wd, req["request_id"], key)
    edited = yaml.safe_load(mpath.read_text())
    edited["bindings"]["tools"] = {"act": ["close_deviation"]}  # granted after signing
    mpath.write_text(yaml.safe_dump(edited))
    with pytest.raises(PermissionError, match="manifest_hash"):
        apply_if_ready(wd, req["request_id"])
    fresh = _request(mpath, wd)
    sign_request(wd, fresh["request_id"], key)
    assert apply_if_ready(wd, fresh["request_id"])["status"] == "applied"


def test_a_signed_release_is_refused_if_a_harness_file_changes_first(tmp_path):
    mpath, wd, key = _signing_workspace(tmp_path)
    req = _request(mpath, wd)
    (tmp_path / "prompts" / "system.md").write_text("v2, unsigned\n")
    sign_request(wd, req["request_id"], key)
    with pytest.raises(PermissionError, match="harness files changed"):
        apply_if_ready(wd, req["request_id"])


# ------------------------------------------------------------- CLI and MCP

def test_cli_harness_reports_file_drift(tmp_path, monkeypatch, capsys):
    from brevet import cli
    (tmp_path / "prompts").mkdir()
    (tmp_path / "prompts" / "system.md").write_text("v1\n")
    (tmp_path / "agent.yaml").write_text(yaml.safe_dump(
        {"agent": "a", "version": "0.1.0", "bindings": {"harness_files": ["prompts/*.md"]}}))
    monkeypatch.chdir(tmp_path)

    def run(*argv):
        monkeypatch.setattr("sys.argv", ["brevet", *argv])
        try:
            cli.main()
        except SystemExit as e:
            return e.code
        return 0

    assert run("release", "--to-version", "0.2.0", "--approver", "human:qa@x") in (0, None)
    assert "1 harness component" in capsys.readouterr().out
    assert run("harness") in (0, None)
    (tmp_path / "prompts" / "new.md").write_text("added later\n")
    assert run("harness") == 1
    assert "added" in capsys.readouterr().out


def test_mcp_reports_harness_drift(tmp_path):
    pytest.importorskip("mcp")
    import asyncio

    from brevet.mcp_server import build_server

    def call(server, name, args=None):
        result = asyncio.run(server.call_tool(name, args or {}))
        content = getattr(result, "content", result)
        if isinstance(content, tuple):
            content = content[0]
        return json.loads(content[0].text)

    (tmp_path / "skills").mkdir()
    (tmp_path / "skills" / "triage.md").write_text("skill v1\n")
    (tmp_path / "agent.yaml").write_text(yaml.safe_dump(
        {"agent": "a", "version": "0.1.0", "bindings": {"harness_files": ["skills"]}}))
    server = build_server(str(tmp_path / ".brevet"), str(tmp_path / "agent.yaml"))
    out = call(server, "brevet_release", {"to_version": "0.2.0", "approver": "human:qa@x"})
    assert out["harness_components"] == 1
    assert call(server, "brevet_harness")["file_drift"] == []
    (tmp_path / "skills" / "new_skill.md").write_text("written by the agent\n")
    report = call(server, "brevet_harness")
    assert core(report["file_drift"]) == [{"component_id": "file:skills/new_skill.md",
                                           "kind": "file", "change": "added"}]
    assert call(server, "brevet_active")["harness_drift"]
    assert call(server, "brevet_status")["harness"]["file_drift"] == 1


# ------------------------------------------------------------- review fixes

def test_an_edited_or_missing_lock_stops_the_run(tmp_path):
    agent = _workspace(tmp_path)
    lock_path = tmp_path / "capabilities.lock"
    lock = json.loads(lock_path.read_text())
    lock["harness"] = [c for c in lock["harness"] if c["kind"] != "file"]  # hide the prompt
    lock_path.write_text(json.dumps(lock))
    with pytest.raises(PermissionError, match="capabilities.lock"):
        agent.run("edited lock")
    lock_path.unlink()
    with pytest.raises(PermissionError, match="capabilities.lock"):
        agent.run("missing lock")


def test_a_re_signed_or_restored_manifest_is_not_the_latest_release(tmp_path):
    import shutil

    agent = _workspace(tmp_path)
    shutil.copy(tmp_path / "agent.yaml", tmp_path / "v020.yaml")
    agent.release(to_version="0.3.0", channel="trial", approver="human:qa@x",
                  delta_in=0.1, delta_out=0.0)
    restored = brevet.wrap(stub, manifest=tmp_path / "v020.yaml", workdir=tmp_path / ".brevet")
    with pytest.raises(PermissionError, match="latest release is 0.3.0"):
        restored.run("an older release, restored by hand")

    relaxed = agent.manifest.model_copy(deep=True)
    relaxed.runtime_safety = {"harness": {"on_drift": "record"}}
    payload = relaxed.unsigned_payload()
    relaxed.signature = {**agent.manifest.signature, "signature": agent.signer.sign(payload),
                         "content_hash": object_sha256(payload)}
    (tmp_path / "agent.yaml").write_text(yaml.safe_dump(relaxed.model_dump()))
    agent.manifest = relaxed  # re-signed with the workspace key, outside any release
    with pytest.raises(PermissionError, match="this manifest is not it"):
        agent.run("re-signed policy")


def test_patterns_handle_odd_paths_hidden_files_and_the_workspace(tmp_path):
    base = tmp_path / "Agent [v2]"
    (base / "skills").mkdir(parents=True)
    (base / "skills" / "triage.md").write_text("skill\n")
    (base / "skills" / ".override.md").write_text("hidden\n")
    (base / "skills" / ".DS_Store").write_text("finder\n")
    (base / "state").mkdir()
    (base / "state" / "ledger.jsonl").write_text("{}\n")
    manifest = AgentManifest(agent="a", bindings={"harness_files": ["."]})
    comps, _ = harness_mod.inventory(manifest, base / "agent.yaml", workdir=base / "state")
    ids = {c.component_id for c in comps}
    assert ids == {"file:skills/triage.md", "file:skills/.override.md"}


def test_partial_agents_hash_the_same_in_every_process():
    import functools

    class Client:
        pass

    def answer(client, task, context):
        return "x"

    first = describe_agent(functools.partial(answer, Client()))
    second = describe_agent(functools.partial(answer, Client()))
    assert first[0].digest == second[0].digest


# ------------------------------------------------------------- second review

def test_editing_the_locks_version_does_not_switch_off_the_drift_check(tmp_path):
    agent = _workspace(tmp_path)
    (tmp_path / "prompts" / "system.md").write_text("Always answer minor.\n")
    with pytest.raises(PermissionError, match="harness"):
        agent.run("unreleased prompt")
    lock_path = tmp_path / "capabilities.lock"
    lock = json.loads(lock_path.read_text())
    lock["agent_version"] = "9.9.9"  # an attempt to make the lock look like another release
    lock_path.write_text(json.dumps(lock))
    with pytest.raises(PermissionError, match="capabilities.lock"):
        agent.run("unreleased prompt, lock edited")


def test_a_long_running_agent_follows_a_release_made_elsewhere(tmp_path):
    agent = _workspace(tmp_path)
    agent.run("before")
    elsewhere = brevet.wrap(stub, manifest=tmp_path / "agent.yaml", workdir=tmp_path / ".brevet")
    elsewhere.release(to_version="0.3.0", channel="trial", approver="human:qa@x",
                      delta_in=0.1, delta_out=0.0)
    agent.run("after a release made in another process")
    assert agent.manifest.version == "0.3.0"


def test_an_agent_with_a_manifest_object_runs_after_its_release(tmp_path):
    agent = brevet.wrap(stub, manifest=AgentManifest(agent="inline"), workdir=tmp_path / ".brevet")
    agent.release(to_version="0.2.0", channel="trial", approver="human:qa@x",
                  delta_in=0.1, delta_out=0.0)
    assert (tmp_path / ".brevet" / "capabilities.lock").exists()
    agent.run("runs from the lock kept in the working directory")


def test_evaluation_runs_record_drift_without_stopping(tmp_path):
    agent = _workspace(tmp_path)
    (tmp_path / "prompts" / "system.md").write_text("A change being evaluated.\n")
    with pytest.raises(PermissionError):
        agent.run("production work is refused")
    result = agent._evaluation_run("an eval case")
    task = next(e for e in agent.ledger.read("brevet.task") if e["envelope_id"] == result.task_id)
    assert task["body"]["evaluation"] is True
    assert len(list(agent.ledger.read("brevet.drift"))) == 1


def test_a_drift_policy_it_does_not_recognise_blocks():
    def policy(value):
        return harness_mod.policy(AgentManifest(agent="a", runtime_safety={"harness": value}))
    assert policy({"on_drift": "record"})["on_drift"] == "record"
    assert policy({"on_drift": "off"})["on_drift"] == "block"
    assert policy({"on_drift": False, "env": "ignore"}) == {"on_drift": "block", "env": "block"}
    assert policy("off") == harness_mod.DEFAULT_POLICY


def test_library_code_and_rotated_secrets_are_not_agent_changes():
    import collections
    import dataclasses
    import threading

    assert harness_mod._is_library(json.dumps) and not harness_mod._is_library(stub)
    assert harness_mod._source(collections.OrderedDict) == "collections.OrderedDict"

    class Agent:
        def __init__(self, token, tool_fn):
            self.instructions = json.dumps  # a framework method, not the user's text
            self.mcp_servers = {"files": {"command": "mcp-files", "env": {"TOKEN": token}}}
            self.tools = [type("Tool", (), {"name": "lookup", "description": "Look up",
                                            "func": staticmethod(tool_fn)})()]

    def lookup_v1(q):
        return q

    def lookup_v2(q):
        return q.upper()

    a = {c.component_id: c.digest for c in describe_agent(Agent("old-token", lookup_v1))}
    b = {c.component_id: c.digest for c in describe_agent(Agent("new-token", lookup_v1))}
    c = {c.component_id: c.digest for c in describe_agent(Agent("old-token", lookup_v2))}
    assert "agent:instructions" not in a
    assert a == b  # a rotated token is not a change
    assert a["agent:tool:lookup"] != c["agent:tool:lookup"]  # the tool's own code is

    @dataclasses.dataclass
    class Settings:
        temperature: float
        extra: dict

    class WithSettings:
        model_settings = Settings(0.2, {"lock": threading.Lock()})
    assert describe_agent(WithSettings())  # no deep copy of objects that cannot be copied


def test_patterns_that_match_nothing_are_reported(tmp_path):
    (tmp_path / "prompts").mkdir()
    (tmp_path / "prompts" / "a.md").write_text("x\n")
    (tmp_path / ".brevet").mkdir()
    (tmp_path / ".brevet" / "notes.md").write_text("inside the workspace\n")
    manifest = AgentManifest(agent="a", bindings={
        "harness_files": ["prompts/", "skils/", ".brevet/notes.md"]})
    assert harness_mod.unmatched_patterns(manifest, tmp_path / "agent.yaml",
                                          workdir=tmp_path / ".brevet") == [
        "skils/", ".brevet/notes.md"]


# ------------------------------------------------------------- third review

def test_the_users_own_installed_code_is_hashed_not_named(tmp_path):
    import importlib.util
    pkg = tmp_path / "site-packages"
    pkg.mkdir()
    (pkg / "my_tools.py").write_text("def lookup(q):\n    return q\n")
    spec = importlib.util.spec_from_file_location("my_tools", pkg / "my_tools.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    assert not harness_mod._is_library(module.lookup)  # not a framework package env records
    assert "return q" in harness_mod._source(module.lookup)


def test_only_secret_values_are_reduced_to_names():
    def digest(cfg):
        class Agent:
            def __init__(self):
                self.mcp_servers = {"github": cfg}
        return describe_agent(Agent())[0].digest
    base = {"command": "gh-mcp", "env": {"GITHUB_TOKEN": "a", "GITHUB_READ_ONLY": "1"},
            "headers": {"Authorization": "Bearer a", "X-MCP-Toolsets": "repos"}}
    rotated = {**base, "env": {**base["env"], "GITHUB_TOKEN": "b"},
               "headers": {**base["headers"], "Authorization": "Bearer b"}}
    widened = {**base, "env": {**base["env"], "GITHUB_READ_ONLY": "0"}}
    toolsets = {**base, "headers": {**base["headers"], "X-MCP-Toolsets": "all"}}
    assert digest(base) == digest(rotated)
    assert digest(base) != digest(widened) and digest(base) != digest(toolsets)

    class Settings:
        def __init__(self, stop):
            self.model_settings = {"stop_token": stop, "max_tokens": 100}
    assert describe_agent(Settings("</s>"))[0].digest != describe_agent(Settings("<eos>"))[0].digest


def test_a_release_without_the_running_agent_keeps_its_components(tmp_path):
    from typer.testing import CliRunner

    from brevet.cli import app
    _workspace(tmp_path)  # released from Python: files, agent code and libraries
    result = CliRunner().invoke(app, [
        "release", "--manifest-path", str(tmp_path / "agent.yaml"),
        "--workdir", str(tmp_path / ".brevet"), "--to-version", "0.3.0", "--channel", "trial",
        "--approver", "human:qa@x", "--delta-in", "0.1"])
    assert result.exit_code == 0, result.output
    lock = CapabilitiesLock(**json.loads((tmp_path / "capabilities.lock").read_text()))
    assert {"agent", "env", "files"} == set(lock.harness_sources)
    changed = brevet.wrap(other_stub, manifest=tmp_path / "agent.yaml",
                          workdir=tmp_path / ".brevet")
    with pytest.raises(PermissionError, match="agent:code"):
        changed.run("new code, released only from the CLI")


def test_release_ships_the_manifest_on_disk(tmp_path):
    agent = _workspace(tmp_path)
    manifest = yaml.safe_load((tmp_path / "agent.yaml").read_text())
    manifest["bindings"]["harness_files"] = ["prompts/*.md", "skills/"]
    (tmp_path / "agent.yaml").write_text(yaml.safe_dump(manifest))
    agent.release(to_version="0.3.0", channel="trial", approver="human:qa@x",
                  delta_in=0.1, delta_out=0.0)
    released = yaml.safe_load((tmp_path / "agent.yaml").read_text())
    assert released["bindings"]["harness_files"] == ["prompts/*.md", "skills/"]


def test_a_half_written_manifest_does_not_crash_a_running_agent(tmp_path):
    agent = _workspace(tmp_path, channel="shadow")
    elsewhere = brevet.wrap(stub, manifest=tmp_path / "agent.yaml", workdir=tmp_path / ".brevet")
    elsewhere.release(to_version="0.3.0", channel="shadow", approver="human:qa@x")
    (tmp_path / "agent.yaml").write_text("agent: [unfinished")
    agent.run("the file is being rewritten")


def test_a_renamed_agent_has_no_release_to_run(tmp_path):
    agent = _workspace(tmp_path)
    renamed = agent.manifest.model_copy(deep=True)
    renamed.agent = "triage_v2"
    payload = renamed.unsigned_payload()
    renamed.signature = {**agent.manifest.signature, "signature": agent.signer.sign(payload),
                         "content_hash": object_sha256(payload)}
    (tmp_path / "agent.yaml").write_text(yaml.safe_dump(renamed.model_dump()))
    with pytest.raises(PermissionError, match="no release of triage_v2"):
        brevet.wrap(stub, manifest=tmp_path / "agent.yaml", workdir=tmp_path / ".brevet").run("x")


def test_an_unrelated_invalid_decision_does_not_stop_the_agent(tmp_path):
    agent = _workspace(tmp_path)
    agent.ledger.append("brevet.promotion", {
        "capability_id": "cap_other", "outcome": "promote", "to_layer": "advisory",
        "approver": "human:qa@x", "approval": {"payload": {}, "signatures": []}})
    agent.run("still the signed release")
