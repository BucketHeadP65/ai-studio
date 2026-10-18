---
id: harness-adr-0019-inspection-rides-the-coordination-backbone
aliases: ["harness-adr-0019-inspection-rides-the-coordination-backbone"]
type: adr
app: harness
status: accepted
tags: []
title: "0019 - The inspection rides the coordination backbone in the sessions database"
---

# 0019 - The inspection rides the coordination backbone in the sessions database

**Status:** Accepted (2026-09-09).

## Context

Before this slice the harness served no job kinds at all: nothing in the running process claimed work from a jobs
table, and the `coordination` schema (jobs, outbox, wake) existed only in the knowledge-base database, provisioned by
engram's own alembic chain. The record's `run_events` table already carried every event an inspection needs, a step
landing (`model_request_completed`) and a run ending (`run_completed`, `run_failed`), written by `StepPersistence` in
the same transaction as the step it describes. What was missing was a way to turn those events into work a worker
could claim, and a database to claim it from.

## Decision

The record's own events are the trigger. A cursor consumer, the dispatch, reads the events past its position
(`inspection.cursors`, row `inspection.dispatch`) and issues one `InspectRun` command per watch that selects the run.
Three lanes carry the work: `inspect_run` (the rules, immediate, one in flight per watch and run), `attribute_lead`
(the inspector, a model call on a tier, one per lead) and `scan_run` (the inspector, a model call on a tier, one per
run), each its own job kind and its own policy on the `coordination` schema.

The `inspection` schema lives beside `history` in the sessions database (`aifactory_sessions`), not in the
knowledge-base database. The `coordination` schema is created there too, from `core.cqrs.schema`'s own metadata
(`create_coordination_schema`), rather than depending on an alembic chain that has never touched this database. The
harness's wake, the `LISTEN`/`NOTIFY` that wakes an idle worker, listens on the sessions DSN, because that is now
where the jobs live. The inspection's own pool is opened with a fixed search path, `inspection,history`, so one
connection reads the record's events, moves the cursor, and resolves the inspection's own tables, with no connection
ever running a `SET search_path`.

`harness inspect --run <id>` and `--since <hours>` only enqueue: they issue commands into the jobs table and print
what they queued, then exit. The running harness's worker serves the jobs, the same worker that already runs the
schedules tick and the idle and archive sweeps. The retro pass (`retro.py`, `RetroPassRun`, `RetroConditions`,
`RetroConfig`, `ProjectConfig.retro`) is deleted whole; a flag's `source` field keeps `retro` as a value a reader
still accepts, so a session log line the retired pass wrote before this slice still decodes.

## Consequences

Dispatch is at-least-once across two connection stacks: the cursor moves on an asyncpg connection (the inspection
pool), the job is enqueued through a SQLAlchemy engine, and no one transaction spans both. A crash between the two
re-reads the same events on the next pass; re-issuing `InspectRun` for an event already handled only answers the job
already in flight, because the live key (`<watch>:<source>`) makes the enqueue idempotent. A consumer that has never
run starts at the record's current position rather than at 0, so a fresh boot never replays the whole record; the
sweep is the catch-up path for a run the dispatch missed, keyed on what the jobs ledger already shows for that watch
and run rather than on time alone.

Both schemas are created at boot, in every process that opens the inspection's pool or the jobs engine, behind an
advisory lock (`pg_advisory_xact_lock`) held on a sessions connection for exactly the DDL's own transaction, so two
processes booting at once serialize their `CREATE SCHEMA` and `CREATE TABLE IF NOT EXISTS` instead of deadlocking on
each other.

## Alternatives rejected

- **Publish to an outbox instead of reading `run_events` directly.** Rejected: a second write for every step, and an
  outbox subscriber inherits the fan-out limits already known for the coordination backbone.
- **One command for both lanes.** Rejected: a slow or refused model call would hold the same lane the free rules run
  on.
- **The inspection's tables inside `history`, alongside the record's own.** Rejected: the record is entered only
  through its own reads and the transcript loader; the inspection keeps its own tables in their own schema.
- **The cursor move and the jobs enqueue in one transaction**, as first proposed. Rejected: `core.cqrs`'s job store
  runs on SQLAlchemy and the record on asyncpg, two connection stacks that cannot share one transaction without
  putting both on the same stack.
- **Re-reading a whole event window every pass**, instead of advancing a position. Rejected: it would re-issue a
  command for every event in the window whose job had already finished.
- **The wake staying on the store (knowledge-base) database**, where it listened before any job lived anywhere.
  Rejected: the jobs now live in the sessions database, so the channel that wakes a worker for them has to live
  there too.
