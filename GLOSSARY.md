# Glossary

Brevet's vocabulary comes from Brightbeam's research on governed agent
adaptation (the CHAP protocol, the Metis governed-memory model, and the
tacit-knowledge work behind them). Every term below is used precisely in the
code, the schemas, and [SPEC.md](SPEC.md).

## The loop

| Term | Meaning |
|---|---|
| **harness** | Everything around the model that makes it an agent: prompts, tool bindings, loop policy, memory bindings, safety rails. In Brevet the harness is data (`agent.yaml`), versioned and signed, never an invisible pile of glue code. |
| **waking / sleeping** | The circadian contract. Waking: the agent executes exactly one signed harness version and cannot modify itself. Sleeping: adaptation runs offline, on evidence, with zero authority. |
| **delta (Δ = enacted ⊖ specified)** | The difference signal between what actually happened (traces and human finals: the *enacted*) and what the signed harness and procedures specified. The ⊖ operator is a structured comparison of episodes matched on task family and conditions, not arithmetic subtraction. A gap may be genuine adaptation, or error, drift, or contamination; only recurrence across cases plus later validation can tell these apart. |
| **dream cycle** | The offline mining run (`agent.dream()` / `brevet dream`). Computes the delta, clusters recurring divergences by failure signature, and emits candidate capabilities plus compiled eval cases. Nothing it produces carries any authority. |
| **failure signature** | The clustering key for divergences: (cause, causal status, mechanism). A substituting override is a hard fail; a refining override is a soft fail. |
| **dawn gate** | The human review moment (`agent.dawn()` / `brevet dawn`). A named human or mission group applies one of `promote`, `hold`, `reject`, `re_elicit` to each candidate. Every decision is recorded as evidence. |
| **override** | A human judgment over an agent output, captured as the diff between the agent's draft and the human's shipped final, plus rationale and tags. Harvested automatically; nobody fills in a form. |
| **refining vs substituting** | The two override classes. Refining: the human kept the decision and changed its expression (a style or retrieval miss). Substituting: the human reached a different decision (a real failure). The delta engine treats them as soft and hard labels respectively. |
| **conservative gate** | The release acceptance rule: `Δ held-in ≥ 0 AND Δ held-out ≥ 0 AND max > 0`. A change may not trade one split against the other, even if the total improves. |
| **channel** | The exposure level of a release: `shadow` (observed, not acted on), `trial` (limited), `production`. Releases are earned through the channels, never hero-deployed. |

## Objects and authority

| Term | Meaning |
|---|---|
| **capability object** | The unit of learned capability: ⟨content, provenance, conditions, confidence, authority, validation-state⟩ plus a `kind`. One object model for everything an agent can learn; one lifecycle; one revocation mechanism. |
| **kind** | What a capability is: `prompt_rule`, `loop_policy`, `tool_binding`, `skill`, `eval_case`, `escalation_rule`, or `memory_fragment` (a binding to a Metis-governed fragment). |
| **authority layer** | How much a capability may influence behaviour. `evidence`: quarantined, exists only for review. `advisory`: may inform suggest-class behaviour. `controlled`: may shape act-class tool behaviour, requires formal review. Authority only ever increases through a recorded human decision. |
| **endogenous / exogenous** | Where a capability came from. Endogenous: the system inferred it from the agent's own operational history; it enters the Evidence layer strictly as a hypothesis, flagged with endogenous provenance, held to a higher evidence bar, and it can never be promoted by the process that proposed it. Exogenous: a human carried it in or confirmed it (for example, compiled eval cases whose labels are human finals). Reviewers use exogenous human judgment to ground and verify endogenous inferences: the pathway changes how a capability is found, not what it must satisfy to be trusted. |
| **mission group** | The accountable review board: a named panel of humans, never a single expert, that examines candidate capabilities, weighs the evidence and its normative alignment, resolves conflicts, decides promotion or rejection, and can request further elicitation (`re_elicit`). In Brevet, mission groups also sign releases and issue recalls. Identity namespace `mission_group:*` (for example `mission_group:right_first_time`). |
| **approver identity** | The recorded identity behind a promotion, release, or recall: `human:<who>` or `mission_group:<which>`. Identities in `agent:*`, `model:*`, or `dream:*` namespaces are structurally rejected: nothing can approve its own learning. |
| **validation state** | Where a capability is in its life: `captured` through `promoted_to_advisory` / `promoted_to_controlled`, or `held`, `rejected`, `re_elicit`, `withdrawn`, `superseded`, `expired`. Rejection is not deletion; the record stays. |
| **conditions (applicability context)** | The envelope a capability is valid in: task family, domain, model family, risk class, exclusions, validity window. Evaluated computationally before any similarity ranking; exclusions veto. |

## Evidence and change control

| Term | Meaning |
|---|---|
| **evidence envelope** | One record in the ledger: `brevet.task`, `brevet.artefact`, `brevet.override`, `brevet.candidate`, `brevet.promotion`, `brevet.release`, `brevet.recall`, `brevet.eval_run`, or `brevet.model_assist`. |
| **evidence chain** | The append-only ledger linking every envelope: `chain_hash = sha256(canonical(envelope) ‖ prev_hash)`. Anyone can replay it offline (`brevet verify`) and detect insertion, alteration, or deletion. |
| **capabilities.lock** | The capability bill of materials for one release: every approved capability with its content hash, authority layer, approver, and evidence references. Answers "what does this agent know and who approved it" in one file. |
| **release record** | One transition in the harness lineage: from-version, to-version, channel, locked capabilities, eval summary, approver, rollback target. |
| **recall** | Un-learning with proof: revoke one capability by id and content hash, enumerate every release whose lockfile contains it, drive `quarantine`, `rollback`, or `re_review`. A recalled capability can never resolve into a lockfile again. |
| **consent scope** | Declared in the manifest: which human-derived material (overrides, traces) the dream cycle may mine. Withdrawal of consent is honoured by recall, never by silent deletion. |
| **model assist** | Optional local-model drafting inside the dream cycle (Ollama by default). Drafts only, always logged as `brevet.model_assist` with `human_review_required: true`, never authoritative, fails soft to deterministic templates. |
| **Expert Agent Signature** | The five-layer manifest structure `agent.yaml` conforms to: identity and policy, prompt architecture, cognitive core, bindings, runtime safety. |
