---
id: harness-adr-0004-parallel-sessions-workspace-pool
aliases: ["harness-adr-0004-parallel-sessions-workspace-pool"]
type: adr
app: harness
status: accepted
tags: []
title: "0004 — Parallel sessions: a workspace pool, not one live slot"
---

# 0004 — Parallel sessions: a workspace pool, not one live slot

**Status:** Accepted (2026-08-23). Extends [0003](0003-rebuildable-session-projection.md) and supersedes the single-live-slot
reading of [0001](0001-session-journal.md). The client contract it serves is
studio-client ADR 0002.

## Context

The chat-first studio's interaction model is parallel-native: each session is its own agent with its own context and world, many
can be answering at once, and clicking a session changes what you view, never which agent answers. The harness held the opposite:
one live conversation per process, a single `Runtime` installed at boot, turns bracketed on one timeline, and a continue endpoint
re-pointing the slot. Two shapes deliver parallel sessions: one process per session under a supervisor (the shape Claude Code and
Codex children take, with the browser needing a spawner daemon), or a pool of runtimes inside the one server (the shape dsh runs).

## Decision

The pool. The :8770 process holds a **registry of runtimes keyed by session id** — a runtime is hydrated on demand from the stored
record, carries its own conversation, draft, and active run, and is evicted when idle. Turns in different sessions run
concurrently; a second turn in the same session is refused while one is in flight. Turn events carry their session id, and settle
and fail events the seq of the start they close (optional fields, so every old journal line decodes unchanged); the folds prefer
the stamps and keep the positional walk only for the legacy prefix. Each project carries its own journal and step store under
`<root>/.aifactory/state/`, and every emitter — the flag tool included — writes through its runtime's journal handle, never a
module-level path. The HTTP contract is session-scoped (`/api/sessions/{id}/...`) and deliberately transport-agnostic.

Rejected: process-per-session with a separate supervisor app. Strongest isolation and the CLI-agent norm, but it demands a
net-new supervisor (spawn, lifecycle, reverse proxy) before anything ships, and both approved specs were written against the
pool. Because the wire contract is identical under both shapes, the process split stays the recorded hardening path if pool
isolation ever proves insufficient.

## Consequences

- The one module-level slot is replaced by the registry; every session-scoped route resolves its runtime from the path.
- One crash takes every live session down — the accepted cost of the pool; the boot sweep closes each session's orphaned turns.
- Draft and active-run state stop being process singletons and live per session under the project's state home.
- Eviction, memory bounds, and a cap on simultaneous turns become owner policy, not architecture.
- The per-run money brake stays per runtime; pool-wide spend accounting is new work this decision creates.
