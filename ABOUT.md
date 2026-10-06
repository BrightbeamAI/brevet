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

## The governed evolution loop

The loop has seven stages. Each one appends its envelopes to the evidence
chain.

| Stage | What happens | Who acts | What is recorded |
|---|---|---|---|
| **Work** `run()` | The agent drafts under one signed harness. | the agent | `brevet.task`, `brevet.artefact` |
| **Override** `record_final()` | An expert corrects the draft; the difference and the reason become an override. | an expert | `brevet.override` |
| **Dream** `dream()` | Offline, recurring overrides become candidate capabilities and eval cases, with no authority. | Brevet | `brevet.candidate` |
| **Dawn** `dawn()` | Each candidate is promoted, held, rejected or sent back for re-elicitation. | a named human or mission group | `brevet.promotion` |
| **Evals** `evaluate()` | Overrides replay as tests. The conservative gate needs neither split to get worse and at least one to improve. | Brevet | `brevet.eval_run` |
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
result. **Controlled**, where it may drive actions directly, needs review by
the mission group. Rejection is not deletion: rejected candidates stay on the
evidence chain, so "was this ever proposed, and why did we say no?" always has
an answer.

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
   Dawn: promoted to Advisory by mission_group:quality_team.
4. Evals: 0/4 passed before, 4/4 after. Conservative gate: pass.
5. Release: 0.2.0 signed; capabilities.lock lists 1 promoted capability and its approver.
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
  structure, one authority ladder (Evidence, Advisory, Controlled) and one
  recall mechanism.
- **The dream cycle.** `agent.dream()` groups overrides that keep recurring
  and drafts a candidate capability from each group. It needs no benchmark,
  because the overrides supply the examples.
- **Override-compiled evals.** `agent.evaluate()` replays the overrides as
  tests, so no separate labelling project is needed. A release must pass the
  conservative gate: neither half may get worse, and at least one must
  improve.
- **A capability bill of materials for every release.** `capabilities.lock`
  lists what the release knows, where each capability came from and who
  approved it.
- **Recall.** `agent.recall()` withdraws a capability, flags every release
  that shipped it and records the reason.
- **Optional model assist.** `brevet dream --assist ollama` lets a local
  model word candidates more clearly. Its output is always a draft, every use
  is logged, and Brevet works fully without it.
- **CHAP mirroring.** Set `ledger: chap:<workspace>` in `agent.yaml`, install
  the extra with `pip install "brevet[chap] @ git+https://github.com/BrightbeamAI/brevet"`,
  and every evidence envelope is also mirrored to a CHAP coordinator running
  alongside, with its own database in the working directory. To use a
  coordinator on another machine, write `ledger: chap:<workspace>@<url>`;
  envelopes queue in an outbox while the connection is down. The local
  evidence chain remains the record of reference.

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
frameworks. The whole loop is also available to any MCP client, such as
Claude Desktop or Cursor, through `brevet mcp`.

## Governing what Claude itself learns

Claude Desktop and Cowork already learn between sessions through memory, saved
skills and standing instructions. [examples/claude-cowork](examples/claude-cowork)
puts that learning under the governed evolution loop in a few minutes. Your
corrections become overrides, you promote candidates at dawn, and Claude follows
only capabilities from a signed release.

## What Brevet is not

Brevet is not an agent framework. It does not let an agent improve itself
without oversight: candidates have no authority until a human promotes them,
and an endogenous candidate can never be promoted by the process that
proposed it. It is not a monitoring tool either. The manifest declares whose
overrides the dream cycle may learn from, and Evidence-layer material never
influences the agent.

## Project status

Version 0.1 implements the whole loop and keeps every record. Three protections
are left to the system you deploy it in, and the [paper](README.md#citation)
sets them out in full:

- **Authenticated approvers.** Version 0.1 records the approver identity and
  rejects machine namespaces. Proving that the named human really decided needs
  authenticated identities, which CHAP's participant keys can supply.
- **Complete recall.** Version 0.1 excludes the recalled capability from future
  releases and flags the affected ones. Blocking identical content from
  returning under a new identifier, and confirming that running agents have
  stopped using it, need checks where the agent runs.
- **An anchored evidence chain.** Replay detects edits that break the chain.
  Detecting a wholesale rewrite needs the latest chain hash held outside the
  machine, which a CHAP coordinator can hold.

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
│   ├── models.py             data models matching the schemas
│   ├── assist.py             optional local model assist
│   ├── chap_bridge.py        mirrors envelopes to a CHAP coordinator
│   ├── chap_evidence.py      imports CHAP review decisions as overrides
│   ├── mcp_server.py         the loop as MCP tools (optional extra: [mcp])
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
│   └── make_diagrams.py      regenerates the diagrams
├── tests/                    the test suite; runs offline
└── README.md · ABOUT.md · GLOSSARY.md · SPEC.md · BENCHMARK.md
```

## What Brevet stores, and where

`brevet.wrap()` keeps everything in a working directory, `.brevet/` by default
(change it with `workdir=`):

```
.brevet/
├── agent.yaml                the manifest, if none was supplied
├── ledger.jsonl              the evidence chain: one envelope per line
├── capabilities.jsonl        the capability store; latest line per id wins
├── keys/brevet_ed25519.pem   the signing key for this folder
└── chap_outbox.jsonl         envelopes waiting to be mirrored to CHAP
```

Never commit `.brevet/` to version control: it holds a private key and the
evidence of real work. The shipped `.gitignore` already excludes it.

## Evidence envelopes

Every envelope is appended, never edited, and hash-linked to the one before
it: `chain_hash = sha256(encode(envelope) ‖ prev_hash)`.

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

## The benchmark

Research on self-improving agents mostly measures one thing: whether the
agent got better. Teams running agents in production need four measures,
and [BENCHMARK.md](BENCHMARK.md) proposes a governed-adaptation benchmark
that reports all four side by side: improvement, regression discipline,
lineage completeness and recall compliance. No implementation of the
benchmark exists yet.

## Design principles

- **Authority is granted, never grabbed.** Every increase in a
  capability's authority is a recorded human decision, and approvals made
  under an `agent:`, `model:` or `dream:` identity are rejected.
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
