# Contributing to Brevet

Thanks for your interest. Brevet's contract is compact: five JSON Schemas
define the records, [SPEC.md](SPEC.md) states the rules, and one Python
runtime implements them.

## Setup

```console
$ git clone https://github.com/BrightbeamAI/brevet && cd brevet
$ pip install -e ".[dev]"
$ pytest          # runs offline
$ ruff check .
$ brevet demo     # the whole loop once, on example data
```

## What contributions land well

- **Framework adapters.** One small class that checks the shape of the
  agent object, tested against stand-ins (see `tests/test_adapters.py`). No
  framework may become a required dependency.
- **Schema/spec issues.** If the schemas or `SPEC.md` under- or
  over-constrain something you hit in practice, open an issue with the
  concrete case.
- **Benchmark work.** `brevet benchmark` scores a workspace on the four
  axes of `BENCHMARK.md`; datasets, baselines and comparisons across
  systems are welcome.

## Ground rules

- The authority invariants in `SPEC.md` section 2 are not negotiable:
  nothing in Brevet may raise a capability's authority without a `human:` or
  `mission_group:` approver, and no machine identity may promote, release or
  recall. Pull requests that weaken these will be
  declined, however convenient the resulting API.
- No network access is required in the core: the tests and the demo must
  pass offline. Model assist and CHAP mirroring stay optional and fail
  safely.
- Keep dependencies minimal (currently: pydantic, typer, PyYAML,
  cryptography and the MCP SDK).
- New behaviour needs a test; changed schemas need a round-trip test in
  `tests/test_schemas.py`.

## Style

`ruff check .` must pass (line length 100). Prefer small modules whose
docstring says, in plain words, what the module is for as well as how it
works. User-facing text (CLI messages, demo output, generated rule text)
should use the terms in [GLOSSARY.md](GLOSSARY.md), with a plain explanation
wherever a newcomer meets a term first. The diagrams
in `docs/assets/` are generated: edit `scripts/make_diagrams.py` and rerun it
rather than editing the SVG files by hand; the run also refreshes the copies
embedded in `docs/demo.html`.

## Releasing

1. Set the new version in `pyproject.toml`, `brevet/__init__.py`,
   `server.json` (twice) and `CITATION.cff` (with `date-released`), date its
   CHANGELOG entry, and update the supported versions in `SECURITY.md` and
   the draft number in `SPEC.md`'s title when they change.
2. Run `python scripts/make_pypi_readme.py`, then `pytest`, which checks that
   the generated files are current.
3. Commit, tag `v<version>` and push both. Push the tag before uploading: the
   README on PyPI loads its figures from that tag.
4. `python -m build`, `twine check dist/*`, then `twine upload dist/*`.
5. Create the GitHub release from the tag with the CHANGELOG entry and the
   two files in `dist/`. Publishing the release runs the *Publish to MCP
   Registry* workflow, which waits for the version on PyPI and publishes
   `server.json` with GitHub OIDC; it can also be run by hand from the Actions
   tab. (Publishing from a laptop with `mcp-publisher login github` works only
   for owners of the BrightbeamAI organisation.)

## Licence

Apache-2.0. By contributing you agree your contributions are licensed under
the same terms.
