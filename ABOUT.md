# About Brevet

The detail behind the [README](README.md): how the governed evolution loop
works, how Brevet is built and how the repository is organised. For
definitions of every term, see the [GLOSSARY](GLOSSARY.md). For the rules any
implementation must follow, see [SPEC.md](SPEC.md).

## Why Brevet exists

Organisations already know how to manage change when people learn on the
job. When someone wants to change how a task is done, they write the change
down, a named person approves it, the procedure gets a new version number,
and a bad change can be rolled back. Those steps are how trust in a team
extends beyond the people who know each other personally.

AI agents now learn on the job too. They save memories, add skills and edit
their own instructions, but those changes skip every one of those steps.
That leaves three questions without answers:

1. **What has the agent learned?** Not the model's weights, but the rules,
   skills, memories and tool permissions it has picked up since it was
   deployed.
2. **Who approved it?** A learned behaviour changed a real decision. Which
   accountable person said it could be used, and on what evidence?
3. **How do we take it back?** A learned rule is wrong. Where does it live,
   which versions included it, and what shows it is no longer in use?

Brevet gives an agent's learning the same steps a team's procedures already
have. It is not an agent framework and does not replace one. It wraps the
framework you already use and keeps the records around it.

## The governed evolution loop

The loop has seven stages. Each one appends its envelopes to the evidence
chain.

| Stage | What happens | Who acts | What is recorded |
|---|---|---|---|
| **Work** `run()` | The agent drafts under one signed harness. | the agent | `brevet.task`, `brevet.artefact` |
| **Override** `record_final()` | An expert corrects the draft; the difference and the reason become an override. | an expert | `brevet.override` |
| **Dream** `dream()` | Offline, recurring overrides become candidate capabilities and eval cases, with no authority. | Brevet | `brevet.candidate` |
| **Dawn** `dawn()` | Each candidate is promoted, held, rejected or sent back for re-elicitation. | a named human or mission group | `brevet.promotion` |
| **Evals** `evaluate()` | Overrides replay as tests. The conservative gate needs neither half (held-in or held-out) to get worse and at least one to improve. | Brevet | `brevet.eval_run` |
| **Release** `release()` | Promoted capabilities ship in a signed release with its `capabilities.lock`. | a named human or mission group | `brevet.release` |
| **Recall** `recall()` | A capability is withdrawn, and every release that shipped it is flagged. | a named human or mission group | `brevet.recall` |

The names follow the loop's day-and-night rhythm, which the paper calls the
circadian contract. Awake, the agent works under one signed version and does
not change itself. While it sleeps, the dream cycle mines what the day's
overrides imply. At dawn, people review what the night proposed, and only what
they promote reaches the agent, through a signed release.

Overrides come in two kinds. A **refining** override keeps the agent's decision
and changes its wording. A **substituting** override reaches a different
decision, as when *minor* becomes *major*. The dream cycle treats them as soft
and hard signals.

The dream cycle adds only what is new, so it can run every night. A group of
overrides that already produced a candidate is not proposed again, whatever
happened to that candidate since. When more overrides join the group, the new
candidate supersedes the pending one, and a candidate whose content matches a
recalled capability is never proposed.

## The authority ladder

<p align="center">
  <picture>
    <source media="(prefers-color-scheme: dark)" srcset="docs/assets/levels-dark.svg">
    <img src="docs/assets/levels-light.svg" alt="The authority ladder: Evidence holds candidates with no operational authority; promotion at the dawn gate by a named human raises a capability to Advisory, where it may inform drafts a human still checks; promotion by the mission group raises it to Controlled, where it may drive actions directly. A recalled capability leaves future releases and every release that shipped it is flagged." width="860">
  </picture>
</p>

Every capability holds one authority layer. It enters at **Evidence**, with no
operational authority. Promotion at the dawn gate raises it to **Advisory**,
where it may inform what the agent drafts while a human still checks each
result. **Controlled**, where it may drive actions directly, needs promotion
by a mission group. Only `human:` and `mission_group:` identities can decide;
every other namespace, including `agent:`, `model:` and `dream:`, is refused.
Rejection is not deletion: rejected candidates stay on the evidence chain, so
"was this ever proposed, and why did we say no?" always has an answer.

## Signed approvals

An identity such as `human:alice@example.com` is a name anyone can type,
including an agent that can call Brevet's tools. Signed approvals make each
decision provable: once a workspace registers its first approver, every dawn
decision, release and recall must be signed with a key that an agent does
not hold.

```console
brevet approver add --identity human:alice@example.com --group mission_group:quality_team
brevet dawn --decide cap_123:promote --approver mission_group:quality_team
brevet approve req_7f3a9c2e41d0     # shows the request, asks for the passphrase, signs
```

- **Keys stay with people.** `brevet approver add` creates an Ed25519 key,
  encrypted with a passphrase, in `~/.config/brevet/approvers/` (or
  `BREVET_APPROVER_DIR`), outside every workspace. With `--ssh-key`, the
  person's existing Ed25519 SSH key is the approver key instead.
- **Request, sign, apply.** Anyone may request a decision, an agent over MCP
  included; nothing changes until the people it needs sign it with
  `brevet approve`. The signed request is bound to the exact decision: the
  capability's content hash for a promotion; the lock's digest and the
  manifest for a release. A release whose promotions changed after signing
  is refused, and a signed request can be applied only once.
- **Mission groups sign through their members.** A decision taken as
  `mission_group:<name>` needs signatures from that group's members, one by
  default or more with `brevet approver threshold`.
- **The register is on the evidence chain.** Each registration, revocation
  and threshold is a `brevet.approver` envelope. The first approver registers
  themselves; every later change needs an existing approver's signature, and
  a new key signs its own registration. `brevet verify` replays the register
  and checks every signature as it stood at the time. A decision with invalid
  signatures grants nothing; if the release being served or run carries
  them, nothing is served or run.
- **No member alone can weaken a group.** Changing a mission group's
  threshold, adding or removing a member, or replacing a member's key needs
  the group's own quorum, counted over its members before the change. An
  approver can always withdraw their own key. A group that needs every
  member's signature cannot replace a member's lost key without it, so set a
  threshold below the group's size.
- **A register change applies to one state of the register.** Each request
  names the last register change it follows. If the register changes before
  it is applied, it is refused and must be requested again, so an old or
  failed request cannot be replayed later or on another workspace.

Each key is checked against an identity source the organisation already
trusts, named under `runtime_safety.approvals`: an OpenSSH allowed-signers
file, the format git uses for signed commits (`allowed_signers: <path>`, or
`BREVET_ALLOWED_SIGNERS`), or GitHub (`github: true`, with each approver
naming their account through `--github`; the key must be one GitHub publishes
for it). A registration whose key the source does not list is refused, and
`brevet approver list` and `brevet verify` re-check every active approver,
however it reached the register. Where the manifest is
not beside the workspace, name it with `--manifest-path`. Register at least
two approvers, so that a lost key can be revoked and replaced by the other.

## The harness under change control

The capabilities are what the agent learned. The harness is everything else
that decides what it does: its instructions, skills, tools, settings and
libraries. Every release locks both: beside the capabilities,
`capabilities.lock` holds a harness bill of materials, a digest for each
component and never the content itself.

- **Files you name.** List the files and folders that shape the agent under
  `bindings.harness_files`: prompts, skills, tool code, `CLAUDE.md`, an MCP
  configuration. Hidden files count; `.brevet/`, `.git/`, `node_modules/`
  and folders named `venv` or `.venv` do not. A pattern that matches no file
  is reported, so a misspelt folder does not pass unnoticed.
- **The live agent.** A wrapped agent is described from what the object
  exposes: its code, instructions, model and settings, each tool's name,
  description, input schema and function, its MCP servers, sub-agents and
  graph structure. What is exposed varies by framework, so name the files
  that define prompts and tools kept out of sight, for example inside a
  compiled graph. Secret values, such as API keys and tokens, count by name
  only, so rotating one is not a change; every other setting counts in full.
- **Library versions.** Brevet, Python and the framework's own packages.
  Their code is recorded by name, not by source, so a framework upgrade
  appears here rather than as a change to the agent. Any other code, your
  own installed packages included, is hashed.

```yaml
bindings:
  harness_files: [prompts/, skills/, CLAUDE.md, .mcp.json]
runtime_safety:
  harness: {on_drift: block, env: record}   # the defaults
```

Releasing from Python locks all three and ships `agent.yaml` as it is on
disk. `brevet release` and the MCP server cannot see the running agent, so
they lock the named files and carry over the agent and library components
of the last release; release changes to the agent itself from Python. Before
each run, the wrapped agent takes the same inventory and compares it with its
release.
Any difference is drift, recorded once for each new state as a
`brevet.drift` envelope. Outside the shadow channel, a changed file or agent
component stops the run until the change is released or undone. A library
upgrade is recorded but does not stop the run unless `env: block` is set;
`on_drift: record` records without stopping, and a value Brevet does not
recognise blocks. `agent.evaluate()` runs regardless, since evaluating a
change is how it earns a release. With signed approvals, a release request
carries the harness it was made with, and applying it is refused if the
named files have changed since. `brevet harness` and the `brevet_harness`
MCP tool show the bill of materials and any drift.

## Running in production

### Rules served where the agent runs

With each task, a wrapped agent receives the governed rules of its release,
checked as `brevet_active` checks them (`runtime_safety.serve_rules`):

```yaml
runtime_safety:
  serve_rules: context   # context["brevet"]["rules"], for a function that takes a context
                         # prompt: a block of rules before the task text, for any framework
                         # none: you serve them yourself
```

A rule is served only inside its validity window (`valid_from`,
`valid_until`) and only where its conditions match the task, conditions
first. A rule's scope (task family, domain, model family, operating mode) is
judged where the task states it. A restriction (role, risk class,
environment) and a trigger hold only where the task establishes them, so a
rule for high-risk work is not served to a task whose risk is unknown, and an
exclusion found in the task's text vetoes the rule. Pass the condition fields
with each task:

```python
agent.run("Pump P-301: vibration high during cleaning", task_family="equipment_triage",
          context={"role": "reliability_engineer", "risk_class": "high"})
```

Each task records the rules it was given.

### Releases bound to their evals

`agent.evaluate(baseline=True)` evaluates the current release and
`agent.evaluate()` what the next release would ship. Each run records what
it evaluated: the cases, the scorer, the repeats (`runtime_safety.evals.repeats`)
and the capabilities, harness and manifest it ran with. A release bound to
two runs takes its deltas from them, and is refused if the 'after' run did
not evaluate exactly what it locks:

```python
before = agent.evaluate(baseline=True)
after = agent.evaluate()
agent.release(to_version="0.2.0", channel="trial", approver="human:qa@site",
              evals=(before, after))
```

From the terminal, `brevet evals` lists the runs and `brevet release
--eval-before <run> --eval-after <run>` binds them. Deltas given without runs
are recorded as attested by the approver. Production releases need measured
runs, unless the manifest sets `runtime_safety.evals.allow_attested`. A
release the gate blocks is recorded too.

### Rollback

Every release keeps a copy of what it shipped in the workspace: the signed
manifest, the lock and the content of every harness file it named.
`brevet rollback 0.2.0 --approver human:qa@site` (or `agent.rollback`, or the
`brevet_rollback` tool) returns the agent to that release as a new signed
release: the earlier manifest and files come back, and so do its
capabilities, minus any recalled or decided against since. The release
record names the version it restores, and the rollback runs on that
release's channel or a lower one: a wider channel needs a release with its
own evidence. Files added since that release are set aside in the workspace
with `--remove-added`, never deleted, and a harness file reached through a
symlink that leads elsewhere is refused. Code that lives outside the named
files comes back from version control, and the drift check holds the agent
until it matches.

### Recalls confirmed where agents run

A recalled rule is withheld from the next task, and the agent's first task
after the recall records a `brevet.recall_ack`. An agent that does not take
its rules from Brevet cannot be shown to have stopped, so outside the shadow
channel it refuses to run until a release leaves the capability out. In an
MCP session, `brevet_active` lists recalled rules, the assistant stops
applying them and confirms with `brevet_acknowledge`; recalls that flagged an
earlier release and are not in the one served are listed under
`recalls_to_confirm` and confirmed the same way. `brevet recalls` shows each
recall with the agents still to confirm it. To remove a byte-identical copy
of a rule without recalling its content, recall the copy with
`--reason-class duplicate`: the copy that stays keeps serving.

### The tool broker

The manifest declares the agent's tools in four tiers, the read/act
contract, and the broker checks the agent's tool calls against them:

```yaml
bindings:
  tools:
    read: [search, "mcp__github__get_*"]   # always allowed
    suggest: [draft_reply]                 # always allowed
    act: [send_email]                      # trial and production, never shadow
    controlled_act: [issue_refund]         # production, with a person's grant per call
```

Declaring any tool switches the broker on; an undeclared tool is then
refused. `runtime_safety.loop.budgets` caps tool calls and tool errors per
task. Refusals and every act or controlled_act call are recorded as
`brevet.tool_call` envelopes, with the person who granted the call. The
broker reaches the tools of Claude Agent SDK, OpenAI Agents (agents used as
tools included), LangChain and LangGraph (subgraphs included), Pydantic AI
(toolsets and MCP servers included) and AutoGen agents, and any agent with a
`tools` list, including tools added after wrapping; a refused call returns
the refusal to the model, which carries on. `@agent.tool` guards tools in
your own code, and `brevet.wrap(..., grantor=...)` supplies grants for
controlled_act calls. In Claude Code, `brevet hook` applies the tiers and the
channel of the latest release, never unreleased edits to `agent.yaml`, as a
PreToolUse hook: it asks you before a controlled_act call, records your
grant through the PostToolUse hook, and refuses any call it cannot check,
including every call while the evidence chain does not replay or does not
match its anchors.

### An anchored evidence chain

After every governing step, Brevet writes the chain's head to anchors outside
the workspace, so a chain that is cut short, rewritten or replaced is
detected even though it replays cleanly:

```yaml
runtime_safety:
  evidence:
    anchors:
      - file:/Volumes/audit/brevet-anchors.jsonl   # another disk or a synced folder
      - chap:wsp_audit@https://chap.example.org     # a CHAP coordinator's audit log
```

`BREVET_ANCHORS` and `~/.config/brevet/anchors` add anchors from outside the
workspace, where an agent that can rewrite it cannot remove them. Each anchor
names the workspace by an identity kept in `~/.config/brevet/workspaces.json`,
so replacing the workspace, even under another agent name, is caught.
`brevet anchor` writes the head on demand, `brevet verify` checks it, and a
wrapped agent (which also replays the chain whenever it changed), the Claude
Code hook and `brevet_active` refuse a chain that no longer holds its
anchored heads. When you add an anchor to a workspace that already has
history, run `brevet anchor` once so the history is anchored from the start.
A head that could not be written waits in the workspace and goes with the
next one; `brevet verify` and `brevet_active` show how many governing steps
are waiting.

### Consent

`runtime_safety.dream.consent_scope: consented_sources_only` limits the
dream cycle to the overrides of the workspace owner and the identities listed
under `consented`; `all_recorded` learns from every override.
`brevet consent withdraw --participant human:rev@site --issued-by
human:qa@site` stops learning from a participant and recalls every capability
built on their overrides.

### The governed-adaptation profile

`brevet benchmark` scores the workspace's lineage on the four axes of
[BENCHMARK.md](BENCHMARK.md): improvement, regression discipline, lineage
completeness and recall compliance, with the candidates promoted, rejected
and held and the releases blocked at the gate beside them.

## The worked example

[examples/pump_vibration.py](examples/pump_vibration.py) runs the README's
example from the first override to the recall. Running it prints:

```text
1. Work and override: 4 overrides recorded from 5 drafts (1 draft accepted as it was).
2. Dream: 1 candidate capability, Evidence layer (no authority yet):
   In equipment_triage work involving 'vibration-during-cleaning', reviewers changed
   the agent's decision 4 times. Their reason: Vibration during cleaning is an early
   sign of seal wear. Proposed rule: when this situation applies, raise it explicitly
   and follow the reviewers' decision.
3. Dawn: rejected an approval from dream:nightly (machine identities cannot promote).
   Dawn: mission_group:quality_team promoted the rule and the 4 eval cases compiled with it to Advisory.
4. Evals: 0/4 passed before, 4/4 after. Conservative gate: pass.
5. Release: 0.2.0 signed; capabilities.lock lists 5 promoted capabilities and their approver.
6. Recall: capability recalled; releases flagged: 0.2.0.
7. Verify: evidence chain intact.
   Records are in <temporary folder>
```

## What is in the box

- **The harness as data.** `agent.yaml`, the manifest, holds the agent's
  instructions, tools, loop policy, memory bindings and safety settings in
  five layers. It is versioned and signed at every release.
- **One capability object for everything learned.** Prompt rules, skills,
  tool bindings, eval cases, escalation rules and memory bindings share one
  structure, one authority ladder and one recall mechanism.
- **Override-compiled evals.** `agent.evaluate()` replays the overrides as
  tests, so no separate labelling project is needed. Cases are sorted by
  content hash and dealt alternately into the held-in and held-out halves.
- **A bill of materials for every release.** `capabilities.lock` lists what
  the release knows, where each capability came from, the hash of its exact
  content and who approved it, and beside that the harness it runs with. The
  release signature covers the lock's digest.
- **Optional model assist.** `brevet dream --assist ollama` lets a local
  model word candidates more clearly. Its output is always a draft, every use
  is logged, and Brevet works fully without it.
- **CHAP mirroring.** Set `ledger: chap:<workspace>` in `agent.yaml`, install
  the extra with `pip install "brevet[chap]"`, and every evidence envelope is
  also mirrored to a CHAP coordinator running alongside, with its own database
  in the working directory. To use a coordinator on another machine, write
  `ledger: chap:<workspace>@<url>`; envelopes queue in an outbox while the
  connection is down. The local evidence chain remains the record of
  reference.

## Supported frameworks

<p align="center">
  <picture>
    <source media="(prefers-color-scheme: dark)" srcset="docs/assets/wrap-dark.svg">
    <img src="docs/assets/wrap-light.svg" alt="brevet.wrap(agent) places the agent, unchanged, inside four records: the signed manifest, the hash-linked evidence chain, the dawn gate, and capabilities.lock." width="860">
  </picture>
</p>

`brevet.wrap()` works out which framework built the agent from the agent
object itself. The framework keeps running the agent; Brevet keeps the
records.

| Framework | How to wrap it |
|---|---|
| LangGraph | `brevet.wrap(compiled_graph)` |
| Claude Agent SDK | `brevet.wrap(sdk_client)` |
| DeepAgents | `brevet.wrap(deep_agent)` |
| AutoGen (AgentChat) | `brevet.wrap(agent_or_team)` |
| LlamaIndex | `brevet.wrap(agent_or_engine)` |
| Pydantic AI | `brevet.wrap(pydantic_agent)` |
| Google Agent Development Kit | `brevet.wrap(runner)` |
| CrewAI | `brevet.wrap(crew)` |
| OpenAI Agents SDK | `brevet.wrap(agent)` |
| any Python function | `brevet.wrap(fn)` |

Another framework needs one adapter class, registered with
`brevet.register_adapter`:

```python
@brevet.register_adapter("myfw", prefixes=("myfw",))
class MyAdapter(brevet.BaseAdapter):
    def invoke(self, task, context):
        return self.target.do(task), [{"step": "do"}]
```

No framework is ever a required dependency: adapters check the shape of the
object they are given, and the tests use stand-ins rather than the real
frameworks. Outside the shadow channel, a wrapped agent runs only the latest
release on its evidence chain: the manifest must be that release, its
signature must verify against the key the chain recorded, and its
`capabilities.lock` must be the lock the release names. An edit made after a
release, a re-signed manifest or a restored older release stops the agent
instead of running unrecorded, and so does a renamed agent with no release
of its own. A running agent picks up a release made from another process,
such as `brevet release` or `brevet approve`, at its next run. Keep one agent
per folder: agents in the same folder share its `capabilities.lock`.

## The MCP server

`brevet mcp` serves the loop to any MCP client, such as Claude Desktop, Claude
Code or Cursor. Clients may start servers from any directory, so point
`BREVET_HOME` at a folder that holds `agent.yaml` and `.brevet/`:

```json
{"mcpServers": {"brevet": {
  "command": "uvx", "args": ["brevet", "mcp"],
  "env": {"BREVET_HOME": "/path/to/workspace"}}}}
```

| Tool | What it does |
|---|---|
| `brevet_record` | records a draft and the expert's final; a difference becomes an override |
| `brevet_chap_ingest` | imports CHAP review verdicts as overrides |
| `brevet_dream` | runs the dream cycle |
| `brevet_dawn_pending` | lists the candidates awaiting a dawn decision |
| `brevet_dawn_decide` | records one dawn decision under a human or mission-group identity |
| `brevet_release` | signs and records a release, bound to eval runs or with attested deltas, with the conservative gate for trial and production |
| `brevet_rollback` | returns the agent to an earlier release as a new signed release |
| `brevet_recall` | recalls a capability and flags the releases that shipped it |
| `brevet_active` | serves the governed rules of the latest release, after checking the chain and its anchors, the approvals, the lock, the signature, and each rule's content and conditions; lists recalled rules |
| `brevet_acknowledge` | confirms that the session stopped applying recalled rules |
| `brevet_status` | reports the version, capabilities by layer, the dawn queue, chain health, signing, the harness, anchors and open recalls |
| `brevet_harness` | shows the release's harness bill of materials and which named files changed since |
| `brevet_anchor` | writes the chain's head to its anchors outside the workspace |
| `brevet_verify` | replays the evidence chain, checks it against its anchors and checks every approval signature |

Read-only tools carry the MCP read-only hint, and the dawn, release,
rollback and recall tools carry the destructive hint, so clients can ask
before running them. When the workspace requires signed approvals, those
tools return a request and the command that signs it, and nothing changes
until a person signs.
Sessions record corrections only when asked, unless the workspace owner turns
on automatic capture with `BREVET_AUTO_CAPTURE=1` or
`runtime_safety.evidence.auto_capture: true` in the manifest.

## Commands

| Command | What it does |
|---|---|
| `brevet demo` | runs the whole loop once on synthetic data, offline |
| `brevet playground` | steps through the loop in a browser |
| `brevet init` | writes a starter `agent.yaml` and `.brevet/` |
| `brevet dream` | mines recurring overrides into candidates |
| `brevet dawn` | lists the dawn queue, or records one decision with `--decide` |
| `brevet evals` | lists the recorded eval runs, to bind a release to them |
| `brevet release` | passes the conservative gate, then signs and records a release |
| `brevet rollback` | returns the agent to an earlier release as a new signed release |
| `brevet recall` | recalls a capability and flags every release that shipped it |
| `brevet recalls` | shows each recall and the agents still to acknowledge it |
| `brevet verify` | replays the evidence chain and checks it against its anchors |
| `brevet anchor` | writes the chain's head to its anchors outside the workspace |
| `brevet status` | shows the version, capabilities by layer and chain health |
| `brevet harness` | shows the harness bill of materials and any drift since the release |
| `brevet benchmark` | scores the lineage on the four governed-adaptation axes |
| `brevet consent withdraw` | stops learning from a participant and recalls what was built on their overrides |
| `brevet hook` | checks a Claude Code tool call against the tool tiers (PreToolUse hook) |
| `brevet chap-ingest` | imports CHAP review verdicts as overrides |
| `brevet mcp` | serves the loop to an MCP client |
| `brevet approver` | registers approvers, revokes them and sets mission-group thresholds |
| `brevet approve` | lists the requests waiting for signatures, or signs them |

## Governing what Claude itself learns

Claude Desktop and Cowork already learn between sessions through memory, saved
skills and standing instructions. [examples/claude-cowork](examples/claude-cowork)
puts that learning under the governed evolution loop in a few minutes. Your
corrections become overrides, you promote candidates at dawn, and the governed
rules Claude receives come only from a signed release. Naming Claude's skill
folders, `CLAUDE.md` and connector settings under `harness_files` puts them
under change control too: a change shows up at the start of the next
session, until it is released. In Claude Code, `brevet hook` applies the
released tool tiers to Claude's tool calls.

## Where Brevet fits

<p align="center">
  <picture>
    <source media="(prefers-color-scheme: dark)" srcset="docs/assets/suite-dark.svg">
    <img src="docs/assets/suite-light.svg" alt="CHAP records what happened, Metis captures what experts know, and Brevet governs how the agent changes" width="860">
  </picture>
</p>

| Project | Question it answers | What it owns |
|---|---|---|
| [CHAP](https://github.com/BrightbeamAI/chap) | What happened between people and agents? | the record of tasks, drafts, reviews and decisions |
| [Metis](https://github.com/BrightbeamAI/metis) | What do our experts know that is not written down? | experts' know-how, captured as governed memory |
| **Brevet** | How does the agent change, and on whose approval? | approvals, capability state and recalls |

The three are designed to work together, and each owns its own records.
Brevet's evidence envelopes follow CHAP's envelope model, so Brevet writes
CHAP-compatible evidence and can mirror it to a live CHAP coordinator;
`brevet chap-ingest` imports CHAP review decisions as overrides. Brevet's
capability object extends the tuple Metis uses for tacit fragments, adding a
`kind` field. For `memory_fragment` capabilities, Metis stays the system of
record; Brevet records only the binding and its approval.

## What Brevet is not

Brevet is not an agent framework. It does not let an agent improve itself
without oversight: candidates have no authority until a human or mission
group promotes them, and approvals under machine identities are refused. It
is not a monitoring tool either: it learns only from the overrides its
consent scope allows, and Evidence-layer material never influences the
agent.

## How the repository is organised

```
brevet/
├── brevet/                   the Python package
│   ├── shell.py              brevet.wrap() and the agent object it returns
│   ├── adapters.py           framework adapters and the adapter registry
│   ├── evidence.py           harvests overrides from drafts and finals
│   ├── delta.py              the dream cycle: mines overrides into candidates
│   ├── lifecycle.py          the dawn gate, releases and recall
│   ├── evals.py              override-compiled evals; the conservative gate
│   ├── runner.py             runs the evals before and after a change
│   ├── ledger.py             the hash-linked evidence chain
│   ├── canonical.py          content hashing and Ed25519 signing
│   ├── identity.py           which identities may decide
│   ├── approvals.py          approver keys, identity checks and signed approvals
│   ├── harness.py            the harness bill of materials and the drift check
│   ├── serving.py            the governed rules an agent may follow, checked
│   ├── broker.py             the tool broker and the Claude Code hook
│   ├── releases.py           publishing, the release archive and rollback
│   ├── recalls.py            recall acknowledgements
│   ├── anchor.py             anchoring the evidence chain outside the workspace
│   ├── consent.py            consent scope and withdrawal
│   ├── benchmark.py          the governed-adaptation profile
│   ├── workdir.py            workspace location, permissions and file locks
│   ├── models.py             data models matching the schemas
│   ├── assist.py             optional local model assist
│   ├── chap_bridge.py        mirrors envelopes to a CHAP coordinator
│   ├── chap_evidence.py      imports CHAP review decisions as overrides
│   ├── mcp_server.py         the loop as MCP tools
│   ├── playground.py         the loop, stage by stage, in a browser
│   ├── demo.py               the end-to-end demonstration
│   └── cli.py                the `brevet` command
├── schemas/                  the five JSON Schemas that define the records
├── examples/
│   ├── pump_vibration.py     the worked example from the README
│   └── claude-cowork/        governing what Claude itself learns
├── docs/
│   ├── demo.html             the interactive tour
│   └── assets/               the diagrams, in light and dark versions
├── scripts/
│   ├── make_diagrams.py      regenerates the diagrams
│   └── make_pypi_readme.py   writes README_PYPI.md for PyPI
├── tests/                    the test suite; runs offline
├── server.json               the MCP Registry entry
└── README.md · ABOUT.md · GLOSSARY.md · SPEC.md · BENCHMARK.md
```

## What Brevet stores, and where

`brevet.wrap()` keeps everything in a working directory, `.brevet/` by default
(change it with `workdir=`). Brevet creates it readable only by you, with a
`.gitignore` that keeps it out of version control:

```
.brevet/
├── agent.yaml                the manifest, if none was supplied
├── capabilities.lock         the latest release's lock, beside the manifest
├── ledger.jsonl              the evidence chain: one envelope per line
├── capabilities.jsonl        the capability store; latest line per id wins
├── keys/brevet_ed25519.pem   the signing key, created at the first release
├── approvals.jsonl           decisions waiting for approver signatures
├── releases/<version>.json   what each release shipped: manifest, lock, file digests
├── objects/                  the content of released harness files, stored by digest
├── anchor_outbox.jsonl       chain heads waiting to reach an anchor
├── chap_cursor.json          how far each CHAP source has been imported
├── chap.db                   an embedded CHAP coordinator's store, if used
├── chap_outbox.jsonl         envelopes waiting to be mirrored to CHAP
└── *.lock                    short-lived locks that let processes share files
```

When a manifest path is given, `agent.yaml` and `capabilities.lock` live
beside it instead. Never commit `.brevet/`: it holds a private key and the
evidence of real work. Approver keys never live here; each person keeps
theirs in `~/.config/brevet/approvers/`.

## Evidence envelopes

Every envelope is appended, never edited, and hash-linked to the one before
it: `chain_hash = sha256(encode(envelope) ‖ prev_hash)`. Each names the
runtime that wrote it (`"runtime": "brevet/0.4.0"`), so replay applies to
every approval record the rules it was written under. Appends take a file
lock, so several processes can share one chain.

| Envelope | Appended when |
|---|---|
| `brevet.task` | the agent is given a task |
| `brevet.artefact` | the agent produces a draft |
| `brevet.override` | an expert's correction is recorded |
| `brevet.candidate` | the dream cycle proposes a candidate |
| `brevet.model_assist` | a local model helps word a candidate |
| `brevet.promotion` | a decision is made at the dawn gate |
| `brevet.eval_run` | the evals run |
| `brevet.release` | a new version is released |
| `brevet.recall` | a capability is recalled |
| `brevet.approver` | an approver is registered or revoked, or a group threshold changes |
| `brevet.drift` | the harness differs from its release, recorded once for each new state |
| `brevet.recall_ack` | a running agent or session confirms it stopped using a recalled capability |
| `brevet.tool_call` | the tool broker refuses a call, or allows an act or controlled_act call |
| `brevet.consent` | a participant withdraws consent |
| `brevet.gate` | the conservative gate blocks a release |

## The benchmark

Research on self-improving agents mostly measures one thing: whether the
agent got better. Teams running agents in production need four measures,
and [BENCHMARK.md](BENCHMARK.md) specifies a governed-adaptation benchmark
that reports all four side by side: improvement, regression discipline,
lineage completeness and recall compliance. `brevet benchmark` scores any
workspace's lineage on the four axes; BENCHMARK.md sets out the datasets and
the systems to compare.

## Design principles

- **Authority is granted, never grabbed.** Every increase in a
  capability's authority is a recorded decision by a named human or mission
  group, signed by them once the workspace registers approvers.
- **The running agent does not change itself.** It runs one released
  version. Learning happens between versions, where it can be reviewed.
- **Overrides are evidence, not truth.** Experts' corrections are the best
  available record of judgement, but they can be mistaken, so candidates are
  reviewed at dawn before they gain authority.
- **No network needed.** The tests, the demonstration and the whole loop run
  offline. Model assist and CHAP mirroring are optional and fail safely.
- **Rejection is not deletion.** Rejected and recalled capabilities stay on
  the evidence chain with their lineage intact.
- **Prove it from the chain.** Promotions, releases and recalls are all
  envelopes on the evidence chain, so anyone holding it can replay and check
  them.
