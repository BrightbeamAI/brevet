"""Keep every test away from the developer's own Brevet configuration."""

import pytest


@pytest.fixture(autouse=True)
def _isolated_config(tmp_path_factory, monkeypatch):
    home = tmp_path_factory.mktemp("brevet-config")
    monkeypatch.setenv("BREVET_CONFIG_DIR", str(home / "config"))
    monkeypatch.setenv("BREVET_APPROVER_DIR", str(home / "approvers"))
    monkeypatch.delenv("BREVET_ANCHORS", raising=False)
    monkeypatch.delenv("BREVET_ALLOWED_SIGNERS", raising=False)
