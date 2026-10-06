# Case study: governing Claude itself

Claude Desktop and Cowork already learn as you use them. Between
sessions, Claude picks up new behaviour through its memory, saved skills
and project instructions, but none of it is versioned, nobody approves it,
and there is no way to take a lesson back. This example puts what your
Claude learns under Brevet's change control, with you as the approver.

It is also the quickest way to see the loop on real work, because the
agent is one you already use every day, and the corrections you make in
normal conversation are the only input it needs.

## What you get

| Stage | In this setup |
|---|---|
| Work | Claude drafts in your chats, exactly as before. |
| Override | When you change Claude's draft and use your own version, Claude records the pair (its first draft and your final) as an override, with your one-line reason and a tag, without being asked. |
| Dream | When you ask, overrides that recur (three or more with the same tag and task family) become candidate rules. Candidates have no authority. |
| Dawn | You promote, hold or reject each candidate in plain chat. Your identity is recorded, and approvals under machine identities are rejected. |
| Release | Promoted rules go into a signed release with its `capabilities.lock` and are written to one governed rules file that future sessions load. |
| Recall | One command recalls a rule; the governed rules file is regenerated without it. |
| Verify | Any session can replay the evidence chain to detect edits to its history. |

Claude's standing instructions (the capture skill) tell it to load
learned rules only from the governed rules file, only after the evidence
chain verifies, and never to persist learned rules through memory or any
other side channel. A rule lasts only if you promoted it at dawn.

Capture is unprompted by design: you should never have to say "log
this". Sessions call `brevet_verify` and `brevet_active` at the start,
so the rules you approved are followed from the first answer, with no
folder mount required.

**Choose one capture path.** A deployment records each human judgment
once, through whichever surface it already has:

- **Brevet only** (the default): `brevet_record` captures in-session and
  Brevet's hash-linked ledger is the evidence store. Nothing else is
  required, and nothing leaves the machine.
- **CHAP as the capture surface**: if your review decisions are already
  recorded with the Collaborative Human-Agent Protocol, `brevet_chap_ingest`
  imports them as overrides. A CHAP override keeps its diff, rationale, tags
  and `intent_preserved`; a rejection counts as a substituting override; an
  approval counts as a draft accepted verbatim. Brevet remains the learning
  gate.

Running both is safe: an override already recorded in the session is
skipped rather than counted twice, and the import reports
`duplicates_skipped`. This matters because an override counted twice would
make a one-off look like recurrence and produce a candidate from evidence
that never recurred.

**Reaching a CHAP coordinator that is only available through MCP.** If
your CHAP coordinator is a database file or a URL, point
`brevet_chap_ingest` straight at it. If it is reachable only as MCP tools,
there is no file to open, so copy its records across: `chap_audit_read`
returns entries in the coordinator's own format; append them unchanged to
`<workspace>/chap-sink/audit-<workspace_id>.jsonl`, and ingest that
directory. The scheduled digest in `digest/dawn-digest.md` does exactly
this, skips cleanly when there are no new verdicts, and never lets a
relay failure block the digest. Run `setup.sh --chap-workspace <id>` to
create the sink.

**Permission prompts.** Checks that interrupt the work tend to get
switched off, so pre-approve the tools: choose "Always allow" the
first time a `brevet_*` prompt appears. On Team and Enterprise plans an
organisation setting (Organization settings, Cowork, Permissions,
"Allow 'Always allow' for connector tools") may need enabling first,
and a project-level `.claude/settings.json` listing the `mcp__brevet__*`
tools under `permissions.allow` covers sessions in that project.

## What Brevet can and cannot enforce here

Brevet cannot sit between you and Claude the way it wraps a Python agent,
because the assistant's harness belongs to its vendor, not to you. So the
controls here detect problems rather than prevent them. Sessions verify the
evidence chain and the governed rules file before following any learned
rule, and a mismatch
shows up rather than being blocked. The records are complete: who promoted
each rule, what each release contains and what was recalled. But a passing
check cannot prove that Claude read only the governed rules file, or that it
stopped using a recalled rule already in its context. The paper's case study
describes this boundary in full.

## Before you run it

Three values are yours to choose; nothing else needs editing.

| Value | Flag | Where it ends up |
|---|---|---|
| Your email | `--owner you@example.com` | `agent.yaml` as `human:<email>`, the accountable identity on every capture and decision |
| Your mission group's name | `--mission-group review_board` | `agent.yaml` as `mission_group:<name>`, the approver identity you promote with |
| Your CHAP workspace id | `--chap-workspace wsp_...` | creates the audit sink; omit entirely if you do not use CHAP |

After setup, two placeholders in `digest/dawn-digest.md` take the same
values when you schedule the weekly task: `<WORKSPACE>` (your workspace
path) and `<CHAP_WORKSPACE>` (delete that step if you skipped CHAP).
The skill and every other file work as shipped.

## Setup (about two minutes)

Requirements: the Claude desktop app, Python 3.10+, macOS (Linux and
Windows users: pass `--claude-config` with your platform's
`claude_desktop_config.json` path). The Python environment install is
the slow part; everything else is seconds.

```console
$ git clone https://github.com/BrightbeamAI/brevet && cd brevet
$ bash examples/claude-cowork/setup.sh --owner you@example.com
```

Options: `--workspace DIR` (default `~/brevet-cowork`),
`--mission-group NAME` (default `review_board`), `--claude-config PATH`,
and `--chap-workspace wsp_id` to enable the CHAP relay described below.

The script creates the workspace, sets your identity in the signed
manifest, registers the MCP server, and writes
`<workspace>/.claude/settings.json` pre-approving the `brevet_*` tools.

Then:

1. Quit Claude completely and reopen it (MCP servers load at startup).
2. Save the capture skill: open `skill/SKILL.md` in a Claude chat and
   say "save this as a skill named brevet-capture".
3. In a new conversation: "call brevet_status". You should see your
   agent at version 0.1.0 with a healthy chain.
4. Schedule the weekly digest: paste `digest/dawn-digest.md` into a
   chat, ask for a Friday 9am scheduled task, and click **Run now**
   once so its tool approvals are stored on the task.

If you keep your work in a different project folder, copy
`settings/claude-settings.json` to `<that folder>/.claude/settings.json`
so sessions there are pre-approved too.

Edit `~/brevet-cowork/governed/families.yaml` to name the kinds of
recurring work you want governed; `assistant_conduct` (standing rules
about how Claude works) and `general` (everything else) are the
always-on defaults. Capture is consent-scoped: Claude
records only in those families, or when you say "log this to brevet".

## A week in the life

Monday: you ask for the weekly summary; Claude drafts; you reorder it
so escalations come first and send it. Claude records one override,
tagged `escalations-first`, with your one-line reason. Wednesday and
Friday: the same correction, recorded the same way. Saturday: you say
"brevet dream, show me pending", and one candidate rule appears, built
from all three overrides. You say "promote it as
mission_group:review_board", then "release 0.2.0 on trial". The rule is
signed into `capabilities.lock` and written to the governed rules file; from now on,
summaries start with escalations because you approved that, and the
record says so. A month later, if the rule stops being right:
"recall it", and it is provably gone.

At any point: "what have I approved and why?" is answered from the
ledger, with capability ids, approvers, and rationales.

## What is stored, and where

Everything lives in your workspace, on your machine: the ledger
(`.brevet/ledger.jsonl`, append-only, hash-linked), the capability
store, the Ed25519 keys, `capabilities.lock` and the governed rules file.
Recorded content is the task description, Claude's draft, the diff to
your final, your one-line rationale, and tags, attributed to the owner
identity in `agent.yaml`. Nothing is sent anywhere.

## Uninstall

Delete `~/.brevet/venv` and your workspace folder, remove the `brevet`
entry from `claude_desktop_config.json` (a timestamped backup sits
beside it), and delete the `brevet-capture` skill in Claude.
