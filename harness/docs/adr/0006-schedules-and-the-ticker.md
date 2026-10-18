---
id: harness-adr-0006-schedules-and-the-ticker
aliases: ["harness-adr-0006-schedules-and-the-ticker"]
type: adr
app: harness
status: accepted
tags: []
title: "0006 — Scheduled workflows: a data-only schedule, a 15-second ticker, and no new run shape"
---

# 0006 — Scheduled workflows: a data-only schedule, a 15-second ticker, and no new run shape

**Status:** Accepted (2026-08-24). Extends [0005](0005-runs-are-first-class-monitored-artifacts.md), whose runner sessions
and run archive this decision reuses without change.

**Superseded in part:** on 2026-08-26 by [0010](0010-temporal-by-default-and-a-harness-owned-local-server.md). The tick is
no longer its own `asyncio` task matched to an in-process run engine. It is a periodic on the coordination worker
(`core.cqrs.worker.Worker`).

**Superseded in part:** on 2026-08-28, on the rejected option below about letting the agent author schedules. The
governance that option asked for is `needs_approval` in `harness/src/harness/features/followme/acts.py`, over the
`SCHEDULE_AUTHORING_ACTS` named in `harness/src/harness/features/followme/catalog.py`. `schedule_workflow`,
`update_schedule` and `delete_schedule` each wait for the owner's approval in every session, not only a master one.
`run_schedule_now` does not, because it fires a schedule the owner already approved rather than standing one up or
changing one.

**Extended:** on 2026-10-06 by root [ADR 0020](../../../docs/adr/0020-refresh-runs-arm-schedules.md), two cadences that
wait for an event (`on refresh`, `after <workflow name>`), armed by a refresh run's report or a run's ending, and
resting between fires.

## Context

The owner ruled that scheduled workflows are specced now, at professional grade, not deferred.
A scheduled run has to become an ordinary run the moment it fires — the fleet, the gates, the archive, and the ledger must
not need a second code path to treat it as one — and a schedule itself needs somewhere durable to live, a cadence grammar
that covers both "every 30 minutes" and calendar-shaped cron intent, and an answer for what a gate does when nobody is
watching at 3am.

## Decision

**The schedule is a `public.schedules` row** (engram alembic `0028`, beside the workflow store's own tables), not a new
concept the runtime invents: `workflow_id`, `arguments`, `cadence` (raw text), `enabled`, `gate_policy`, `auto_approve_gates`,
`session_id` (the one runner session this schedule's fires reuse), and the tick log `created_at` / `last_run_at` /
`last_outcome` / `next_run_at`. `harness/features/scheduling/domain.py` parses `cadence` into exactly one of two closed
shapes — `IntervalCadence` (`"every 30m"`, this module's own regex) or `CronCadence` (a 5-field expression, validated and
walked forward by `croniter`) — and `next_after()` is the one function that turns either kind into its next fire time, so
a due-scan and a freshly stamped `next_run_at` can never compute two different answers for the same cadence. `gate_policy`
is `"wait"` (default: `ChatApproval`'s gate timeout never fires, the run waits for a person indefinitely) or
`"fail_after:<seconds>"` — which is `ChatApproval.review()`'s own two-strike rule, not a single deadline: the first
timeout on a gate is a quiet rejection with no feedback, so the upstream step reruns once and the same gate parks again;
only a *second* consecutive timeout on that same gate fails the run, honestly attested. Parsed by `gate_wait_seconds()`,
refused in words at write time rather than discovered wrong when a schedule fires unattended.

**The ticker is one `asyncio` task, not a durable workflow.** `tick_forever()` wakes every 15 seconds, and `tick_once()`
fires every enabled schedule whose `next_run_at` has arrived. Firing (`_fire()`) is the one function both the ticker and
`POST /api/schedules/{id}/run-now` call: it resolves the schedule's own runner session (`_runner_session()` — reuses
`schedule.session_id` if the pool can still answer for it, else mints one via `pool.create(None, session_kind="runner")`
and titles it after the workflow), then either records the tick as missed (`overlap_policy` is fixed at `"skip"` for v1 —
a due tick while the previous run is still live stamps `"missed (previous run still live)"` and never queues) or launches
through `running_service.start()` exactly as the Run button does, carrying `runner=f"app:schedule:{schedule.id}"` and the
schedule's own `gate_policy`/`auto_approve_gates`. A fire nobody watched and one an owner clicked through `run-now`
therefore leave the identical session state, archive record, and tick-log stamp behind them. Missed ticks while the
process was down are never replayed (a `catch_up` flag is out of scope for v1); the ticker never runs inside a request and
never blocks a request on a gate — a scheduled run parked at its gate is exactly a waiting card in the fleet, read the same
way any other waiting run is.

**Rejected.**
- A durable, cross-process scheduler (Temporal cron, or equivalent) — Temporal already owns durable *runs*; the ticker's
  job is only to decide *when* to start one, which an in-process 15-second loop does correctly for a single-owner studio,
  matching the run engine's own in-process default.
- A queued overlap policy (a due tick waits for the live run to finish, then fires) — v1 fixes `overlap_policy: "skip"`
  precisely so a schedule can never build a backlog of queued fires; a missed tick is a fact the tick log states, not
  work deferred to later.
- Letting the agent author schedules directly — governance for an agent that can stand up its own standing, recurring
  triggers needs its own design; v1 restricts creation to the owner's own doors (`POST /api/schedules`).

## Consequences

- A schedule fires through the exact same `running_service.start()` every other launch path uses; nothing downstream of a
  launch — the fleet's cards, the gate machinery, the archive, the ledger — needed a second implementation to understand a
  scheduled run.
- `next_after()` is the one seam a third cadence shape would extend; `parse_cadence()` is the one seam a new spelling of
  an existing shape would extend. Neither the ticker nor the due-scan should ever compute a next fire time on their own.
- A schedule's runner session is long-lived across fires (reused, not reminted), so a worker's memory or an orchestrator
  turn from one fire is, in principle, still addressable at the next — a fact schedule authors should know, not a bug to
  fix.
- `gate_policy: "fail_after:<seconds>"` means an unattended failed gate is an honest, attested failure, never a silent
  drop; an owner reading the fleet's Recent lane sees it exactly as any other failed run.
- Because ticks missed during downtime are never replayed, a schedule's actual fire history can visibly undercount its
  nominal cadence after a restart — deliberate, and stated in the tick log's own `last_outcome`, never hidden.
