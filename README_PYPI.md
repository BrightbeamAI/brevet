<!-- Generated from README.md by scripts/make_pypi_readme.py for PyPI. Edit README.md, then run the script. -->
<p align="center">
  <a href="https://github.com/BrightbeamAI">
      <img src="https://raw.githubusercontent.com/BrightbeamAI/brevet/v0.4.0/docs/assets/brightbeam-logo-light.svg" alt="Brightbeam" width="220">
  </a>
</p>

<h1 align="center">Brevet: Change Control for What AI Agents Learn</h1>

<p align="center"><b>A governed evolution loop that makes agent learning promotable,
auditable, and revocable.</b></p>

<p align="center">
  <a href="https://pypi.org/project/brevet/"><img src="https://img.shields.io/pypi/v/brevet.svg" alt="PyPI version"></a>
  <a href="https://www.python.org/downloads/"><img src="https://img.shields.io/badge/python-3.10%2B-blue.svg" alt="Python 3.10+"></a>
  <a href="https://github.com/BrightbeamAI/brevet/blob/v0.4.0/LICENSE"><img src="https://img.shields.io/badge/license-Apache--2.0-green.svg" alt="License: Apache-2.0"></a>
  <a href="https://github.com/BrightbeamAI/chap"><img src="https://img.shields.io/badge/CHAP-compatible-EA4700.svg" alt="CHAP-compatible"></a>
</p>

<!-- mcp-name: io.github.BrightbeamAI/brevet -->

---

AI agents now change their own behaviour while they work. They save memories,
write themselves new skills and edit their own instructions. Many of these
changes help. Yet none of them passes through the steps an organisation expects
when a person changes how work is done. Nobody writes the change down, nobody
approves it, and when it turns out to be wrong there is no earlier version to
go back to.

Brevet adds those steps. It is a local-first Python runtime that wraps the
agent you already have and runs its learning as a **governed evolution loop**.
When an expert corrects the agent's draft, Brevet records the correction and its
reason as an **override**. Offline, in the **dream** cycle, overrides that keep
recurring become **candidate** capabilities: proposed rules and other learned
behaviour, with no authority. At the **dawn** gate, a named human or **mission
group** (the accountable review board) decides which candidates to promote. The
overrides then replay as **evals**, and the **conservative gate** stops a
release that makes either half of them worse. Promoted capabilities ship in a
signed **release**, listed in `capabilities.lock`, and a capability that proves
wrong can be **recalled**, with every release that shipped it flagged. Every
step is recorded on a hash-linked **evidence chain**.

> Agents propose deltas; evidence tests them; humans promote them;
> the runtime only ever executes signed versions.

<p align="center">
    <img src="https://raw.githubusercontent.com/BrightbeamAI/brevet/v0.4.0/docs/assets/how-it-works-light.svg" alt="The governed evolution loop: the agent works under one signed harness; an expert's correction is recorded as an override; the dream cycle turns recurring overrides into candidate capabilities with no authority; at the dawn gate a named human or mission group promotes, holds or rejects each candidate; evals replay the overrides and the conservative gate must pass; promoted capabilities ship in a signed release listed in capabilities.lock. A capability that proves wrong is recalled and every release that shipped it is flagged." width="900">
</p>

## Three questions Brevet answers

| Question | How Brevet answers it |
|---|---|
| **What has the agent learned?** | Every release carries `capabilities.lock`, its bill of materials: each learned capability, where it came from and the hash of its exact content, plus the harness the release runs with (prompts, skills, tools, settings and libraries). |
| **Who approved it?** | Each capability records the human or mission group that promoted it at the dawn gate, with the overrides that justified it, signed by their registered key once the workspace requires it. |
| **How do we take it back?** | Recall it, or roll back to an earlier release. Brevet flags every release that shipped it, withholds it from running agents and records each agent's acknowledgement. |

## A concrete example

A quality reviewer at a pharmaceutical plant checks an agent's severity rating
for each equipment problem. The agent rates pump vibration during cleaning as
*minor*. She overrides it to *major* every time, because that vibration is an
early sign of seal wear. After four overrides, the dream cycle proposes a
candidate rule. At dawn her mission group promotes it, and release 0.2.0 ships
with the rule in its `capabilities.lock`. Months later, engineers trace the
vibration to a faulty sensor, so the mission group recalls the rule and Brevet
flags release 0.2.0.

<p align="center">
    <img src="https://raw.githubusercontent.com/BrightbeamAI/brevet/v0.4.0/docs/assets/example-light.svg" alt="The example as a timeline: over three weeks the reviewer records four overrides; that night the dream cycle proposes a candidate rule at the Evidence layer; next morning at dawn the mission group promotes it and release 0.2.0 ships signed; months later the rule is recalled and release 0.2.0 is flagged." width="860">
</p>

<details>
<summary><b>The same story in code</b></summary>

The agent here is a plain Python function; with a real framework you pass your
agent object instead.

```python
import brevet

agent = brevet.wrap(triage_agent)          # wrap the agent you already have

# Work and override: the agent drafts; the reviewer corrects the draft and says why.
result = agent.run("Pump P-301: vibration high during cleaning",
                   task_family="equipment_triage")
agent.record_final(result.task_id, "severity: major",
                   participant="human:qa.reviewer@example.com",
                   rationale="Vibration during cleaning is an early sign of seal wear.",
                   tags=["vibration-during-cleaning"])

# Dream: once the same override keeps recurring, it becomes a candidate.
agent.dream()
candidates = agent.dawn()                   # the dawn queue
rule = next(c for c in candidates if c.kind == "prompt_rule")

# Dawn: a named mission group promotes the rule and the eval cases compiled
# with it. A dream:* approver is rejected.
for cap in candidates:
    agent.dawn(decide=(cap.capability_id, "promote"),
               approver="mission_group:quality_team")

# Evals: replay the overrides as tests, on the current release and on the next.
before = agent.evaluate(baseline=True)
#    ...the agent now receives the promoted rule with each task...
after = agent.evaluate()

# Release: bound to those two runs, and refused unless the conservative gate passes.
agent.release(to_version="0.2.0", channel="trial",
              approver="mission_group:quality_team", evals=(before, after))

# Recall: the rule proves wrong. Then verify the whole evidence chain.
agent.recall(rule.capability_id, reason="The vibration came from a faulty sensor.",
             issued_by="mission_group:quality_team")
agent.verify()
```

The full script is [examples/pump_vibration.py](https://github.com/BrightbeamAI/brevet/blob/v0.4.0/examples/pump_vibration.py), and
[ABOUT.md](https://github.com/BrightbeamAI/brevet/blob/v0.4.0/ABOUT.md#the-worked-example) shows what it prints.

</details>

## Quickstart

```console
pip install brevet
brevet demo                  # the whole loop as one command
brevet playground            # step through the loop in your browser
```

Everything runs on your own machine, with no model or network connection. To
run the example above, install Brevet, clone the repository and run
`python examples/pump_vibration.py`. For a guided, clickable tour, open
`docs/demo.html` from your clone in a browser.

## Works with the agent you already have

`brevet.wrap()` recognises agents built with LangGraph, the Claude Agent SDK,
DeepAgents, AutoGen, LlamaIndex, Pydantic AI, the Google Agent Development Kit,
CrewAI and the OpenAI Agents SDK, and it accepts any Python function. Your
framework keeps running the agent; Brevet serves it its governed rules with
each task and, wherever the framework exposes its tools, checks each tool call
against the tiers its release declares. `uvx brevet mcp` offers capture,
review, release and recall to any MCP client
([ABOUT.md](https://github.com/BrightbeamAI/brevet/blob/v0.4.0/ABOUT.md#the-mcp-server) shows the setup), and
[examples/claude-cowork](https://github.com/BrightbeamAI/brevet/blob/v0.4.0/examples/claude-cowork) uses it to govern what Claude
itself learns.

## Built for production

Every decision can require the signatures of approvers whose keys are checked
against your allowed-signers file or GitHub. Releases are bound to the eval
runs behind their numbers, and any release can be rolled back. A recalled rule
is withheld from running agents, which confirm it on the record. A tool broker
checks the agent's tool calls against the tiers the manifest grants, and the
evidence chain is anchored outside the workspace, so even a rewritten chain is
caught. ABOUT.md shows how to switch each one on: see
[Signed approvals](https://github.com/BrightbeamAI/brevet/blob/v0.4.0/ABOUT.md#signed-approvals) and
[Running in production](https://github.com/BrightbeamAI/brevet/blob/v0.4.0/ABOUT.md#running-in-production).

## Learn more

- **[ABOUT.md](https://github.com/BrightbeamAI/brevet/blob/v0.4.0/ABOUT.md)**: the seven stages, the authority ladder, supported
  frameworks, the MCP server and commands, how Brevet fits with CHAP and Metis,
  and how the repository is organised.
- **[GLOSSARY.md](https://github.com/BrightbeamAI/brevet/blob/v0.4.0/GLOSSARY.md)**: every term, with its plain meaning first.
- **[SPEC.md](https://github.com/BrightbeamAI/brevet/blob/v0.4.0/SPEC.md)**: the rules any implementation must follow.
- **[BENCHMARK.md](https://github.com/BrightbeamAI/brevet/blob/v0.4.0/BENCHMARK.md)**: the governed-adaptation benchmark;
  `brevet benchmark` scores a workspace on its four axes.

## Citation

If you use Brevet in research, please cite it as [CITATION.cff](https://github.com/BrightbeamAI/brevet/blob/v0.4.0/CITATION.cff)
describes. The accompanying paper is *Brevet: Change Control for What
Self-Evolving AI Agents Learn* (Shahid, Suttie and Black, 2026).

## License

Apache-2.0. See [LICENSE](https://github.com/BrightbeamAI/brevet/blob/v0.4.0/LICENSE). Brevet is a
[Brightbeam](https://github.com/BrightbeamAI) project.
