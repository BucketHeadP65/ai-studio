---
id: adr-0020-refresh-runs-arm-schedules
type: adr
title: A refresh run reaches the harness over HTTP, and schedules fire on a refresh run or a run's end
summary: 'Studio reports each ended refresh run to the harness through its outbox, the harness tells the owner and arms schedules whose cadence waits for a refresh or for another workflow, and an armed schedule fires once and rests'
status: accepted
aliases:
- adr-0020-refresh-runs-arm-schedules
app: root
created: '2026-10-06'
---

## Context

The refresh runs hourly and finds new sources. Nothing tells the owner. The digest and flag workflows start only
by hand, and no step hands work to the next one. The owner wants a refresh that finds sources to notify them and
start the digest, and a digest that writes digests to start the flag pass.

Studio owns the refresh and the harness owns schedules, runs and the owner's notifier. The two applications share
no code, so the report has to cross the boundary as data.

## Decision

D1. **Studio reports every ended refresh run to the harness.** Its `harness_forward` subscriber posts every
completed refresh run, and every abandoned one that created sources, to `POST /api/schedules/refresh-runs` over
`infrastructure.harness.base_url`. The outbox delivers it again until the harness answers. Studio names no
workflow.

D2. **The harness notifies the owner and arms schedules.** The harness sends the notification through its own
notifier. It then arms every enabled `on refresh` schedule whose workflow card's knowledge-base selector holds
that knowledge base.

D3. **A run's end arms the schedules waiting for it.** A run that ends `succeeded` or `partial` arms every enabled
`after <workflow name>` schedule over a shared knowledge base. The fire runs with the schedule's own stored
arguments. Nothing passes the ids of what the run wrote.

D4. **The event id is recorded with the arm.** It is written to `coordination.event_deliveries` of
`engram_workflows` in the same transaction as the arm, so a report delivered twice arms nothing the second time.

D5. **An armed schedule fires on the next tick through every hold and rests.** It then has no next fire until it
is armed again. An overlap or a blackout hold keeps the arm. Two arms before the tick give one fire.

## Consequences

- No migration. `cadence` stays a text column and the receipt table already exists.
- No workflow or knowledge base name appears in code. A schedule's knowledge base comes from its card's selector.
- A harness that is down gets the event when it is back. The notification waits with it.
- Runs on the Temporal engine write an outcome of `unknown`, so an `after <workflow name>` schedule never fires
  after them. The default engine is in process.
- An after-run arm carries no event id, so two runs ending before the tick give one fire.
- The counts a failed refresh attempt brought in are carried by the worker process that ran it. An attempt a
  previous process ran is not counted, and the next completed refresh still digests every undigested source.
- The route is unauthenticated, like every other harness mutating route, behind the same CORS and trusted-host
  rules.

## Options considered

- **Notify from studio by moving the notifier to core.** Refused. The notifier stays in the harness, so studio
  never notifies.
- **Pass the digest ids to the flag run.** Deferred. An empty list already means the recent digests no flag links
  yet, and ids can be added if re-reading costs too much.
- **A new receipts table.** Refused. A migration blocks every refresh until both databases are upgraded.
- **Match a schedule through a parameter named `collection`.** Refused. It puts a template's word in harness code.
