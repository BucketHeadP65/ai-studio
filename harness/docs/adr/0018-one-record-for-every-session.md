---
id: harness-adr-0018-one-record-for-every-session
aliases: ["harness-adr-0018-one-record-for-every-session"]
type: adr
app: harness
status: accepted
tags: []
title: "0018 - One record for every session, and one chat path"
---

# 0018 - One record for every session, and one chat path

**Status:** Accepted (2026-09-07). Amends [0003](0003-rebuildable-session-projection.md): the projection is still a
read model, and what it is rebuilt from moves from files to the record.

## Context

Every session's conversation was a file store: `.state/steps` for the checkout and
`<root>/.aifactory/state/steps` for each project. Which session a run belonged to was which directory it sat in. The
vendored store writes the whole history again at every step, so a conversation cost steps times history, and two
surfaces asking the same question had to agree on which directory to open. The chat had three ways in: two unscoped
entries acting on the process's own session, and the session-scoped stream the studio's own chat rail used.

## Decision

One record, `core.ai.history`'s `PostgresStepStore`, built once (`adapters/wiring.py`) and answering two ports:
`SessionTranscript` (the step store the app already knew, indexing every step of a run for recall) and
`ConversationHistory` (`history_for_next_run`, the record's newest complete step, for a fresh turn's history). Its
ten tables (`runs`, `run_events`, `steps`, `messages`, `message_forms`, `message_stamps`, `instructions`,
`request_parameters`, `step_parameters`, `tool_effects`) sit in their own Postgres schema, `history`, inside the same
`aifactory_sessions` database the projection already uses, reached through the record's own connection pool rather
than the projection's: the pool's search path is fixed at connect time, so no connection ever runs a `SET
search_path`, and the record's own `messages` table never collides with the projection's. A `run_events` row also
carries a global position, `id`, assigned when the row is inserted rather than when its transaction commits, and
`events_after` in `core.ai.history.reads` reads the events past a position, oldest first, each joined with the
run's own agent and conversation. Every root that needs the record (the server, the ACP entry, every console verb)
opens that pool and applies its schema through one function, `record_database()`, behind an `OpenRecordPool` port a
test can answer with a fake.

Sessions are told apart by the conversation id every run already carries, so the per-project step directory and the
per-project store are gone, and every read of the record names a conversation: recall, boot resume, and the retro
pass. `harness sessions import` copies the runs of the old file stores into the record, once, keyed by run id.

Beyond the messages, the record also keeps what every request was sent with. `RequestParametersRecorder`, mounted
beside step persistence on the agent, writes each request's tool definitions and model settings into
`request_parameters` (content-addressed, one row per distinct set) and `step_parameters` (one row per step, pointing
at the set that step used), keyed by the run the transcript itself registered rather than the run id pydantic-ai
hands the capability, because the two can differ. The fork exposes that registered run id publicly
(`active_run_id()`) so any host mounting both capabilities gets rows that join. Since the fork was removed, the recorder
finds that run id in `RunContext.capabilities` instead.

One chat path, `POST /api/sessions/{session_id}/messages`, serving both the studio's own chat rail and the
control-room drawer. Its body is this app's own submit-message type over pydantic-ai's, carrying the new message,
the page the person is looking at, and the browser's answers to a turn that stopped for one. A session already held
in this process answers from its own live conversation first, and only falls back to the record when this process
is holding nothing for it (freshly resumed or never opened), which is what lets a deferred turn's tool answers
reach the run that is still waiting for them rather than a stale one rebuilt from the record. One `wire.py` holds
the protocol, a `Chat` port and a `Turn` hold the run, and the route only translates. The three routes it replaces,
the two unscoped entries and the earlier session-scoped one described above, are deleted.

Deleting a session deletes its conversation in the record, a cascade from its id.

## Consequences

The projection keeps its five tables and its rebuild, reading snapshots through the port instead of a directory.
Recall and the retro pass read the asking session's own conversation rather than its own directory, which is
narrower than what a shared checkout directory used to give them. Every chunk the studio reads stays on the one
path: the protocol's own, plus the step reading each finished model response leaves and the settled payload the
panel takes its draft from. A turn is now always answerable, because every caller of the one path is a browser
that can answer a deferred call.

Two sessions can write to one record at once without one's rows leaking into the other's: the same run ids appear
in both `public.messages` (the projection) and `history.messages` (the record), but as rows of two distinct
relations, one keyed to a session's project and the other to its conversation id.
