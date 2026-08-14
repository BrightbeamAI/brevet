# Weekly dawn digest (scheduled task template)

Create this as a scheduled task in Claude so the loop keeps its rhythm
without anyone remembering to run it. Ask Claude: "create a scheduled
task called brevet-dawn-digest that runs Fridays at 9am with this
prompt", then paste the prompt below, replacing the two placeholders:

- `<WORKSPACE>`: your Brevet workspace path (default `~/brevet-cowork`)
- `<CHAP_WORKSPACE>`: your CHAP review workspace id, or delete step 2
  entirely if you do not use CHAP

Click **Run now** once after creating it. Approvals granted during a run
are stored on the task, so later runs never pause on permission prompts.

---

You are running the weekly Brevet dawn digest. Brevet is a change
control runtime governing what this Claude learns; its MCP tools
(`brevet_*`) point at the governed workspace at `<WORKSPACE>`. CHAP
tools (`chap_*`) may also be available; the review workspace is
`<CHAP_WORKSPACE>`.

Run these steps in order.

1. Chain health. Call `brevet_verify`. If `chain_ok` is false, report
   ONLY that, prominently: the evidence chain failed verification and it
   must be investigated before anything else. Stop there.

2. Relay CHAP verdicts into evidence (skip cleanly if nothing to do).
   a. Call `chap_workspace_describe` for `<CHAP_WORKSPACE>`. If
      `task_count` and `override_count` are both 0, skip to step 3 and
      note "no CHAP verdicts this week".
   b. Otherwise call `chap_audit_read` for that workspace.
   c. Append the entries, one JSON object per line exactly as returned
      (each has `seq`, `arrived`, `envelope`), to
      `<WORKSPACE>/chap-sink/audit-<CHAP_WORKSPACE>.jsonl`. Create the
      file if absent. Never rewrite or reorder existing lines: it is an
      append-only transport buffer.
   d. Call `brevet_chap_ingest` with `source` set to the `chap-sink`
      directory. Report overrides, approvals, rejections and
      `duplicates_skipped`. A non-zero `duplicates_skipped` is healthy:
      a judgment already captured in-session was not counted twice.
   e. If any step here fails, note it in one line and continue. A relay
      problem must never block the digest.

3. Call `brevet_dream` to mine newly recorded overrides into candidates.

4. Call `brevet_dawn_pending` and `brevet_status`.

5. Write a short digest: chain health, envelope count, CHAP relay
   result, then each pending candidate on one line (capability id,
   title, recurrence). If the queue is empty, say so in one sentence.

6. Close by showing the exact decision phrasing, for example:
   "promote cap_xxx as mission_group:<your group>" or "reject cap_xxx".

Never promote, hold, reject, release or recall anything yourself: dawn
decisions require the owner's identity and explicit instruction, always.
Keep the digest under 18 lines.
