# Glossary

The terms below are the ones the paper, the code, the schemas in `schemas/`
and [SPEC.md](SPEC.md) all use. Each entry gives the plain meaning first and
the precise detail second.

## The loop

| Term | Plain meaning | Detail |
|---|---|---|
| **governed evolution loop** | The cycle through which an agent's learning is proposed, reviewed, tested, released and, if necessary, taken back. | Seven stages: work, override, dream, dawn, evals, release and recall. Every stage appends an envelope to the evidence chain. |
| **circadian contract** | The rule that the agent works without changing itself, and learning happens separately, between versions. | Awake, the agent executes one signed harness version. While it sleeps, the dream cycle mines evidence with no authority. At dawn, humans decide what the next version contains. |
| **work** | The agent doing its job. | `agent.run()`. Each task and draft is recorded as `brevet.task` and `brevet.artefact` envelopes. |
| **draft** and **final** | The draft is what the agent produced. The final is the version the expert actually used, after any correction. | If the final matches the draft, the draft was accepted as it was and no override is recorded. |
| **override** | An expert's correction of a draft, together with the reason. | Recorded by `agent.record_final()` as the line-by-line difference between draft and final, plus the expert's rationale, optional tags and identity. If the workflow already keeps the draft and the final, nobody fills in a separate form. |
| **refining** and **substituting** override | A refining override keeps the agent's decision and changes how it is expressed. A substituting override reaches a different decision, for example *minor* becomes *major*. | Stored as `intent_preserved` (true for refining, false for substituting), a field taken from CHAP. The dream cycle treats them as soft and hard signals, grouped as `soft_fail` and `hard_fail`. Brevet infers the kind from decision-bearing words such as severity labels; it cannot reliably read intent from free prose. |
| **dream** (dream cycle) | The offline step that looks for overrides that keep recurring and proposes a candidate capability for each. | `agent.dream()` or `brevet dream`. It examines the **difference signal** Δ = enacted ⊖ specified: a structured comparison of what was actually done with what the signed harness specified, not an arithmetic subtraction. Overrides are grouped by **failure signature**: task family, override kind and first tag. A group needs at least three overrides before a candidate is proposed. The cycle also compiles overrides into eval cases. It adds only what is new: a group already proposed is not proposed again, and a candidate built from more overrides supersedes a pending one. Nothing it produces has authority. |
| **candidate** | A capability the dream cycle has proposed and nobody has approved. | Every candidate enters the Evidence layer of the authority ladder. |
| **dawn** (dawn gate) | The step where a named human or mission group reviews each candidate and decides. | `agent.dawn()` lists the queue; `agent.dawn(decide=(id, outcome), approver=...)` records one decision. Outcomes: `promote`, `hold` (wait for more evidence), `reject`, and `re_elicit` (go back to the experts whose overrides produced it). Each decision is a `brevet.promotion` envelope naming the approver. |
| **evals** | The overrides, replayed as tests, to check a change before it ships. | `agent.evaluate()`. Each substituting override that carries the expert's final becomes an eval case whose expected answer is that final. Cases are sorted by content hash and dealt alternately into held-in and held-out halves. Eval cases are capabilities themselves, so they can be recalled if a label proves wrong. |
| **conservative gate** | The release rule: neither half of the evals may get worse, and at least one must improve. | `Δin ≥ 0 AND Δout ≥ 0 AND max > 0`, following Self-Harness, checked on the deltas passed to `release()`. It stops a change that improves one half while worsening the other. It is not a no-regression test: gains and losses can still cancel within one half. Enforced for `trial` and `production` releases. |
| **release** | A new version of the agent, signed, like a software release. | `agent.release()`. It resolves promoted capabilities into `capabilities.lock`, signs the manifest with Ed25519 (the signature covers the lock's digest) and records a `brevet.release` envelope with the rollback target and the signing key. |
| **channel** | How widely a release is used. | `shadow` (observed, not acted on), `trial` (limited exposure) and `production`. |
| **recall** | Taking a capability back after it proves wrong, like a product recall. | `agent.recall()`. It withdraws the capability, flags every release that shipped it and records the reason and the requested action (`quarantine`, `rollback` or `re_review`) in a recall notice. Later releases leave out the recalled capability and any capability with identical content. Confirming that running agents have stopped using it needs checks where they run. |

## Capabilities and authority

| Term | Plain meaning | Detail |
|---|---|---|
| **capability** (capability object) | Anything the agent learns: a rule, a skill, a tool binding, an eval case and so on. | One structure for all of them: content, provenance, conditions, confidence, authority and validation state, plus a `kind`. One lifecycle and one recall mechanism for every kind. |
| **kind** | What sort of capability it is. | `prompt_rule`, `loop_policy`, `tool_binding`, `skill`, `eval_case`, `escalation_rule` or `memory_fragment` (a binding to a memory item that Metis keeps). |
| **authority ladder** (authority layer) | How much a capability may influence the agent. | **Evidence**: no operational authority; humans can inspect it and evals can test it. **Advisory**: may inform drafts that a human still checks. **Controlled**: may shape actions with no human between draft and effect, and needs promotion by a mission group. Authority rises only through a recorded decision by a named human or mission group; how a layer limits use is applied where the agent runs. |
| **validation state** | Where a capability is in its life. | `captured`, `promoted_to_advisory`, `promoted_to_controlled`, `held`, `rejected`, `re_elicit`, `withdrawn`, `superseded` or `expired`. Rejection is not deletion: rejected and recalled capabilities stay on record. |
| **conditions** | Where a capability applies. | Task family, domain, risk class, exclusions and validity window. The intended policy withholds a capability whose conditions do not match, and an exclusion always vetoes. |
| **endogenous** and **exogenous** | Endogenous: inferred by the system from the agent's own records. Exogenous: contributed or confirmed by a human. | Endogenous candidates enter at Evidence as hypotheses, are held to a higher evidence bar, and cannot be promoted under a machine identity. Eval cases are exogenous, because their labels are human finals. |

## People and approval

| Term | Plain meaning | Detail |
|---|---|---|
| **mission group** | The accountable review board for the work: a named panel of humans, never a single expert. | It weighs candidates, resolves conflicts, promotes to Controlled, signs releases and issues recalls. Identity form: `mission_group:<name>`, for example `mission_group:quality_team`. |
| **approver identity** | The human or group recorded as making a decision. | `human:<who>` or `mission_group:<which>`. Every other namespace is refused, including `agent:`, `model:` and `dream:`, for dawn decisions, releases and recalls alike. Without registered approvers, Brevet records the identity it is given. With them, every decision must carry signatures (see signed approval). |
| **signed approval** | A decision signed by the people it needs, with keys an agent does not hold. | Once a workspace registers approvers, every dawn decision, release and recall must be signed. A request records the exact decision (the capability's content hash, or the lock's digest); `brevet approve` signs it with a passphrase-protected Ed25519 key kept outside the workspace; it is applied once the human, or the mission group's threshold of members, has signed, and only once. |
| **approver register** | The list of people whose signatures a workspace accepts, with their groups and group thresholds. | Kept on the evidence chain as `brevet.approver` envelopes. The first approver registers themselves; every later change needs an existing approver's signature, and a new key signs its own registration. |
| **consent scope** | Whose overrides the dream cycle may learn from. | Declared in the manifest; the dream cycle does not yet filter overrides by it, so a deployment applies it at capture. Consent withdrawal is honoured through recall, never by silent deletion. |

## Records

| Term | Plain meaning | Detail |
|---|---|---|
| **evidence chain** (ledger) | The record of every step, written so that edits to the history can be detected. | An append-only file, `.brevet/ledger.jsonl`. Each envelope's hash covers the previous one: `chain_hash = sha256(encode(envelope) ‖ prev_hash)`. `brevet verify` replays it and detects any edit that breaks the chain. Detecting a complete rewrite, or a chain cut short at the end, needs the latest chain hash held elsewhere, for example by a CHAP coordinator. |
| **evidence envelope** | One record on the evidence chain. | Kinds: `brevet.task`, `brevet.artefact`, `brevet.override`, `brevet.candidate`, `brevet.promotion`, `brevet.eval_run`, `brevet.release`, `brevet.recall`, `brevet.approver` and `brevet.model_assist`. Envelopes follow CHAP's envelope model. |
| **content hash** | A short code computed from a capability's exact content. Any change to the content changes the code. | SHA-256 of the content. Lockfiles record it, so an auditor can tell when content changed under the same identifier. |
| **capabilities.lock** | The capability bill of materials for one release: what the agent has learned and who approved each piece. | Each entry records the capability's identifier, kind, content hash, authority layer, a digest of its conditions, its approver and references to its evidence. The release signature covers the lock's digest. It plays the role a software bill of materials plays for code. |
| **harness** and **manifest** | The harness is everything around the model that makes it an agent: instructions, tools, loop policy, memory bindings and safety settings. The manifest is the harness written down as one versioned file. | `agent.yaml`, in five layers: identity and policy, prompt architecture, cognitive core, bindings, and runtime safety. Signed at each release. |
| **model assist** | Optional help from a local language model in wording a candidate. | Off by default. Each use is logged as `brevet.model_assist` and marked for human review. The wording gains no authority from its fluency, and Brevet falls back to its own templates when no model is installed. |
