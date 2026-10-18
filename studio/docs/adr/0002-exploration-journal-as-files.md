---
id: studio-adr-0002-exploration-journal-as-files
aliases: ["studio-adr-0002-exploration-journal-as-files"]
type: adr
app: studio
status: superseded
tags: []
title: "0002 — The exploration journal is files, not Postgres"
---

# 0002 — The exploration journal is files, not Postgres

**Status:** Superseded (2026-08-21) — the Explore station was removed from the product; no successor decision replaces it.

**Context.** Every Explore session must persist — every manual act, agent turn, result, and error, resumable across restarts and readable
by tooling outside the server. Two Postgres databases already run (the knowledge base DB and the workflow-store DB), which makes tables the
reflexive answer. But the agent half of the studio is deliberately DB-less: it composes plainly in `studio_session()`, and everything it
persists today is a file — the run journal that re-parks gates after a restart, the attestation ledger, skills as data. The repo's one
alembic chain binds `engram.db.base.KbBase.metadata`; studio-owned tables cannot join it without the library learning about the app,
which the package boundary forbids.

**Decision.** A session is a directory under the agent state dir: `explore/<session-id>/meta.json` (name, created-at, knobs — rename
rewrites it) plus `journal.jsonl` (one entry per line, append-only, `seq` = line order), behind a `JournalStore` Protocol. Delete removes
the directory; Logfire keeps the immutable telemetry mirror and serves as the queryable, durable record. Graduating journals to Postgres
is a store-adapter swap behind the same Protocol, warranted only when out-of-band operators need SQL over journals.

**Consequences.**
- (+) Idiom-consistent with the agent half; zero new infrastructure; journals are readable with `cat` and greppable by any attached
  session or tool.
- (+) Payloads need no cap machinery: every stored result is already bounded by its tool (`k` caps hits, `run_sql` caps rows, disclosure
  envelopes are leveled).
- (−) No SQL over journals; cross-session questions are Logfire queries by design.
- (−) One writer assumed: the agent backend owns the directory. A second writer means the Postgres graduation, not file locks.

**References.** studio ADR-0001 (the drivers that write these entries).
