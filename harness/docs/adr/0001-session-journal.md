---
id: harness-adr-0001-session-journal
aliases: ["harness-adr-0001-session-journal"]
type: adr
app: harness
status: accepted
tags: []
title: "0001 — The session journal: the session on an event-sourced log"
---

# 0001 — The session journal: the session on an event-sourced log

**Status:** Accepted (2026-08-22). The running contract is
[`docs/reference/session-journal.md`](../reference/session-journal.md).

## Context

Session state lived as latest-state files (a forget marker, a draft snapshot, knob files) plus the step log. None of them could
answer "what happened, in what order?": crashed turns vanished silently, boundaries were a marker file, and past conversations
could not be searched. The deepseek-harness study named the discipline worth borrowing: an append-only session event log with
derived views ("model-visible means logged").

## Decision

One append-only JSONL file, `.state/session-journal.jsonl`, owned by the server process: every act that changes what the model
sees next turn appends one typed event (the grammar and its pure projections live in `harness.domain.events`; the file adapter
in `harness.adapters.journal`). **Hybrid authority, deliberately short of full event sourcing:** the journal is the timeline,
and the authority for the conversation lifecycle only — the Forget boundary is an event (the marker file migrated once at
boot), and turns a crash left open are closed as explicit `turn_abandoned` by the boot sweep. Message content stays in the step
log (events carry `run_id` references), attestations and activations stay in their ledgers, and loadout files remain what the
runtime reads; their events mirror the acts. CLI-process acts are ledgered but never journalled — the journal is what this
server process saw. The owner's window is `harness sessions list | tail | search` (a scan, not an index).

## Consequences

- Postmortems cite `seq` numbers; crashes are visible on the timeline instead of silently truncated.
- New model-visible seams must emit an event — extending the closed grammar is part of the change, test-first.
- The ACP door's turn events are deferred (the vendored ACP session owns that loop); its message content still lands in the
  step log.
- The journal is the spine later work rides: conversation fork/branch, and the composer-takeover gate UX.
