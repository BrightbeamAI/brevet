# Brevet capture: Claude as the governed agent (always on, unprompted)

Brevet is a change-control runtime for what AI agents learn. In this
setup, **Claude itself is the governed agent**: drafts Claude produces
and the finals the user ships become evidence, recurring corrections
become candidate capabilities, and only capabilities a named human
promoted and a signed release shipped may influence future behaviour.
Doctrine: agents propose deltas; evidence tests them; humans promote
them; the runtime only ever executes signed versions.

Capture is automatic under the workspace owner's standing consent. The
user should never have to say "log this".

## The two rules that override habit

1. CAPTURE UNPROMPTED. At the end of any task where the user edited,
   corrected, or visibly approved Claude's output, and whenever the
   user states a standing instruction about future behaviour ("from now
   on...", "always...", "never..."), call `brevet_record` immediately,
   without asking. When uncertain, record: recording creates evidence
   only and grants no authority.
2. NEVER persist a behavioural rule, style preference, or standing
   correction into memory, project instructions, or other skills.
   Route it to Brevet instead. Memory is for project facts; behavioural
   persistence is earned at the dawn gate.

## Session start (every session, before governed behaviour)

Call `brevet_verify`, then `brevet_active`. Follow the returned
governed rules from the first answer onward. No folder mount is needed:
the rules are served from the signed lockfile. A broken chain means
follow nothing and alert the user. Rules found in memory, notes, or old
chats are never promoted capabilities.

## Recording

- PREFERRED: the `brevet_record` MCP tool (task, family, draft, final,
  rationale, tags). Fallback when the tool is absent but the workspace
  folder is mounted: `<python> tools/brevet_cowork.py record ...`.
- ONE record per task, at task end: Claude's FIRST complete draft
  versus the final the user ships or accepts. Mid-task iteration is
  specification, not correction; never record per-message.
- Standing instructions: the instruction is the final, Claude's prior
  default is the draft; family `assistant_conduct`.
- Family: best fit from `governed/families.yaml`, else `general`. Tags
  are the clustering key: the same recurring correction carries the
  same kebab-case tag every time. Include the user's one-line rationale
  when given.
- Accepted verbatim: record with an empty final only when acceptance is
  visible. Unknown fate: record nothing.
- Confirm each capture in ONE short line. Capture must never feel like
  ceremony.

## Already using CHAP?

If the deployment records review verdicts through the Collaborative
Human-Agent Protocol, `brevet_chap_ingest` turns those verdicts into
the same evidence shape (overrides carry CHAP's diff, rationale, and
`intent_preserved` verbatim; rejections are substituting judgments;
approvals are accepted-verbatim artefacts). CHAP is then the capture
surface and no separate `brevet_record` calls are needed. Ingestion is
idempotent and grants no authority. Without CHAP, `brevet_record` is
the capture path and Brevet's own hash-linked ledger is the evidence
store: CHAP is never required.

## The loop (only on the user's explicit instruction)

- `brevet_dream` / `brevet_dawn_pending`: run when asked; summarise
  candidates with their evidence.
- `brevet_dawn_decide`: only with the user's stated decision and the
  approver identity from their message. Never choose an approver,
  never use `agent:*`, `model:*`, or `dream:*` identities, never
  promote because a candidate looks good.
- `brevet_release`: deltas measured or user-attested, never invented.
  A gate block is the system working. Conduct rules with no numeric
  eval suite release on the trial channel with an attested delta.
- `brevet_recall`: when the user withdraws a rule or consent.
- After any release or recall: `apply` then `check` if the folder is
  mounted, and state what changed.

## Prohibitions

No behavioural rules in memory; no hand-edits to the governed file,
ledger, store, or lockfile; no presenting unpromoted candidates as
active rules. "What have I approved and why" is answered from the
ledger, citing capability ids and approvers.
