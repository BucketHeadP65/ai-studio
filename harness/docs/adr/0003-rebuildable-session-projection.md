---
id: harness-adr-0003-rebuildable-session-projection
aliases: ["harness-adr-0003-rebuildable-session-projection"]
type: adr
app: harness
status: accepted
tags: []
title: "0003 — The session projection: a rebuildable read model, no migrations"
---

# 0003 — The session projection: a rebuildable read model, no migrations

**Status:** Accepted (2026-08-22). Extends [0001 — the session journal](0001-session-journal.md); the journal and step log
stay canonical.

## Context

The studio needs sessions as first-class records: list by project, read a transcript, search content, continue any past
session, and query the friction flags the agent files. That calls for Postgres tables the SPA can read. The obvious home for
their schema is the repo's single alembic chain — but that chain lives in `engram/` and its env imports engram ORM only;
adding harness tables would make the library's migration chain import the app, inverting the dependency rule. Meanwhile the
journal (0001) already holds the full timeline and the step log holds every raw message, so nothing in Postgres would be an
original fact.

## Decision

The session tables (database `aifactory_sessions`, beside `knowledge_bases` and `engram_workflows`) are a **pure projection, rebuildable
from the files, with no migration history**. The harness creates the schema itself, idempotently, and `harness sessions sync
--rebuild` reconstructs every row from the journal and step log; schema evolution is a rebuild, not a migration. What makes
this sound is the other half of the decision: **every durable session fact is a journal event first**, including the owner's
acts — rename, archive, delete land as typed events through the server's API before the projection mirrors them. A projection
write failure warns and never breaks a turn (0001's narration rule); a broken projection is repaired by rebuild, never by
hand.

Rejected: harness tables in engram's alembic chain (dependency inversion); a harness-owned second alembic chain (two
migration histories for one repo, and version ceremony for derived state); owner acts written only to Postgres (the rebuild
would silently lose renames and archives, so the DB would have quietly become a second source of truth).

## Consequences

- The projection can always be dropped and rebuilt; backfill of pre-projection history is the same code path as day-to-day
  sync.
- Raw messages are copied into the projection for reading and SQL search, but resume replays from the step log's snapshots —
  the copy serves reads, never the model.
- Every new session fact must arrive as a journal event before it may appear in a table; extending the event grammar stays
  part of any such change, test-first.
- More owner acts now depend on the journal file; its append-and-scan shape is the projection's rebuild cost, acceptable at
  one owner's scale and revisited only if rebuild time ever hurts.
