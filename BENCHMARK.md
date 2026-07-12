# The Governed Adaptation Benchmark (design note, v0)

Self-evolving-agent papers score one thing: improvement. Production owners
need four. This benchmark scores them all, and systems not shaped like a
governed loop cannot pass it. That is the point.

## Task

Given:

- a task stream `T` (held-in and held-out splits) with executable outcomes
  where available,
- an **override stream** `O` in CHAP format (diff + rationale + tags +
  `intent_preserved`), replayed on a schedule, the human-judgment signal
  that substitutes for a verifier in non-benchmarkable work,
- an initial signed harness `h0`,
- a set of **revocation events** issued mid-stream (capability X, issued at
  time t), and
- a consent policy over override sources,

a submitted system runs its adaptation loop and produces a harness lineage
`h0 … hN` with lockfiles, promotion records, and an evidence log.

## Scored axes

1. **Improvement**: held-out pass-rate delta across the lineage
   (the only axis the current literature reports).
2. **Regression discipline**: frequency and magnitude of held-in/held-out
   degradations that survived promotion; violations of the conservative gate.
3. **Lineage completeness**: for a sampled set of behavioural changes
   between `hi` and `hj`, can the change be traced mechanically to: the
   capability that caused it, the evidence that motivated it, the eval run
   that tested it, and the identity that approved it? Scored by automated
   audit replay, not self-report.
4. **Recall compliance**: after each revocation event: latency until the
   capability stops influencing behaviour, correctness (no lockfile after
   recall contains it), and provability from the evidence chain alone.
   Includes one `consent_withdrawn` event.

Reported as a profile, not a single number: `(Δperf, regressions, lineage%, recall)`.

## Baselines to port

- **Self-Harness** (arXiv:2606.09498) on its DeepAgents harness. Expected:
  strong on axis 1, near-zero on axes 3-4 by construction.
- **No-adaptation** (frozen h0): the floor for axis 1, perfect on axes 2-4.
- **Brevet reference loop**: the reference implementation in this repo.

## Dataset

First release: synthetic-but-principled override corpora generated from the
worked scenarios (deviation triage, batch quality, shift handover), published
in CHAP envelope format, to our knowledge the first public dataset of
structured human overrides. Later releases: anonymised, consented corpora
from live regulated engagements.

## Non-goals

Not a capability benchmark (use Terminal-Bench/SWE-bench for that). Not a
memory-recall benchmark (use LoCoMo/LongMemEval). This benchmark measures the
property none of them touch: whether adaptation is *governable*.
