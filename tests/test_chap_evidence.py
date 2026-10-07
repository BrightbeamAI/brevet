"""Inbound CHAP evidence source: verdict mapping, idempotency, chain checks,
and that ingested overrides feed the same mining path as brevet_record."""

import json

import pytest

from brevet.chap_evidence import INGEST_TAG, _apply_patch, _structural_ok, ingest
from brevet.delta import load_overrides
from brevet.ledger import Ledger

GENESIS = "sha256:" + "0" * 64
WS = "wsp_test"


def _entry(seq: int, method: str, params: dict, prev: str = GENESIS) -> dict:
    return {"seq": seq, "arrived": f"2026-08-13T16:00:{seq:02d}.000Z",
            "envelope": {"jsonrpc": "2.0", "id": f"mcp-{seq}",
                         "method": method, "params": {"workspace": WS, **params}},
            "prev_hash": prev if seq == 0 else f"sha256:{seq:064x}"}


ARTEFACT = {"title": "Client email", "kind": "write_email",
            "summary": "Casual tone", "content_sha256": "aa" * 32}


def _chain(decide: dict) -> list[dict]:
    return [
        _entry(0, "workspace.create", {"profiles": ["core/1.0"]}),
        _entry(1, "participant.join", {"from": "human:a@b.com", "type": "human"}),
        _entry(2, "task.create", {"from": "human:a@b.com", "kind": "write_email",
                                  "input": {"request": "Draft email to Sam"}}),
        _entry(3, "task.complete", {"from": "agent:claude-cowork",
                                    "task_id": "tsk_1", "output": ARTEFACT}),
        _entry(4, "review.request", {"from": "agent:claude-cowork",
                                     "task_id": "tsk_1", "to": "human:a@b.com",
                                     "artefact": ARTEFACT}),
        _entry(5, decide["method"], {"from": "human:a@b.com",
                                     "task_id": "tsk_1", **decide["params"]}),
    ]


def _write_sink(tmp_path, entries):
    sink = tmp_path / f"audit-{WS}.jsonl"
    sink.write_text("".join(json.dumps(e) + "\n" for e in entries))
    return sink


def test_override_carried_verbatim_and_patched(tmp_path):
    diff = [{"op": "replace", "path": "/summary", "value": "Formal tone"}]
    sink = _write_sink(tmp_path, _chain({
        "method": "decide.override",
        "params": {"diff": diff, "rationale": "too casual",
                   "tags": ["tone-formal"], "intent_preserved": True}}))
    summary = ingest(str(sink), workdir=tmp_path / ".brevet")
    assert summary["overrides"] == 1 and summary["chain"] == "structural"

    ovs = load_overrides(Ledger(tmp_path / ".brevet" / "ledger.jsonl"))
    assert len(ovs) == 1
    ov = ovs[0]
    assert ov.intent_preserved is True
    assert ov.diff == diff
    assert ov.rationale == "too casual"
    assert {"tone-formal", INGEST_TAG} <= set(ov.tags)
    assert ov.task_family == "write_email"
    assert '"Casual tone"' in ov.draft and '"Formal tone"' in ov.final
    assert ov.trace_ref == f"chap:{WS}#5"


def test_reject_is_substituting_judgment(tmp_path):
    sink = _write_sink(tmp_path, _chain({
        "method": "decide.reject",
        "params": {"comment": "stale source", "request_revision": True}}))
    summary = ingest(str(sink), workdir=tmp_path / ".brevet")
    assert summary["rejections"] == 1

    ov = load_overrides(Ledger(tmp_path / ".brevet" / "ledger.jsonl"))[0]
    assert ov.intent_preserved is False and ov.final == ""
    assert ov.rationale == "stale source" and "rejected" in ov.tags


def test_approve_marks_accepted_verbatim(tmp_path):
    sink = _write_sink(tmp_path, _chain({
        "method": "decide.approve", "params": {"comment": "ship it"}}))
    summary = ingest(str(sink), workdir=tmp_path / ".brevet")
    assert summary["approvals"] == 1 and summary["overrides"] == 0

    led = Ledger(tmp_path / ".brevet" / "ledger.jsonl")
    marks = [e for e in led.read("brevet.artefact")
             if e["body"].get("accepted_verbatim")]
    assert len(marks) == 1 and marks[0]["body"]["comment"] == "ship it"
    assert load_overrides(led) == []


def test_idempotent_via_cursor(tmp_path):
    sink = _write_sink(tmp_path, _chain({
        "method": "decide.override",
        "params": {"diff": [], "rationale": "r"}}))
    wd = tmp_path / ".brevet"
    first = ingest(str(sink), workdir=wd)
    second = ingest(str(sink), workdir=wd)
    assert first["overrides"] == 1 and second["overrides"] == 0
    assert len(load_overrides(Ledger(wd / "ledger.jsonl"))) == 1


def test_ledger_chain_stays_valid_after_ingest(tmp_path):
    sink = _write_sink(tmp_path, _chain({
        "method": "decide.override", "params": {"diff": [], "rationale": "r"}}))
    wd = tmp_path / ".brevet"
    ingest(str(sink), workdir=wd)
    ok, n = Ledger(wd / "ledger.jsonl").verify()
    assert ok and n == 3  # brevet.task + brevet.artefact + brevet.override


def test_strict_rejects_broken_genesis(tmp_path):
    entries = _chain({"method": "decide.approve", "params": {}})
    entries[0]["prev_hash"] = "sha256:" + "f" * 64  # not genesis
    sink = _write_sink(tmp_path, entries)
    assert not _structural_ok(entries)
    with pytest.raises(RuntimeError):
        ingest(str(sink), workdir=tmp_path / ".brevet", strict=True)


def test_directory_of_sinks_and_workspace_filter(tmp_path):
    _write_sink(tmp_path, _chain({
        "method": "decide.approve", "params": {}}))
    other = _chain({"method": "decide.approve", "params": {}})
    for e in other:
        e["envelope"]["params"]["workspace"] = "wsp_other"
    (tmp_path / "audit-wsp_other.jsonl").write_text(
        "".join(json.dumps(e) + "\n" for e in other))

    all_ws = ingest(str(tmp_path), workdir=tmp_path / ".brevet")
    assert set(all_ws["workspaces"]) == {WS, "wsp_other"}
    only = ingest(str(tmp_path), workdir=tmp_path / ".brevet2", workspace=WS)
    assert set(only["workspaces"]) == {WS}


def test_apply_patch_semantics():
    doc = {"a": {"b": 1}, "xs": [1, 2]}
    assert _apply_patch(doc, [{"op": "replace", "path": "/a/b", "value": 2}])["a"]["b"] == 2
    assert _apply_patch(doc, [{"op": "add", "path": "/xs/-", "value": 3}])["xs"] == [1, 2, 3]
    assert "a" not in _apply_patch(doc, [{"op": "remove", "path": "/a"}])
    assert _apply_patch(doc, [{"op": "move", "path": "/a"}]) is None  # unsupported -> verbatim diff only
    assert doc == {"a": {"b": 1}, "xs": [1, 2]}  # input never mutated


def test_reimport_after_a_lost_cursor_is_skipped(tmp_path):
    """A verdict imported once is never imported again, even when the cursor
    file is lost: each CHAP entry is identified by workspace and seq."""
    diff = [{"op": "replace", "path": "/summary", "value": "Formal tone"}]
    sink = _write_sink(tmp_path, _chain({
        "method": "decide.override",
        "params": {"diff": diff, "rationale": "too casual",
                   "tags": ["tone-formal"], "intent_preserved": True}}))
    wd = tmp_path / ".brevet"

    first = ingest(str(sink), workdir=wd)
    assert first["overrides"] == 1

    # the same judgment offered again from a fresh cursor: one override only
    (wd / "chap_cursor.json").unlink()
    second = ingest(str(sink), workdir=wd)
    assert second["overrides"] == 0
    assert second["duplicates_skipped"] == 1
    assert len(load_overrides(Ledger(wd / "ledger.jsonl"))) == 1


def _in_session_override(wd, family, draft, final):
    Ledger(wd / "ledger.jsonl").append("brevet.override", {
        "override_id": "ovr_local", "task_id": "env_local", "participant": "human:a@b.com",
        "intent_preserved": True, "diff": [], "draft": draft, "final": final,
        "rationale": "too casual", "tags": ["tone-formal"], "task_family": family})


def test_in_session_record_absorbs_one_matching_chap_verdict(tmp_path):
    """Both capture paths on one judgment yield one override, not two."""
    diff = [{"op": "replace", "path": "/summary", "value": "Formal tone"}]
    sink = _write_sink(tmp_path, _chain({
        "method": "decide.override",
        "params": {"diff": diff, "rationale": "too casual",
                   "tags": ["tone-formal"], "intent_preserved": True}}))
    wd = tmp_path / ".brevet"
    final = {**ARTEFACT, "summary": "Formal tone"}
    _in_session_override(wd, "write_email", json.dumps(ARTEFACT), json.dumps(final))

    out = ingest(str(sink), workdir=wd)
    assert out["overrides"] == 0 and out["duplicates_skipped"] == 1
    assert len(load_overrides(Ledger(wd / "ledger.jsonl"))) == 1


def test_identical_corrections_on_different_chap_tasks_all_count(tmp_path):
    """Three tasks corrected the same way are three judgments: dropping any
    of them would hide real recurrence from the dream cycle."""
    diff = [{"op": "replace", "path": "/summary", "value": "Formal tone"}]
    entries = [_entry(0, "workspace.create", {"profiles": ["core/1.0"]})]
    seq = 1
    for i in range(3):
        tid = f"tsk_{i}"
        entries += [
            _entry(seq, "task.create", {"from": "human:a@b.com", "kind": "write_email",
                                        "input": {"request": f"Draft email {i}"}}),
            _entry(seq + 1, "task.complete", {"from": "agent:x", "task_id": tid,
                                              "output": ARTEFACT}),
            _entry(seq + 2, "decide.override", {"from": "human:a@b.com", "task_id": tid,
                                                "diff": diff, "rationale": "too casual",
                                                "tags": ["tone-formal"],
                                                "intent_preserved": True}),
        ]
        seq += 3
    sink = _write_sink(tmp_path, entries)
    out = ingest(str(sink), workdir=tmp_path / ".brevet")
    assert out["overrides"] == 3 and out["duplicates_skipped"] == 0


def test_damaged_line_marks_the_chain_failed_but_ingests_the_rest(tmp_path):
    sink = _write_sink(tmp_path, _chain({"method": "decide.approve", "params": {}}))
    with sink.open("a") as f:
        f.write('{"seq": 6, "envelope": {"method": "decide.ove')  # half-written line
    out = ingest(str(sink), workdir=tmp_path / ".brevet")
    assert out["chain"] == "failed" and out["approvals"] == 1
    with pytest.raises(RuntimeError):
        ingest(str(sink), workdir=tmp_path / ".brevet2", strict=True)


def test_structural_check_requires_contiguous_seq():
    entries = [_entry(0, "workspace.create", {}), _entry(1, "x", {}), _entry(7, "y", {})]
    assert not _structural_ok(entries)


def test_missing_store_path_is_an_error_not_an_empty_store(tmp_path):
    with pytest.raises(FileNotFoundError):
        ingest(str(tmp_path / "typo.db"), workdir=tmp_path / ".brevet", workspace=WS)
    assert not (tmp_path / "typo.db").exists()


def test_a_sink_appended_to_twice_is_read_once(tmp_path):
    chain = _chain({"method": "decide.override",
                    "params": {"diff": [], "rationale": "r", "tags": ["tone-formal"]}})
    sink = _write_sink(tmp_path, chain + chain)  # the whole log relayed a second time
    summary = ingest(str(sink), workdir=tmp_path / ".brevet")
    assert summary["chain"] == "structural" and summary["overrides"] == 1
    ov = load_overrides(Ledger(tmp_path / ".brevet" / "ledger.jsonl"))[0]
    assert ov.task_family == "write_email"
