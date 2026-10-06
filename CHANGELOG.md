# Changelog

## 0.4.0 (2026-10-07)

The whole harness under change control, and the controls a production
deployment needs: anchored evidence, releases bound to their evals,
rollback, recalls confirmed where agents run, a tool broker, consent and
verified approvers.

### Added

- **Harness bill of materials.** Beside the capabilities, every release
  locks a digest of each part of the harness: the files named in
  `bindings.harness_files` (prompts, skills, tool code, `CLAUDE.md`, MCP
  configuration), what the live agent exposes (instructions, model,
  settings, tools with their schemas and functions, MCP servers, sub-agents,
  graph nodes) and library versions. Pydantic AI, AutoGen, LangGraph, Claude
  Agent SDK and OpenAI Agents agents are inventoried in depth. Secret values
  count by name only; the framework's own code is recorded by name, any
  other code is hashed.
- **Drift check.** Before each run, a wrapped agent compares its harness
  with its release; drift is recorded once per new state as a `brevet.drift`
  envelope and, outside the shadow channel, a changed file or agent
  component stops the run. `runtime_safety.harness` sets the policy; a value
  Brevet does not recognise blocks. `brevet harness` and `brevet_harness`
  show the bill of materials and any drift.
- **Anchored evidence chain.** After every governing step, the chain's head
  is written to anchors outside the workspace (`file:` logs and CHAP
  coordinators), from `runtime_safety.evidence.anchors`, `BREVET_ANCHORS` or
  `~/.config/brevet/anchors`. Each anchor names the workspace by an identity
  kept in the user's configuration, so a replaced workspace is caught under
  any agent name. A chain cut short, rewritten or replaced is refused by
  wrapped agents (which also replay the chain whenever it changed) and
  `brevet_active`, and reported by `brevet verify`, which also show
  governing steps still waiting to be anchored. `brevet anchor` and
  `brevet_anchor` anchor on demand.
- **Releases bound to their evals.** Every eval run records what it
  evaluated (cases, scorer, repeats, capabilities, harness and manifest) and
  each case's result with the tasks behind it; a release re-checks those
  against the evidence chain, re-scoring with the default scorer, so a run
  cannot claim results its tasks do not support.
  `agent.evaluate(baseline=True)` evaluates the current release and
  `agent.evaluate()` the next; `release(evals=(before, after))`,
  `brevet release --eval-before/--eval-after` and `brevet_release` bind a
  release to the two runs and take its deltas from them. Deltas without runs
  are recorded as attested by the approver; production releases need
  measured runs unless `runtime_safety.evals.allow_attested` is set. Eval
  cases run `runtime_safety.evals.repeats` times. `brevet evals` lists the
  runs, and a release the gate blocks is recorded as `brevet.gate`.
- **Rollback.** Every release keeps an archive of its manifest, lock and
  harness files in the workspace. `brevet rollback`, `agent.rollback()` and
  `brevet_rollback` return the agent to an earlier release as a new signed
  release, restoring its files (each re-hashed against the lock the chain
  vouches for) and capabilities minus anything recalled since; files added
  since are set aside on request, never deleted. Signed approvals cover
  rollbacks like any release.
- **Recall acknowledgements.** A wrapped agent receives its governed rules
  with each task (`runtime_safety.serve_rules`: `context`, `prompt` or
  `none`), so a recalled rule is withheld from the next task, which records
  a `brevet.recall_ack`. An agent that does not take its rules from Brevet
  refuses to run outside shadow while its release ships a recalled
  capability. `brevet_active` lists recalled rules, and recalls of earlier
  releases still to confirm, and sessions confirm with `brevet_acknowledge`.
  `brevet recalls` shows what is still open.
- **Duplicate recalls.** A recall with `reason_class: duplicate` withdraws
  one byte-identical copy of a capability and leaves its content to the copy
  that stays; it is refused when no other copy holds the content.
- **Tool broker.** The agent's tool calls are checked against the read,
  suggest, act and controlled_act tiers under `bindings.tools`: undeclared
  tools are refused, act tools never run in shadow, and controlled_act tools
  need production and a person's grant per call. Budgets cap calls and
  errors per task. Refusals and act-class calls are recorded as
  `brevet.tool_call`. The broker reaches Claude Agent SDK, OpenAI Agents,
  LangChain and LangGraph (subgraphs included), Pydantic AI (toolsets and MCP
  servers included) and AutoGen tools, `@agent.tool` guards your own, and
  `brevet hook` enforces the released tiers in Claude Code as a PreToolUse
  hook that refuses any call it cannot check, including every call while
  the evidence chain does not replay or does not match its anchors.
- **Consent.** The dream cycle learns only from the participants its
  consent scope allows (`consented_sources_only` with `consented`, or
  `all_recorded`). `brevet consent withdraw` and `agent.withdraw_consent()`
  stop learning from a participant and recall everything built on their
  overrides.
- **Conditions and validity windows.** Rules are served only inside their
  validity window and only where their conditions match the task. A rule
  restricted to a role, risk class or environment, or carrying a trigger, is
  served only where the task establishes it, and an exclusion found in the
  task's text vetoes it. Wrapped agents match each task's text and the
  condition fields of its context; `brevet_active` accepts the task's
  family, domain, role, environment, risk class and text.
- **Verified approvers.** Approver keys are checked against an
  allowed-signers file or the keys GitHub publishes for each approver
  (`runtime_safety.approvals` in the manifest the workspace uses, named with
  `--manifest-path` where it is not beside the workspace), and an existing
  Ed25519 SSH key can be the approver key (`brevet approver add --ssh-key`,
  with the `ssh` extra).
- **Governed-adaptation profile.** `brevet benchmark` scores a workspace's
  lineage on improvement, regression discipline, lineage completeness and
  recall compliance, with the cost of governance beside them.

### Changed

- Outside the shadow channel, a wrapped agent runs only the latest release
  of its agent on the evidence chain: the manifest must match that release
  and verify against the signing key the chain recorded, its
  `capabilities.lock` must be the one the release names, and the release
  must carry valid approver signatures once approvers are registered. A
  running agent picks up releases made by other processes, and
  `agent.release()` ships `agent.yaml` as it is on disk.
- `brevet release` and the MCP server carry over the agent and library
  components of the last release, since they cannot see the running agent.
- A release locks only capabilities whose promotion is on the evidence
  chain, with valid signatures once approvers are registered.
- `brevet_active` withholds rules whose conditions changed or that were
  switched off without a recall, lists recalled ones, no longer serves
  unhashed titles, and serves nothing when the release it would serve
  carries invalid signatures, lacks required signatures or does not match
  `agent.yaml`. Other invalid decisions, which grant nothing, are reported.
- Releases write `agent.yaml` and `capabilities.lock` atomically. A release
  made before archives existed is archived the first time its manifest and
  lock are seen on disk exactly as released.
- Each envelope names the runtime that wrote it (`runtime`).
- `brevet demo` and the worked example use the same mission group,
  `mission_group:quality_team`, and both lock the eval cases with the rule.
- The interactive tour (`docs/demo.html`) marks its scenario as
  illustrative, works by keyboard, keeps Back and Next in reach on phones and
  lets its diagrams scroll sideways there rather than shrink. The diagrams
  use Brightbeam ember (#EA4700).
- The Claude Cowork example's `apply` writes exactly what `brevet_active`
  would serve, the tool records and releases the same agent as the MCP
  server, `setup.sh` leaves an unchanged manifest alone, and the weekly
  digest anchors the week's evidence.

### Fixed

- Release approvals cover the manifest and the eval evidence as well as the
  lock, and a release request carries the harness it was made with.
- Replaying an old approver registration never restores a revoked approver.
- No member acting alone can weaken a mission group: changing its threshold,
  adding or removing a member, or replacing a member's key needs the group's
  own quorum. An approver can always withdraw their own key. Records signed
  under 0.3.0 replay under the rules they were made with only while they are
  older than the chain's first envelope written by 0.4.0.
- A signed register change applies only to the state of the register it
  was requested for, so an old or failed request cannot be replayed later or
  on another workspace.
- `brevet verify` and `brevet_verify` check every registered approver's key
  against the identity source, however it reached the register.
- Allowed-signers entries with options OpenSSH does not accept vouch for
  nothing.
- A wrapped agent given its manifest as an object keeps its lock in the
  working directory.

## 0.3.0 (2026-10-06)

Signed approvals: an agent can request a decision, but only a person can
sign it.

### Added

- Signed approvals. Once a workspace registers its first approver with
  `brevet approver add`, every dawn decision, release and recall must be
  signed with a passphrase-protected Ed25519 key kept outside the workspace.
  Anyone, an agent over MCP included, may request a decision; `brevet approve`
  signs it, and it is applied once the named human, or the threshold of a
  mission group's members, has signed. A signed request is bound to the exact
  decision and can be applied only once.
- The approver register is kept on the evidence chain (`brevet.approver`
  envelopes): the first approver registers themselves, later changes need an
  existing approver's signature, and new keys sign their own registration.
- `brevet verify`, `brevet_verify` and `brevet_active` check every approval
  signature against the register as it stood at the time; `brevet_active`
  serves nothing if any is invalid.
- When signing is required, the MCP dawn, release and recall tools return a
  request with the command that signs it instead of acting.

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
