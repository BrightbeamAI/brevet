# Brevet Specification (draft 0.1)

The five JSON Schemas in `schemas/` define Brevet's records. This document
states the rules a conforming deployment must enforce around them, using
MUST and SHOULD in their usual standards sense. For plain-language
definitions, see [GLOSSARY.md](GLOSSARY.md). Version 0.1 of this repository
implements the records and steps; the README's *Project status* section and
the paper's implementation table list the requirements a deployment adds.

## 1. Objects

| Object | Schema | Role |
|---|---|---|
| Capability object | `capability_object.schema.json` | The unit of learned capability |
| Agent manifest | `agent_manifest.schema.json` | The harness as signed data (five-layer manifest) |
| Capabilities lock | `capabilities_lock.schema.json` | The capability bill of materials for one release |
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
context is withheld, not down-weighted. Exclusion conditions veto.
Out-of-envelope near-matches SHOULD trigger escalation to a human rather than
return a confident-looking precedent.

## 4. The circadian contract

- **Waking (execution):** the runtime executes exactly one signed manifest
  version. Outside the `shadow` channel, unsigned or hash-mismatched
  manifests MUST be refused. The waking agent MUST NOT modify its own
  harness, lockfile, or capability store authority fields.
- **Sleeping (the dream cycle):** offline, the delta engine computes the
  difference signal `Δ = enacted ⊖ specified` over matched episodes (traces
  and overrides against the signed harness and procedures), clusters
  recurring divergences by failure signature
  `φ = (cause, causal_status, mechanism)`, and emits candidates.
  A candidate MUST record provenance to its supporting traces/overrides and a
  recurrence count. Proposal drafting MAY use a model (local by default);
  model assistance MUST be logged and its output treated as a draft.
- **Dawn (promotion):** a human or mission group reviews ranked candidates and issues one of
  `promote | hold | reject | re_elicit`. Every decision is an evidence
  envelope.

## 5. Evals and the gate

Regression suites are compiled from override history: a substituting override
(`intent_preserved=false`) yields a regression case whose expected outcome is
the human's final; a refining override yields a rubric case. Eval cases are
capability objects and carry provenance. Before any non-shadow release, the
candidate set MUST pass the conservative gate:

    Δ_held_in ≥ 0  AND  Δ_held_out ≥ 0  AND  max(Δ_held_in, Δ_held_out) > 0

Trading one split against the other is rejection, even if the total improves.
The gate is not a no-regression test: gains and losses can cancel within one
split, so deployments SHOULD also check paired, case-level regressions on
protected cases. Evaluation results MUST be bound to the exact candidate set,
cases, scorer and configuration being released. Stochastic evaluation SHOULD
aggregate over repeats.

## 6. Releases

A release: (1) resolves all `releasable` capabilities into a lockfile with
per-entry `content_hash`, `conditions_digest`, approver, and promotion refs;
(2) signs a commitment to both the manifest and the lockfile digest
(Ed25519 over an agreed canonical encoding, RFC 8785 for interoperability,
signature excluded from the signed payload); (3) records a release envelope
with `rollback_to`. Channels SHOULD progress `shadow → trial → production`.
A channel label or rollback target records an intended action; it does not
deploy or restore an agent by itself.

## 7. Recall

A recall notice revokes one capability by id and content hash, enumerates
every release whose lockfile contains it, and drives an action:
`quarantine | rollback | re_review`. After recall, the capability MUST fail
`releasable`, and its content hash MUST be excluded from every future lockfile
while the recall is in force, so identical content cannot return under a new
id. The recall decision and the affected releases MUST be reconstructible from
the evidence chain. Showing that running endpoints have stopped using the
capability additionally requires their acknowledgement. Derived capabilities
SHOULD be traced through their provenance and reviewed.

## 8. Evidence

All envelopes (`brevet.task`, `brevet.artefact`, `brevet.override`,
`brevet.candidate`, `brevet.promotion`, `brevet.release`, `brevet.recall`,
`brevet.eval_run`) are append-only and hash-linked
(`sha256(canonical(envelope) || prev_hash)`), independently replayable, and
CHAP-compatible: when a live CHAP coordinator is configured, envelopes are
mirrored to it; the local chain remains the offline-verifiable copy. Replay
detects edits that break the chain. Detecting truncation or a complete
rewrite requires the chain head to be anchored outside the local machine,
for example in a CHAP coordinator's transparency log (SHOULD).

## 9. Consent

Where provenance includes human-derived material (overrides, whisper
responses, worker traces), consent follows the Metis consent record:
attribution mode, visibility, withdrawal. `consent_withdrawn` is a recall
reason class; withdrawal MUST be honoured by recall, not by silent deletion.
