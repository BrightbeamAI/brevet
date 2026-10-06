"""Files generated from other files stay in step with their sources: the
PyPI README, the figures embedded in the tour, the example output quoted in
ABOUT.md, and the MCP Registry entry."""

import contextlib
import importlib.util
import io
import json
import re
from pathlib import Path

import brevet

ROOT = Path(__file__).resolve().parent.parent


def _load(path: Path):
    spec = importlib.util.spec_from_file_location(path.stem, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_pypi_readme_is_current_and_fully_absolute():
    assert _load(ROOT / "scripts" / "make_pypi_readme.py").main(["--check"]) == 0
    text = (ROOT / "README_PYPI.md").read_text(encoding="utf-8")
    assert "<picture" not in text and "<source" not in text
    for url in re.findall(r'(?:src|href)="([^"]+)"|\]\(([^)]+)\)', text):
        link = url[0] or url[1]
        assert link.startswith(("https://", "#")), link


def test_pypi_readme_figures_exist_in_the_repository():
    text = (ROOT / "README_PYPI.md").read_text(encoding="utf-8")
    for path in re.findall(r"raw\.githubusercontent\.com/BrightbeamAI/brevet/v[^/]+/([^\"]+)", text):
        assert (ROOT / path).is_file(), path


def test_tour_embeds_the_current_diagrams():
    assert _load(ROOT / "scripts" / "embed_demo_assets.py").main(["--check"]) == 0


def test_about_quotes_the_example_output():
    about = (ROOT / "ABOUT.md").read_text(encoding="utf-8")
    quoted = re.search(r"Running it prints:\n\n```text\n(.*?)```", about, re.DOTALL)[1]
    example = _load(ROOT / "examples" / "pump_vibration.py")
    out = io.StringIO()
    with contextlib.redirect_stdout(out):
        example.main()
    printed = re.sub(r"Records are in \S+", "Records are in <temporary folder>", out.getvalue())
    assert printed == quoted


def test_registry_entry_matches_the_package():
    server = json.loads((ROOT / "server.json").read_text(encoding="utf-8"))
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    assert f"mcp-name: {server['name']}" in readme
    assert server["version"] == brevet.__version__
    assert all(p["version"] == brevet.__version__ for p in server["packages"])
    assert len(server["description"]) <= 100
    pyproject = (ROOT / "pyproject.toml").read_text(encoding="utf-8")
    assert f'version = "{brevet.__version__}"' in pyproject
