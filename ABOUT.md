# About Brevet

How Brevet is built, how the repository is organised and what each part
does. For a first look, start with the [README](README.md). For definitions
of every term, see the [GLOSSARY](GLOSSARY.md). For the rules any
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

The three are designed to work together. Brevet's evidence envelopes
follow CHAP's envelope model, and Brevet can mirror them to a live CHAP
coordinator or import CHAP review decisions as overrides. Brevet's
capability object extends the tuple Metis uses for tacit fragments, adding a
`kind` field. For `memory_fragment` capabilities, Metis stays the system of
record; Brevet records only the binding and its approval.

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
  tests. A release must pass the conservative gate: neither half may get
  worse, and at least one must improve.
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

Another framework needs one adapter class registered with
`brevet.register_adapter` (the README shows how). No framework is ever a
required dependency: adapters check the shape of the object they are given,
and the tests use stand-ins rather than the real frameworks.

## What Brevet is not

Brevet is not an agent framework. It does not let an agent improve itself
without oversight: candidates have no authority until a human promotes them,
and an endogenous candidate can never be promoted by the process that
proposed it. It is not a monitoring tool either. The manifest declares whose
overrides the dream cycle may learn from, and Evidence-layer material never
influences the agent.

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
  capability's authority is a recorded human decision.
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
