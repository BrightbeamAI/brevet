"""Every model round-trips through its published JSON Schema."""

import json
from pathlib import Path

import pytest

jsonschema = pytest.importorskip("jsonschema")

SCHEMAS = Path(__file__).resolve().parents[1] / "schemas"


def _validate(schema_file: str, instance: dict) -> None:
    schema = json.loads((SCHEMAS / schema_file).read_text())
    jsonschema.Draft202012Validator(schema).validate(instance)


def test_capability_object_schema():
    from brevet.delta import mine
    from brevet.models import OverrideRecord
    ovr = [OverrideRecord(task_id=f"t{i}", intent_preserved=False, draft="a", final="b",
                          tags=["x"], task_family="f") for i in range(3)]
    cap = mine(ovr)[0]
    _validate("capability_object.schema.json", json.loads(cap.model_dump_json()))


def test_agent_manifest_schema():
    from brevet.models import AgentManifest
    m = AgentManifest(
        agent="demo_agent", version="0.1.0",
        identity_policy={"agent_id": "demo_agent", "owner": "human:o@x.com"},
        prompt_architecture={}, bindings={},
        cognitive_core={"model_policy": {"local_default": "ollama:gemma4:12b"}},
        runtime_safety={"loop": {"policy": "react"},
                        "evidence": {"ledger": "file:./ledger.jsonl"}},
    )
    _validate("agent_manifest.schema.json", json.loads(m.model_dump_json()))


def test_lock_release_recall_schemas(tmp_path):
    from brevet.canonical import Signer
    from brevet.delta import mine
    from brevet.ledger import Ledger
    from brevet.lifecycle import CapabilityStore, dawn_decide, recall, release
    from brevet.models import (
        AgentManifest,
        AuthorityLayer,
        OverrideRecord,
        ReleaseChannel,
        ReleaseRecord,
    )

    store, led = CapabilityStore(tmp_path / "c.jsonl"), Ledger(tmp_path / "l.jsonl")
    cand = mine([OverrideRecord(task_id=f"t{i}", intent_preserved=False, draft="a",
                                final="b", tags=["x"], task_family="f") for i in range(3)])[0]
    store.add(cand)
    dawn_decide(store, led, cand.capability_id, "promote", approver="mission_group:rft",
                to_layer=AuthorityLayer.advisory)
    _manifest, lock, record = release(
        AgentManifest(agent="demo_agent",
                      identity_policy={"agent_id": "demo_agent", "owner": "h"},
                      cognitive_core={"model_policy": {"local_default": "ollama:gemma4:12b"}},
                      prompt_architecture={}, bindings={},
                      runtime_safety={"loop": {"policy": "react"},
                                      "evidence": {"ledger": "file:./l.jsonl"}}),
        store, led, Signer(tmp_path / "k.pem"),
        to_version="0.2.0", channel=ReleaseChannel.shadow, approver="mission_group:rft")
    _validate("capabilities_lock.schema.json", json.loads(lock.model_dump_json()))
    _validate("release_record.schema.json", json.loads(record.model_dump_json()))
    notice = recall(store, led, cand.capability_id, reason="r", reason_class="stale",
                    severity="low", issued_by="mission_group:rft",
                    releases=[ReleaseRecord(**e["body"]) for e in led.read("brevet.release")])
    _validate("recall_notice.schema.json", json.loads(notice.model_dump_json()))
