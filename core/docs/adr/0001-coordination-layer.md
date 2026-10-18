---
id: core-adr-0001-coordination-layer
aliases: ["core-adr-0001-coordination-layer"]
type: adr
app: core
status: accepted
tags: []
title: "0001 — The coordination layer: Postgres jobs, an outbox, a delivery ledger, one worker"
---

# 0001 — The coordination layer: Postgres jobs, an outbox, a delivery ledger, one worker

**Status:** Accepted (2026-08-26).

## Context

The bus drained events in memory after a handler committed, so a crash between the commit and a subscriber's effect lost
the signal. Work ran inline in whichever process received the call, guarded by per-process memory, so two processes could
run the same job. Two apps polled Postgres on their own loops. The deployment is one machine, one Postgres, and a handful
of processes, which is the scale Postgres primitives cover.

## Decision

`core.cqrs` owns a `coordination` schema with three tables. `jobs`, claimed with `FOR UPDATE SKIP LOCKED`, leased with a
heartbeat, unique per `(kind, key)` while live. `outbox`, written by a unit of work inside the producing transaction.
`event_deliveries`, one row per (event, subscriber) handled. `MessageBus.handle` routes a command and hands the handler's
events to the outbox. `Worker` claims jobs, delivers events once per subscriber, and runs periodics, waking on
`pg_notify` with a polling fallback. A subscriber whose effect is a database write commits its delivery with it (exactly
once), every other subscriber may see one repeat after a crash (at least once). In-memory adapters are the default for
one-shot processes and the doubles for tests. Engram's alembic chain carries the migration, a drift test holds the
migrated database to core's `MetaData`.

## Consequences

Events are durable and delivered at least once. A subscriber with an outside effect must tolerate a repeat. Every app
that emits or delivers events runs a worker. The bus no longer cascades events, a subscriber that must emit is a
transactional one. No broker. Revisit when a second host or a consumer outside these processes appears.
