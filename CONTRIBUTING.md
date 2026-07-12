# Contributing to Brevet

Thanks for your interest. Brevet is early (v0.1.x) and the contract surface
is deliberately small: five JSON Schemas, one spec, one runtime.

## Setup

```console
$ git clone https://github.com/BrightbeamAI/brevet && cd brevet
$ pip install -e ".[dev]"
$ pytest          # 33 tests, sub-second (chap mirror test needs the [chap] extra)
$ ruff check .
$ brevet demo     # the whole loop on synthetic data
```

## What contributions land well

- **Framework adapters.** One class, duck-typed, tested against fakes (see
  `tests/test_adapters.py`). No framework may become a hard dependency.
- **Schema/spec issues.** If the schemas or `SPEC.md` under- or
  over-constrain something you hit in practice, open an issue with the
  concrete case.
- **Benchmark work.** `BENCHMARK.md` is a design note; implementations of
  the scored axes are welcome.

## Ground rules

- The authority invariants in `SPEC.md` §2 are not negotiable: nothing in
  Brevet may promote a capability without a human or mission-group approver,
  and endogenous candidates never self-promote. PRs that weaken these will
  be declined regardless of how convenient the resulting API is.
- Zero required network access in the core: tests and the demo must pass
  offline. Model assistance and CHAP mirroring stay optional and fail soft.
- Keep dependencies minimal (currently: pydantic, typer, PyYAML,
  cryptography).
- New behaviour needs a test; changed schemas need a round-trip test in
  `tests/test_schemas.py`.

## Style

`ruff check .` must pass (line length 100). Prefer small modules with a
docstring that states the doctrine of the module, not just its mechanics.

## Licence

Apache-2.0. By contributing you agree your contributions are licensed under
the same terms.
