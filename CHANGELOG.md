# Changelog

## Unreleased

- Added: **native CHAP evidence source** (`brevet/chap_evidence.py`,
  `brevet chap-ingest`, MCP tool `brevet_chap_ingest`). The inbound
  counterpart to `chap_bridge`: ingests a CHAP audit chain — a JSONL
  `on_audit` sink (file or synced folder), a coordinator SQLite store,
  or a served coordinator URL — and turns human verdicts into brevet
  evidence. `decide.override` carries its diff, rationale, tags and
  `intent_preserved` through verbatim; `decide.reject` lands as a
  substituting override; `decide.approve` as accepted-verbatim.
  Store/URL sources are queried through the official
  `chap-coordinator` (`audit.read`), with `--strict` running
  `audit.verify_chain` first; JSONL sources get a structural chain
  check. Idempotent via a per-source seq cursor. Deployments that
  capture reviews through CHAP (e.g. Claude Cowork with a chap-capture
  skill) no longer need parallel `brevet_record` calls for verdicts:
  CHAP is the capture surface, brevet remains the learning gate.

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
