# Changelog

## 0.2.0 (2026-10-06)

First release on PyPI and the MCP Registry.

### Added

- `pip install brevet` and `uvx brevet mcp`: the MCP SDK is now a core
  dependency, and `server.json` describes the server for the MCP Registry.
- MCP tools `brevet_record` (records a draft and the expert's final as
  evidence), `brevet_active` (serves the governed rules of the latest
  release, after checking them) and `brevet_chap_ingest`.
- `brevet chap-ingest` imports CHAP review verdicts from an audit sink, a
  coordinator store or a served coordinator. An override keeps its diff,
  rationale, tags and `intent_preserved`; a rejection becomes a substituting
  override; an approval becomes a draft accepted as it was.
- `BREVET_HOME`, `BREVET_WORKDIR` and `BREVET_MANIFEST` tell the MCP server
  where its workspace is, since clients can start servers from any directory.
- `examples/pump_vibration.py`, the README's worked example as a runnable
  script.
- In `examples/claude-cowork`: a CHAP-enabled setup path, a weekly dawn
  digest template, a tool-permission template and always-on capture
  families.
- `scripts/make_diagrams.py` regenerates every diagram, checks that no text
  overflows its box and refreshes the copies embedded in `docs/demo.html`;
  `scripts/make_pypi_readme.py` writes the README shown on PyPI.

### Changed

- Releases sign the manifest together with the digest of `capabilities.lock`
  and record the signing key on the evidence chain.
- Outside the shadow channel, a wrapped agent runs only while its manifest
  signature verifies.
- `brevet_active` serves nothing when the evidence chain is broken, the lock
  does not match the digest recorded for its release, or the release
  signature fails, and it withholds any rule whose stored content no longer
  matches its hash.
- Only `human:` and `mission_group:` identities can promote, release or
  recall, checked strictly. Promotion to Controlled needs a mission group, and
  a recalled or rejected capability can no longer be decided at dawn or be
  recalled twice.
- The dream cycle adds only what is new: a group of overrides already
  proposed is not proposed again, a candidate with more evidence supersedes a
  pending one, and eval cases are compiled once per override.
- Later releases leave out a recalled capability and any capability with
  identical content.
- Eval cases are dealt alternately into held-in and held-out halves by
  content hash, so both halves have cases; cases without an expert final are
  skipped.
- Automatic capture over MCP is opt-in (`BREVET_AUTO_CAPTURE=1` or
  `runtime_safety.evidence.auto_capture`), and the MCP tools carry read-only
  and destructive hints.
- Documentation rewritten so that each of the paper's terms comes with a
  plain explanation: a short README built around a worked example, an
  ABOUT.md that walks through the loop, new diagrams in light and dark
  versions, a glossary that gives each term's plain meaning first, and a
  revised interactive tour. The earlier diagrams and the animated GIF were
  replaced.
- Candidate rules drafted by the dream cycle read in plain language at the
  dawn gate.
- Clearer CLI: refusals print one line instead of a traceback, `brevet
  verify` reports where a broken chain first fails, `brevet release` takes
  `--rationale`, `brevet recall` takes `--action`, and `brevet init` does not
  overwrite an existing manifest without `--force`.

### Fixed

- Concurrent writers (an MCP server, a scheduled import, the CLI) can no
  longer break the evidence chain: appends take a file lock.
- A damaged line in the evidence chain is reported as a break instead of
  crashing `verify`, `status` and `record`.
- The MCP server reports why a call was refused on both MCP SDK versions,
  and explains how to set its workspace instead of crashing when started
  from a read-only directory.
- CHAP import keeps identical corrections made on different tasks, matches
  an in-session record to at most one CHAP verdict, never imports a verdict
  twice even if its cursor is lost, and reports a half-written line or a
  mistyped store path instead of failing or creating an empty store.
- A plain function no longer runs twice when it raises, and a user module
  named `agents` is no longer mistaken for the OpenAI Agents SDK.
- Evaluating with a CHAP override whose final could not be reconstructed no
  longer crashes.
- Files are read and written as UTF-8 on every platform.

### Security

- The signing key is created only at the first release and is readable only
  by its owner, and the workspace gets a `.gitignore`.
- The playground refuses cross-site requests.

## 0.1.0 (2026-08-02)

First public release.

- Added: `examples/claude-cowork`, a complete case study that governs
  what Claude Desktop / Cowork itself learns: a two-minute setup script,
  a capture skill with precise override semantics for chat, a bridge CLI
  (record / dream / dawn / release / recall / apply / check), and a
  governed rules file that sessions load only after the evidence chain
  verifies.
- Fixed: the MCP server now works with both the 1.x and 2.x MCP SDKs
  (SDK 2.0 moved `FastMCP` to `mcp.server.mcpserver.MCPServer`), and
  server startup errors are written to stderr so they can no longer
  corrupt the JSON-RPC stream read by the connected client.

- Added: `brevet playground`, a local web UI that drives the governed
  evolution loop step by step against a real workspace: live evidence
  chain, the rejected self-promotion attempt, eval gating, the signed
  lockfile, and recall. Standard library only; nothing leaves your
  machine.
- Fixed: dawn-gate promotions record the approver in capability
  provenance for every authority layer, so `capabilities.lock` always
  answers "who approved this" (Advisory promotions previously showed
  `approved_by: "unrecorded"`).

- Visual documentation: loop, envelope, authority-ladder, and
  employee-analogy diagrams plus an animated demo GIF (`docs/assets/`), an
  interactive story tour (`docs/demo.html`), and a project glossary
  (`GLOSSARY.md`).

- `brevet.wrap()`: zero-config wrapping with auto-detection for ten
  frameworks: LangGraph, Claude Agent SDK, DeepAgents, AutoGen, LlamaIndex,
  Pydantic AI, Google ADK, CrewAI, OpenAI Agents SDK, and any callable.
- Override harvesting: the draft/final diff is classified refining vs
  substituting and chained as evidence, no manual annotation.
- The delta engine (`dream`): clusters overrides by failure signature and
  emits Evidence-layer candidate capability objects with provenance.
- Dawn gate: human-only promotion (`promote | hold | reject | re_elicit`);
  approver identities in `agent:*`, `model:*`, `dream:*` namespaces are
  rejected.
- Override-compiled evals with deterministic held-in/held-out splits and the
  conservative acceptance gate.
- Signed releases (Ed25519 over canonical JSON) with `capabilities.lock`,
  the Capability Bill of Materials.
- Recall: CVE-style capability withdrawal with affected-release enumeration,
  provable from the evidence chain alone.
- Hash-linked append-only evidence ledger with independent replay
  (`verify`).
- MCP server (`brevet mcp`) exposing the full lifecycle; authority
  invariants hold over MCP.
- Optional local model assist via Ollama (drafts only, always logged, fails
  soft to deterministic templates).
- CHAP mirroring through the official `chap-coordinator` package (on
  PyPI):
  embedded in-process with a SQLite store, or to a served coordinator
  over CHAP Core JSON-RPC, with an offline-tolerant outbox.
- Five JSON Schemas as the normative contract (`schemas/`), a normative
  spec (`SPEC.md`), and a benchmark design note (`BENCHMARK.md`).
