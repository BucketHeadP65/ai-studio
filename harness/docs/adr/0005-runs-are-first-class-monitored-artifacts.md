---
id: harness-adr-0005-runs-are-first-class-monitored-artifacts
aliases: ["harness-adr-0005-runs-are-first-class-monitored-artifacts"]
type: adr
app: harness
status: accepted
tags: []
title: "0005 — Runs are first-class: the archive, a true Stop, and runner sessions"
---

# 0005 — Runs are first-class: the archive, a true Stop, and runner sessions

**Status:** Accepted (2026-08-24). Extends
[0003](0003-rebuildable-session-projection.md) and [0004](0004-parallel-sessions-workspace-pool.md).

## Context

`RunJournal.close()` used to unlink the journal on any ending, so a finished run left behind only what the attestation
ledger happened to record — a shape, never the values, the graph it ran, or the gates it crossed. Pause was the only way to
stop a run short of completion, and a person cancelling a run outright got the same on-disk leftovers a process crash would,
with no record that the ending was deliberate. Manual runs and schedules also had nowhere honest to live: a run needs an
owning session (B4 of the spec), but minting an ordinary chat session for it would put a run in the rail as if it were a
conversation. The interim shipped ahead of this decision — a runner session born already archived, so the rail's normal
listing skipped it by accident of its `archived` flag rather than by what it actually was.

## Decision

**The archive.** On any ending — completed, failed, or stopped, never a pause — the journal's content plus an ending
envelope is written to a durable record before the on-disk journal is removed. `RunArchiveRecord`
(`harness/features/running/archive.py`) carries `run_id` (`<ended-at-utc-compact>-<uuid4-short>`, sortable and
collision-free), `outcome`, `failure`, `started_at`/`ended_at`, `runner` (`app:harness | app:chat | app:schedule:<id>`),
`session_id`, `params`, the **spec snapshot** (`WorkflowSpec.model_dump(mode="json")`, so a historical run renders the graph
as it was, never as the store now holds it), `values`, `conversations` (each worker step's messages at the moment the run
ended), `events` (the transition log, so the timeline replays), `gates_approved`/`gates_rejected`/`gates_auto`, and
`pinned`. Both ending paths — the Run button's `launch()` in `features/running/service.py` and the chat door's `_execute`
in `features/running/tools.py` — call the one `archive_run()` function, so a run archives identically whichever door ran
it. The module reads and writes through the `RunArchive` protocol (`put`, `get`, `list_for`, `recent`, `set_pinned`,
`delete`, `sweep`); `FileRunArchive`, one JSON file per run under `.state/runs/archive/<workflow_id>/<run_id>.json`, is the
only adapter today, so a blob store is a future adapter behind the same port, not a rewrite. Retention is
`RunSettings.archive_days` (`APP_RUN__ARCHIVE_DAYS`, default `30`; `0` turns archiving off outright — `archive_run` becomes a
no-op); `pinned: true` survives every sweep, the owner's per-run "forever".

**Stop, distinct from Pause.** `POST /api/run/stop` (and the session-scoped `/api/sessions/{id}/run/stop`) end a run for
good, unlike Pause: `session.active.stopping` is set first, the task is cancelled and awaited out exactly as a pause
unwinds, then the parked journal is read back, attested with `outcome: "stopped"`, archived, and its on-disk file
unlinked — nothing is resumable afterward. `Attestation.outcome` (`core.ai.workflows`) widens by this one value; trust
derivation treats `stopped` the same as `failed`, confirming nothing. The `stopping` flag is how the chat door's
`run_workflow` tells apart its three cancellation causes on the same `asyncio.CancelledError`: a person paused (the turn
survives, the run's journal stays parked), a person stopped (the turn survives, the run is over for good), or the turn
itself was cancelled by something outside the run (the cancellation travels on, and what it leaves behind is a pause,
unchanged from before this decision). Refused on the durable engine in words, exactly as pause already was.

**Runner sessions become first-class.** `SessionStarted` gains an optional `session_kind: str = "chat"` field, so an old
journal line still decodes as `"chat"` with no migration. `SessionPool.create(project_root, session_kind=...)` stamps it,
and the sessions projection (`features/sessions/projection.py`) carries it as a `session_kind` column, added idempotently
alongside the table's other `ALTER TABLE ... ADD COLUMN IF NOT EXISTS` statements; `harness sessions sync --rebuild`
reprojects existing rows from the journal. `POST /api/sessions` accepts `kind` (default `"chat"`); `GET /api/sessions`
answers chat sessions only unless asked with `kind=all` (`features/sessions/controller.py`). Manual runs and schedules mint
`kind: "runner"` sessions, titled after the workflow they drive (`SessionRenamed`); a runner session is reachable by id —
the run card and the chat drawer address it directly — it is simply never rail furniture. The interim archived-at-birth
trick is deleted outright: a runner session's absence from the rail is now a property of its `session_kind`, not an
accident of its `archived` flag.

**Rejected.**
- Keeping runs ephemeral, with only the attestation ledger's shapes surviving an ending — the owner's mandate (D1) is
  full per-node output history with a 30-day default and a per-run pin, which the ledger's append-only evidence record
  was never built to hold.
- One ending event with no separate archive path — `completed`, `failed`, and `stopped` need distinct trust semantics
  (a stop and a failure both confirm nothing; a completion can), so collapsing them into a single "ended" outcome would
  either lose that distinction or push it onto every reader to reconstruct.
- Filtering runner sessions out of the rail on the client, by convention rather than by a stamped kind — every future
  reader of the session list would have to know and re-apply the same convention; a `session_kind` on the record is one
  fact the server states once.

## Consequences

- `RunJournal.close()` is no longer the whole ending story: both launch paths now write an archive record before closing
  the journal, so extending what an ending records means touching `archive_run` and its one call in each path, not the
  journal.
- The `RunArchive` protocol means a future blob-store adapter (S3 and kin) is a `put`/`get`/`list_for`/`recent` implementation,
  never a rewrite of the two callers.
- A run's history renders the spec as it ran, not as the store holds the artifact today — deliberate, and it means a
  historical run's graph can visibly diverge from the live one without that being a bug.
- `Attestation.outcome` is a closed `Literal` that every reader (trust derivation, the ledger's own consumers) must now
  handle a third way; a stop is recorded honestly rather than folded into "failed" or dropped.
- Every session record carries a `session_kind`; the sessions projection's schema evolves by rebuild (0003's own rule),
  not a migration, and old journals default cleanly to `"chat"`.
- `harness sessions list` and the projection's `GET /api/sessions` must be read with `kind=all` to see runner sessions —
  the default view is deliberately the chat-only one the rail always wanted.
