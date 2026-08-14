# CHAP audit sink

Landing area for CHAP verdicts on their way into Brevet evidence.

A CHAP coordinator reached over MCP has no file path an ingester can
open, so the scheduled digest relays it: `chap_audit_read` returns audit
entries in the coordinator's own shape (`{seq, arrived, envelope}`),
which are appended here as `audit-<workspace>.jsonl`, and
`brevet_chap_ingest` reads this directory. A coordinator you can reach
as a SQLite store or a URL needs no relay: point `brevet_chap_ingest`
straight at it.

Nothing here is authoritative. It is a transport buffer: the evidence of
record is the hash-linked ledger in `../.brevet/ledger.jsonl`, and
ingestion grants no authority. Ingestion is idempotent twice over, by
per-source sequence cursor and by cross-path fingerprint, so re-running
is always safe.
