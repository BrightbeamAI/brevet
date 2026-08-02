# Case study: governing Claude itself

Claude Desktop and Cowork already form a self-evolving agent. Between
sessions, Claude learns through its memory, saved skills, and project
instructions, and that learning is ungoverned: nothing is versioned,
nobody signs anything, and there is no way to un-learn. This example
puts what your Claude learns under Brevet's change control. You become
the dawn gate.

It is also the fastest way to feel the loop on real work, because the
agent is one you already use every day, and the corrections you make in
normal conversation are the only input it needs.

## What you get

| Loop stage | In this setup |
|---|---|
| Work | Claude drafts in your chats, exactly as before |
| Override | when you ship your version of a draft, the pair (Claude's first draft, your final, your one-line reason, a tag) is recorded to a local ledger |
| Dream | on your command, recurring corrections (3+ with the same tag and family) become candidate rules with zero authority |
| Dawn | you promote, hold, or reject each candidate in plain chat; your identity is recorded; machine identities are structurally rejected |
| Release | approved rules lock into a signed `capabilities.lock` and are materialised into one governed rules file that future sessions load |
| Recall | one command withdraws a rule provably; the rules file is regenerated without it |
| Verify | any session can replay the hash-linked evidence chain |

Claude's standing instructions (the capture skill) require it to load
behavioural rules only from the governed file, only after the chain
verifies, and never to persist learned rules through memory or other
side channels. Persistence is earned at the dawn gate or not at all.

## An honest limitation

Brevet cannot intercept Claude's execution the way it wraps a Python
agent, because the assistant's harness is not yours to sign. Immutable
execution (requirement R2) therefore holds by verification, not
prevention: sessions check the chain and the governed file's hashes
before following any learned rule, and drift is detected rather than
blocked. Attribution, inventory, gated promotion, and provable recall
hold fully.

## Setup (about two minutes)

Requirements: the Claude desktop app, Python 3.10+, macOS (Linux and
Windows users: pass `--claude-config` with your platform's
`claude_desktop_config.json` path).

```console
$ git clone https://github.com/BrightbeamAI/brevet && cd brevet
$ bash examples/claude-cowork/setup.sh --owner you@example.com
```

Options: `--workspace DIR` (default `~/brevet-cowork`),
`--mission-group NAME` (default `review_board`), `--claude-config PATH`.

Then:

1. Quit Claude completely and reopen it (MCP servers load at startup).
2. Save the capture skill: open `skill/SKILL.md` in a Claude chat and
   say "save this as a skill named brevet-capture".
3. In a new conversation: "call brevet_status". You should see your
   agent at version 0.1.0 with a healthy chain.

Edit `~/brevet-cowork/governed/families.yaml` to name the kinds of
recurring work you want governed. Capture is consent-scoped: Claude
records only in those families, or when you say "log this to brevet".

## A week in the life

Monday: you ask for the weekly summary; Claude drafts; you reorder it
so escalations come first and ship it. Claude records one override,
tagged `escalations-first`, with your one-line reason. Wednesday and
Friday: the same correction, recorded the same way. Saturday: you say
"brevet dream, show me pending", and one candidate rule appears with
recurrence 3 and links to all three overrides. You say "promote it as
mission_group:review_board", then "release 0.2.0 on trial". The rule is
signed into the lockfile and written to the governed file; from now on,
summaries start with escalations because you approved that, and the
record says so. A month later, if the rule stops being right:
"recall it", and it is provably gone.

At any point: "what have I approved and why?" is answered from the
ledger, with capability ids, approvers, and rationales.

## What is stored, and where

Everything lives in your workspace, on your machine: the ledger
(`.brevet/ledger.jsonl`, append-only, hash-linked), the capability
store, the Ed25519 keys, the lockfile, and the governed rules file.
Recorded content is the task description, Claude's draft, the diff to
your final, your one-line rationale, and tags, attributed to the owner
identity in `agent.yaml`. Nothing is sent anywhere.

## Uninstall

Delete `~/.brevet/venv` and your workspace folder, remove the `brevet`
entry from `claude_desktop_config.json` (a timestamped backup sits
beside it), and delete the `brevet-capture` skill in Claude.
