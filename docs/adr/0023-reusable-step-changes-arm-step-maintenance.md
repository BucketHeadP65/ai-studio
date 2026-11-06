---
id: adr-0023-reusable-step-changes-arm-step-maintenance
type: adr
title: A new reusable step version arms an on-step-change schedule through the library's outbox
summary: 'Saving a reusable step version above every saved version of its name writes ReusableStepSaved into the outbox of engram_workflows in the same transaction as the page, a worker in the harness server drains that outbox and arms every on-step-change schedule, and the fire passes the collected step ids to the workflow parameter changed_step_ids'
status: accepted
aliases:
- adr-0023-reusable-step-changes-arm-step-maintenance
app: root
created: '2026-11-05'
---

## Context

A workflow keeps the copy of each reusable step it was saved with. When a new version of a step is saved, nothing
tells the workflows that hold an older copy. The owner wants a maintenance workflow, `step-maintenance`, to check every
consumer of the new version and adopt it where it fits, without anyone starting it by hand.

Reusable steps reach the library only through `harness import`, a CLI process. The scheduler runs in the harness
server. An observer inside the CLI cannot reach the server's scheduler. Both processes share one database,
`engram_workflows`, which holds the library's pages, the schedules and the `coordination` tables that the other arms
already record their events in.

## Decision

D1. **A new reusable step version raises `ReusableStepSaved` in the transaction that writes its page.** `put_step`
hands the event to the library's ingest only when it writes a version above every saved version of the name, live,
superseded or archived. An equal re-save, a version below the newest, a cases update and a supersession rewrite raise
nothing. engram's ingest records the events its caller hands it on the load's unit of work before it commits, so the
row in `coordination.outbox` lands with the page or not at all. engram learns nothing about steps. The event carries
the step's name, its version, its id `st.<name>.v<version>`, its content address, who saved it and the live lower
versions it retires.

D2. **The harness server drains the library's outbox with a worker of its own.** The worker reads
`coordination.outbox` of `engram_workflows` every 30 seconds through a `PostgresOutbox` and delivers each
`ReusableStepSaved` to one subscriber, `StepChangeArming`, registered under the name `schedules.on_step_change`. It
serves no job and runs no periodic. It is the only reader of that outbox, and it closes any other event type there
undelivered. An event written while the server is down is delivered after it starts. A delivery whose arm fails stays
in the outbox for the next pass.

D3. **A schedule can wait for a reusable step change.** The cadence `on step change`, optionally with
`quiet <N><s|m|h>`, never comes due by the clock. Each delivered event moves the fire of every enabled on-step-change
schedule to the moment of delivery plus its quiet period, 30 minutes unless the cadence names one, at most 24 hours.
The quiet period counts from delivery, not from the save, so events delivered after a stop still wait it out.

D4. **Arming collects the changed steps, once per event.** The arm records the event id in
`coordination.event_deliveries` under `schedules.on_step_change` in the same transaction that moves the fires and adds
the event's step id to the new column `public.schedules.pending_step_ids`, each id once. An event delivered twice arms
nothing the second time. The worker finds the delivery the arm recorded under the same name, so a delivery cut short
after the arm is not handed to the arm again. Two events naming the same step inside one quiet period give one fire
that passes the step once.

D5. **A launched fire passes the changed steps to `changed_step_ids`.** The ids are sorted and comma separated. The
fire's stamp removes exactly the ids it passed, so a step a later event added while the fire ran waits for the next
fire, and the schedule keeps the fire that event gave it. A held or refused fire keeps every id. Saving or updating an
on-step-change schedule is refused when its workflow declares no `changed_step_ids` parameter, and a fire whose newest
workflow version declares none is stamped refused and rests.

D6. **An event that finds no on-step-change schedule is consumed and arms nothing.** The arm records the event even
when no schedule waits for it, and the worker closes its row. A schedule created later never sees it. The maintenance
schedule is made by hand, with the cadence `on step change quiet 10m` and gate mode `auto`. Steps saved before it
exists are maintained only by running `step-maintenance` by hand with their ids.

## Consequences

- Migration 0074 adds the column `pending_step_ids` to `public.schedules`. It must be applied before the harness that
  reads it starts, since every schedule read names the column.
- `harness import` and the server now meet at the outbox of `engram_workflows`, beside the routes the studio posts to.
- One import that saves twenty step versions gives one fire, passing twenty ids.
- A reusable step version saved while no on-step-change schedule exists is never maintained on its own.
- A second worker over the same outbox, with other subscribers, would lose its events to this one. Any future reader
  of that outbox joins this worker's bus instead.
- engram's index and incremental services record `PagesLoaded` and `CitingPagesChanged` in the outbox of the database
  they index, so a knowledge base indexed in `engram_workflows` would have its rows closed undelivered by this worker.

## Options considered

- **An HTTP route the CLI posts to after the import.** Refused. It is a second delivery path with its own failures, and
  an import while the server is down would lose the event. The outbox already lands with the page.
- **A cadence per step, `on step <name>`.** Refused. One schedule per reusable step is bookkeeping no person can keep.
- **Count the quiet period from the save, `occurred_at`.** Refused. A backlog delivered after a stop would set every
  fire in the past and fire before the rest of the backlog was delivered.
- **Keep the rows of events no schedule waits for until one exists.** Refused. The first fire after the schedule is
  made would carry every step saved since the worker first ran, with no bound on how old they are.
