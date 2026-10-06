# Brevet Specification (draft 0.4)

The five JSON Schemas in `schemas/` define Brevet's records. This document
states the rules a conforming deployment must enforce around them, using
MUST and SHOULD in their usual standards sense. For plain-language
definitions, see [GLOSSARY.md](GLOSSARY.md). This repository is the
reference implementation; [ABOUT.md](ABOUT.md#running-in-production) shows how
each control is switched on.

## 1. Objects

| Object | Schema | Role |
|---|---|---|
| Capability object | `capability_object.schema.json` | The unit of learned capability |
| Agent manifest | `agent_manifest.schema.json` | The harness as signed data (five-layer manifest) |
| Capabilities lock | `capabilities_lock.schema.json` | The bill of materials for one release: capabilities and harness |
| Release record | `release_record.schema.json` | One transition in the harness lineage |
| Recall notice | `recall_notice.schema.json` | The recall notice for one capability |

A **capability object** generalises the Metis tacit-fragment tuple
⟨content, provenance, conditions, confidence, authority, validation-state⟩
with a `kind`: `prompt_rule | loop_policy | tool_binding | skill | eval_case |
escalation_rule | memory_fragment`. Enum vocabularies (authority layers,
validation states, revocation states, source pathways) are shared verbatim
with Metis so fragments and capabilities interoperate without translation.
For `memory_fragment`, Metis remains the system of record; Brevet records
only the binding.

## 2. Authority invariants (MUST)

1. **Evidence never acts.** A capability with `authority_layer=evidence` MUST
   NOT appear in a lockfile, influence retrieval, or shape behaviour. It
   exists for review.
2. **No self-authorisation.** Promotion (any transition raising
   `authority_layer`) MUST be attributable to a human or mission-group
   identity. Implementations MUST reject approver identities in the
   `agent:*`, `model:*`, `dream:*` namespaces. A deployment that claims
   separation of authority MUST also authenticate the asserted identity and
   MUST prevent the proposing process from holding approval privileges.
   Signed approvals do this: once approvers are registered, every decision
   MUST carry signatures by registered approver keys that meet the decider's
   threshold, each signed request MUST be applied at most once, and every
   change to the approver register MUST be signed by an existing approver.
   No member acting alone may weaken a mission group: changing its
   threshold, adding or removing a member, or replacing a member's key MUST
   meet the group's own threshold, counted over its members before the
   change. An approver MAY always withdraw their own key. A signed register
   change MUST name the state of the register it was requested for (the
   last register change applied) and MUST apply only to that state, so a
   signature cannot be replayed later or on another chain. Approver
   keys SHOULD be verified against an identity source the organisation
   trusts, such as an OpenSSH allowed-signers file or a code host's published
   keys; a registration whose key the configured source does not list MUST be
   refused.
3. **Endogenous quarantine.** A capability with `source_pathway=endogenous`
   MUST enter at the Evidence layer strictly as a hypothesis and MUST NOT be
   promoted by any automated step of the process that proposed it. (Mirror of
   the Metis rule that endogenous fragments never self-promote.) Endogenous
   candidates SHOULD be held to a higher evidence bar than exogenous
   material, and reviewers SHOULD use exogenous human judgments to ground
   and verify endogenous inferences.
4. **Controlled means change control.** Promotion to `controlled` MUST carry
   `mission_group_reviewed_by` provenance and SHOULD reference formal change
   control. Only `controlled` capabilities may shape act-class tool behaviour.
5. **Rejection is not deletion.** Rejected/held capabilities remain stored as
   governed evidence with their lineage intact.

## 3. Conditions-first retrieval (MUST)

Applicability conditions are evaluated computationally BEFORE any semantic or
similarity ranking. A capability whose conditions do not match the runtime
context is withheld, not down-weighted. A condition restricting a capability
to a role, risk class or environment, and a trigger, MUST hold only where the
runtime context establishes it: unknown is not a match. Exclusion conditions
found in the task's context veto. A capability outside its validity window
(`valid_from`, `valid_until`) MUST NOT be served, and a bound that cannot be
read counts as outside it.
Out-of-envelope near-matches SHOULD trigger escalation to a human rather than
return a confident-looking precedent.

## 4. The circadian contract

- **Work (execution):** the runtime executes exactly one signed manifest
  version. Outside the `shadow` channel, unsigned or hash-mismatched
  manifests MUST be refused. A working agent MUST NOT modify its own
  harness, lockfile, or capability store authority fields.
- **Dream (the dream cycle):** offline, the dream cycle computes the
  difference signal `Δ = enacted ⊖ specified` over matched episodes (traces
  and overrides against the signed harness and procedures), groups
  recurring divergences by failure signature, and emits candidates. The
  failure signature is `φ = (cause, causal_status, mechanism)`: the cause is
  the override's task family, the causal status its kind (`hard_fail` for a
  substituting override, `soft_fail` for a refining one) and the mechanism
  its first descriptive tag. Overrides with the same signature form one
  group.
  A candidate MUST record provenance to its supporting traces/overrides and a
  recurrence count. Proposal drafting MAY use a model (local by default);
  model assistance MUST be logged and its output treated as a draft.
- **Dawn (promotion):** a human or mission group reviews candidates and issues one of
  `promote | hold | reject | re_elicit`. Every decision is an evidence
  envelope.

## 5. Evals and the gate

Regression suites are compiled from override history: a substituting override
(`intent_preserved=false`) yields a regression case whose expected outcome is
the human's final; a refining override MAY yield a rubric case (this
repository compiles substituting overrides only). Eval cases are
capability objects and carry provenance. Before any non-shadow release, the
candidate set MUST pass the conservative gate:

    Δ_held_in ≥ 0  AND  Δ_held_out ≥ 0  AND  max(Δ_held_in, Δ_held_out) > 0

Trading one split against the other is rejection, even if the total improves.
The gate is not a no-regression test: gains and losses can cancel within one
split, so deployments SHOULD also check paired, case-level regressions on
protected cases. Evaluation results MUST be bound to the exact candidate set,
cases, scorer and configuration being released: a release bound to two eval
runs takes its deltas from them, the 'before' run MUST have evaluated the
current release and the 'after' run the capabilities, harness and manifest
being released, both on the same cases with the same scorer and repeats.
Deltas supplied without runs MUST be recorded as attested, naming the
approver who attests them, and production releases SHOULD require measured
runs. Stochastic evaluation SHOULD aggregate over repeats. A release the gate
blocks SHOULD be recorded, so the cost of governance is visible.

## 6. Releases

A release: (1) resolves all `releasable` capabilities into a lockfile with
per-entry `content_hash`, `conditions_digest`, approver, and promotion refs;
(2) signs a commitment to both the manifest and the lockfile digest
(Ed25519 over an agreed canonical encoding, RFC 8785 for interoperability,
signature excluded from the signed payload; this repository uses sorted-key
compact JSON, which matches RFC 8785 except for some float forms); (3) records a release envelope
with `rollback_to`. Channels SHOULD progress `shadow → trial → production`.
A rollback MUST be a new signed release that names the release it restores
(`restores`), carrying that release's manifest content, harness and
capabilities minus any recalled or decided against since; implementations
SHOULD keep an archive of each release (manifest, lock and the content of its
named harness files) so it can be restored.

Every releasable capability MUST match a promotion decision on the evidence
chain (outcome, authority layer and content hash); a capability marked
promoted without one MUST NOT be locked. The lockfile SHOULD also carry a
harness bill of materials, `harness`: a digest for each file, live-agent
component and library version that shapes the release, with the sources
taken in `harness_sources`. Outside the shadow channel, a deployment MUST run
only the latest release on the evidence chain: the manifest's content hash
MUST equal that release's `manifest_hash`, its signature MUST verify against
the signing key the release recorded, and the lockfile's digest MUST equal
the manifest's `release.lockfile_hash`. Before each run the live harness
SHOULD be compared with the locked one. A difference is drift: it is
recorded once as a `brevet.drift` envelope, and outside the shadow channel a
changed file or agent component MUST stop the run unless
`runtime_safety.harness.on_drift` is `record`; an unrecognised policy value
MUST be treated as `block`. A rule served to an agent MUST match its lock
entry's `content_hash` and `conditions_digest`, and a released rule switched
off without a recall or decision on the evidence chain MUST be withheld and
reported, not silently dropped.

## 7. Recall

A recall notice revokes one capability by id and content hash, enumerates
every release whose lockfile contains it, and records the action requested:
`quarantine | rollback | re_review`. After recall, the capability MUST fail
`releasable`, and its content hash MUST be excluded from every future lockfile
while the recall is in force, so identical content cannot return under a new
id. The one exception is a recall whose `reason_class` is `duplicate`: it
withdraws one byte-identical copy, MUST be refused unless another active
capability holds the same content, and leaves that content to the copy that
stays. The recall decision and the affected releases MUST be reconstructible from
the evidence chain. Every serving point (a wrapped agent, an assistant
session) MUST acknowledge a recall that affects its release with a
`brevet.recall_ack` envelope once the capability no longer reaches the agent;
an agent whose rules the serving point cannot withhold MUST NOT run outside
shadow while its release ships the recalled capability. A recall is complete
when every agent that shipped the capability has acknowledged it. Derived
capabilities SHOULD be traced through their provenance and reviewed.

## 8. Evidence

All envelopes (`brevet.task`, `brevet.artefact`, `brevet.override`,
`brevet.candidate`, `brevet.model_assist`, `brevet.promotion`,
`brevet.release`, `brevet.recall`, `brevet.eval_run`, `brevet.approver`,
`brevet.drift`, `brevet.recall_ack`, `brevet.tool_call`, `brevet.consent`,
`brevet.gate`) are append-only and hash-linked
(`sha256(canonical(envelope) || prev_hash)`), independently replayable, and
CHAP-compatible: when a live CHAP coordinator is configured, envelopes are
mirrored to it; the local chain remains the offline-verifiable copy. Each
envelope SHOULD name the runtime that wrote it (`runtime`); approval records
older than a chain's first such envelope replay under the rules of the
release that wrote them, and every later record MUST meet the current rules.
Replay detects edits that break the chain. To detect truncation, rewriting or
replacement, the chain head SHOULD be anchored outside the workspace (a file
the agent cannot write, or a CHAP coordinator's audit log) after every
governing envelope, each anchor naming the chain it belongs to and signed by
the workspace key; verification MUST then fail when the chain no longer holds
an anchored head.

## 9. Consent

Where provenance includes human-derived material (overrides, whisper
responses, worker traces), consent follows the Metis consent record:
attribution mode, visibility, withdrawal. The dream cycle MUST NOT learn from
overrides outside the manifest's consent scope. `consent_withdrawn` is a
recall reason class; withdrawal MUST be honoured by recall, not by silent
deletion: the participant's overrides stop counting, and every capability
built on them is recalled.

## 10. Tools

The manifest declares the agent's tools in four tiers, the read/act contract:
`read` and `suggest` tools are always allowed; `act` tools MUST NOT run on the
shadow channel; `controlled_act` tools MUST run only in production and only
with a human or mission-group grant for that call. Once any tool is declared,
an undeclared tool MUST be refused, and a name matching several tiers takes
the strictest. Refusals and every act or controlled_act call SHOULD be
recorded as `brevet.tool_call` envelopes, with the grantor where there is one.
Budgets in `runtime_safety.loop.budgets` SHOULD cap tool calls and tool
errors per task.
