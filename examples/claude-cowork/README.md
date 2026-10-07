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
| Dream | When you ask, overrides that recur (three or more with the same task family, kind and first tag) become candidate rules. Candidates have no authority. |
| Dawn | You promote, hold or reject each candidate in plain chat. Your identity is recorded, and approvals under machine identities are rejected. |
| Evals | Trial releases need the conservative gate on the deltas you attest: neither half of the eval cases may get worse, and at least one must improve. Production releases need measured eval runs, unless `agent.yaml` allows attestation. |
| Release | Promoted rules go into a signed release with its `capabilities.lock`. Sessions fetch them with `brevet_active`, which checks the chain and its anchors, the lock, the signature and each rule before serving it. `brevet_rollback` returns to an earlier archived release (every release from Brevet 0.4.0 on), on its channel or a lower one. |
| Recall | One command recalls a rule. `brevet_active` lists it as recalled, Claude stops applying it at once and confirms with `brevet_acknowledge`, and later releases leave it out. |
| Verify | Any session can replay the evidence chain and check it against its anchors, so edits, truncation or a replaced history are caught. |

Claude's standing instructions (the capture skill) tell it to take learned rules only from `brevet_active`, which serves nothing when the evidence chain or the release fails its checks, and never to persist learned rules through memory or any other side channel. A rule is meant to last only if you promoted it at dawn. When the workspace folder is mounted, `tools/brevet_cowork.py apply` also writes the active rules to `governed/ACTIVE_CAPABILITIES.md` as a readable copy.

Capture is unprompted by design: setup turns on automatic capture for this workspace (`auto_capture: true` in `agent.yaml`), so you never have to say "log this". Sessions call `brevet_verify` and `brevet_active` at the start,
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

Running both is safe: a CHAP verdict that matches an override already recorded in the session is skipped rather than counted twice, and the import reports `duplicates_skipped`. Identical corrections on different tasks still all count. This matters because an override counted twice would make a one-off look like recurrence, and a recurrence dropped would hide a real pattern.

**Reaching a CHAP coordinator that is only available through MCP.** If
your CHAP coordinator is a database file or a URL, point
`brevet_chap_ingest` straight at it. If it is reachable only as MCP tools,
there is no file to open, so copy its records across: `chap_audit_read`
returns entries in the coordinator's own format; append them unchanged to
`<workspace>/chap-sink/audit-<workspace_id>.jsonl`, and ingest that
directory. The scheduled digest in `digest/dawn-digest.md` does exactly
this: it appends only the entries after the sink's last one, skips cleanly
when there are none, and never lets a relay failure block the digest. Run `setup.sh --chap-workspace <id>` to
create the sink.

**Permission prompts.** Checks that interrupt the work tend to get
switched off, so pre-approve the tools: choose "Always allow" the
first time a `brevet_*` prompt appears. On Team and Enterprise plans an
organisation setting (Organization settings, Cowork, Permissions,
"Allow 'Always allow' for connector tools") may need enabling first,
and a project-level `.claude/settings.json` listing the `mcp__brevet__*`
tools under `permissions.allow` covers sessions in that project.

## Signed approvals (optional)

By default your dawn decisions are recorded under the identity you give, and
an assistant that can call the Brevet tools could type that identity too. To
make every decision provably yours, register a key once, in Terminal:

```console
$ ~/.brevet/venv/bin/brevet approver add --identity human:you@example.com \
    --group mission_group:review_board --workdir ~/brevet-cowork/.brevet
```

Then release again and sign it: once an approver is registered,
`brevet_active` serves rules only from a signed release. From then on,
"promote it" in chat makes Claude prepare a request and give you a
`brevet approve` command. Nothing changes until you run it in Terminal
and enter your passphrase; the key never leaves your Mac and Claude cannot use
it. One command signs everything you agreed to in a dawn session:
`~/.brevet/venv/bin/brevet approve --all --workdir ~/brevet-cowork/.brevet`.

## Claude's own harness (optional)

Skills, `CLAUDE.md` files and connector settings shape Claude as much as
learned rules do. Name them in `agent.yaml`, and every release locks a
digest of each one. The lock holds digests only; the release archive in
`.brevet/objects/` keeps a copy of each file so a rollback can restore it,
so leave out files holding secrets you do not want copied there:

```yaml
bindings:
  harness_files:
    - ~/.claude/CLAUDE.md
    - ~/.claude/skills/
    - ~/Library/Application Support/Claude/claude_desktop_config.json
```

Release straight after editing `agent.yaml`: until the edit is released,
`brevet_active` serves no rules, because the manifest is no longer the one
signed (a workspace whose last release predates Brevet 0.2.0 gets a warning
instead). From then on, `brevet_active` reports any of these files that
changed since, Claude tells you at the start of the session, and
`brevet_harness` lists them. Release again to accept a change.

## Anchoring the record (recommended)

Keep a copy of the evidence chain's head off the machine, so a history that
is cut short, rewritten or replaced is caught. One line in your own
configuration, outside the workspace, is enough; a synced folder works:

```console
$ mkdir -p ~/.config/brevet
$ echo "file:$HOME/Library/Mobile Documents/com~apple~CloudDocs/brevet-anchors.jsonl" \
    >> ~/.config/brevet/anchors
$ ~/.brevet/venv/bin/brevet anchor --workdir ~/brevet-cowork/.brevet \
    --manifest-path ~/brevet-cowork/agent.yaml
```

The last command anchors the history the workspace already has. Brevet then
anchors after every dawn decision, release and recall; the weekly digest
anchors the week's evidence with `brevet_anchor`, and `brevet_verify`,
`brevet_active` and the Claude Code hook refuse a chain that no longer holds
its anchored heads.

## Claude Code: tool tiers (optional)

In Claude Code, declare which tools Claude may use in which tier under
`bindings.tools` in `agent.yaml`, release it, and add Brevet as a hook in
`.claude/settings.json`. The hook applies the tiers and channel of the latest
release: an undeclared tool is refused, an act tool is refused on the shadow
channel, in production Claude Code asks you before a controlled_act call
(elsewhere it is refused), and the PostToolUse hook records your grant. A
call the hook cannot check is refused.

```json
{"hooks": {
  "PreToolUse": [{"matcher": "*", "hooks": [{"type": "command",
    "command": "~/.brevet/venv/bin/brevet hook --workdir ~/brevet-cowork/.brevet --manifest-path ~/brevet-cowork/agent.yaml"}]}],
  "PostToolUse": [{"matcher": "*", "hooks": [{"type": "command",
    "command": "~/.brevet/venv/bin/brevet hook --workdir ~/brevet-cowork/.brevet --manifest-path ~/brevet-cowork/agent.yaml"}]}]}}
```

## How the checks hold Claude to its releases

Sessions take learned rules only from `brevet_active`, which checks the
evidence chain and its anchors, the approvals, the lock, the release
signature, and each rule's content and conditions before serving anything.
It reports harness files or an `agent.yaml` changed since the release, and
lists recalled rules, which Claude stops applying and acknowledges on the
record; in a session, that acknowledgement is the assistant's own
confirmation, while a wrapped Python agent's comes from Brevet withholding
the rule. The records are complete: who promoted each rule, what each release
contains, what was recalled and who confirmed it.

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

Requirements: the Claude desktop app, Python 3.10+, and macOS or Linux
(on Linux, pass `--claude-config` with your `claude_desktop_config.json`
path); the script needs bash. The Python environment install is
the slow part; everything else is seconds.

```console
$ git clone https://github.com/BrightbeamAI/brevet && cd brevet
$ bash examples/claude-cowork/setup.sh --owner you@example.com
```

Options: `--workspace DIR` (default `~/brevet-cowork`),
`--mission-group NAME` (default `review_board`), `--claude-config PATH`,
and `--chap-workspace wsp_id` to enable the CHAP relay described above.

The script creates the workspace, sets your identity in the manifest (it is signed at your first release), turns on automatic capture, registers the MCP server, and writes
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
always-on defaults. Claude files each capture under the family that fits,
and under `general` when none does.

## A week in the life

Monday: you ask for the weekly summary; Claude drafts; you reorder it
so escalations come first and send it. Claude records one override,
tagged `escalations-first`, with your one-line reason. Wednesday and
Friday: the same correction, recorded the same way. Saturday: you say
"brevet dream, show me pending", and one candidate rule appears, built
from all three overrides. You say "promote it as mission_group:review_board", then "release 0.2.0 on trial", attesting the before-and-after deltas. The release is signed, `capabilities.lock` lists the rule, and `brevet_active` serves it from the next session; from now on, summaries start with escalations because you approved that, and the record says so. A month later, if the rule stops being right: "recall it", and `brevet_active` stops serving it, with the recall on the record.

At any point: "what have I approved and why?" is answered from the
ledger, with capability ids, approvers, and rationales.

## What is stored, and where

Everything lives on your machine: in your workspace, the ledger
(`.brevet/ledger.jsonl`, append-only, hash-linked), the capability store,
the workspace's Ed25519 key, `capabilities.lock`, the release archive
(`.brevet/releases/` and `.brevet/objects/`) and the governed rules file;
in `~/.config/brevet/`, your approver keys and anchor settings. Recorded
content is the task description, Claude's draft, the diff to your final,
your one-line rationale, and tags, attributed to the owner identity in
`agent.yaml`. Nothing is sent anywhere unless you configure an anchor or
a CHAP coordinator, which receive what this page describes for them.

## Uninstall

Delete `~/.brevet/venv`, your workspace folder and `~/.config/brevet`,
remove the `brevet` entry from `claude_desktop_config.json` (a timestamped
backup sits beside it), and delete the `brevet-capture` skill in Claude.
