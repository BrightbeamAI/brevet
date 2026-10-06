# Contributing to Brevet

Thanks for your interest. Brevet is early (version 0.1) and deliberately
small: five JSON Schemas define the records, [SPEC.md](SPEC.md) states the
rules, and one Python runtime implements them.

## Setup

```console
$ git clone https://github.com/BrightbeamAI/brevet && cd brevet
$ pip install -e ".[dev]"
$ pytest          # runs offline (the CHAP mirror test needs the [chap] extra)
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
- **Benchmark work.** `BENCHMARK.md` is a design specification;
  implementations of its four scored axes are welcome.

## Ground rules

- The authority invariants in `SPEC.md` section 2 are not negotiable:
  nothing in Brevet may raise a capability's authority without a human or
  mission-group approver, and an endogenous candidate can never be promoted
  by the process that proposed it. Pull requests that weaken these will be
  declined, however convenient the resulting API.
- No network access is required in the core: the tests and the demo must
  pass offline. Model assist and CHAP mirroring stay optional and fail
  safely.
- Keep dependencies minimal (currently: pydantic, typer, PyYAML,
  cryptography).
- New behaviour needs a test; changed schemas need a round-trip test in
  `tests/test_schemas.py`.

## Style

`ruff check .` must pass (line length 100). Prefer small modules whose
docstring says, in plain words, what the module is for as well as how it
works. User-facing text (CLI messages, demo output, generated rule text)
should use the terms in [GLOSSARY.md](GLOSSARY.md), the same ones the paper
uses, with a plain explanation wherever a newcomer meets a term first. The diagrams
in `docs/assets/` are generated: edit `scripts/make_diagrams.py` and rerun it
rather than editing the SVG files by hand.

## Licence

Apache-2.0. By contributing you agree your contributions are licensed under
the same terms.
