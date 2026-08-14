"""A missing sink is 'no evidence yet', not a failure: scheduled ingestion
runs before the CHAP audit folder exists and must stay quiet about it."""

from brevet.chap_evidence import ingest


def test_missing_path_yields_empty_summary(tmp_path):
    summary = ingest(str(tmp_path / "nope" / "audit-wsp_x.jsonl"),
                     workdir=tmp_path / ".brevet")
    assert summary["workspaces"] == {}
    assert summary["overrides"] == summary["approvals"] == summary["rejections"] == 0


def test_empty_directory_yields_empty_summary(tmp_path):
    empty = tmp_path / "sink"
    empty.mkdir()
    summary = ingest(str(empty), workdir=tmp_path / ".brevet")
    assert summary["workspaces"] == {} and summary["chain"] == "structural"


def test_missing_path_is_not_a_strict_failure(tmp_path):
    # strict guards chain integrity, not sink existence
    summary = ingest(str(tmp_path / "nope"), workdir=tmp_path / ".brevet",
                     strict=True)
    assert summary["workspaces"] == {}
