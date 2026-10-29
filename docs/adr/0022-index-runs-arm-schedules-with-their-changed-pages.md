---
id: adr-0022-index-runs-arm-schedules-with-their-changed-pages
type: adr
title: An index tells the harness which pages it changed, and an on-index schedule passes them to its run
summary: 'Studio forwards the pages each index changed to the harness, which collects them on every on-index schedule naming that knowledge base and fires it once its quiet period passes, with the page ids in the workflow parameter changed_page_ids'
status: accepted
aliases:
- adr-0022-index-runs-arm-schedules-with-their-changed-pages
app: root
created: '2026-10-28'
---

## Context

The research knowledge base keeps one stance page per topic. Each says what engram does and holds engram's position
as its status. The pages drift from engram's own docs. On 2026-10-09 seven of the eight still read `considering`
for mechanisms engram ships. The owner wants a workflow that refreshes them whenever the repo-docs knowledge base is
indexed.

Nothing fires when a knowledge base is indexed. The index records `PagesLoaded` in studio's outbox with counts only,
and nothing subscribes to it. Root ADR 0020 D3 also says no fire passes the ids of what a run wrote, so a workflow
started by an index could only read every page again.

## Decision

D1. **An index records the pages it changed.** `PagesLoaded` gains a required `page_ids` field with no default. It
holds the page ids of the note files that are new to the manifest the index started from, or whose content hash
differs from it, taken before any embedding backfill widens the set. A page that was only re-linked is not listed,
and neither is a source mirror. The full index reads the stored manifest before it writes the new one and applies
the same rule. When the stored manifest is empty or belongs to another knowledge base root, `page_ids` is empty. The
dry run records an empty tuple.

D2. **Studio forwards each index's changed pages to the harness.** Its `harness_index_forward` subscriber
(`HarnessIndexForward`, in studio's indexing slice) posts `{event_id, knowledge_base, page_ids}` to
`POST /api/schedules/index-runs` over `infrastructure.harness.base_url`. It finds `knowledge_base` in the registry of
knowledge bases, as the one whose schema's `retrieval.collection` is the event's `collection`. It forwards nothing
when `page_ids` is empty or no registered knowledge base matches. A transport error, a 404 or a 5xx raises, so the
outbox delivers it again, as it does for the harness forward.

D3. **A schedule can wait for an index.** The cadence `on index <knowledge base>`, for example `on index repo-docs`,
fires a schedule after the named knowledge base is indexed with changed pages. The name follows the slug grammar
`[a-z0-9]+(-[a-z0-9]+)*`. The cadence names the watched knowledge base itself, and arming matches that name, not
the card's knowledge-base selectors.

D4. **An on-index schedule waits out a quiet period.** It fires only once its quiet period passes with no further
arm. The quiet period is 30 minutes unless the cadence names one, as in `on index repo-docs quiet 10m`. The other
event cadences keep an earlier fire. An on-index arm instead moves the fire to the moment of arming plus the quiet
period.

D5. **Arming collects the changed pages, and a launched fire passes them to the run.** Arming records the event id
in `coordination.event_deliveries` under the subscriber `schedules.on_index`, in the same transaction as the arm, so
an event delivered twice arms nothing the second time. Each matching enabled schedule adds the event's page ids to
the new column `public.schedules.pending_page_ids`. A fire that launches a run passes the collected ids, sorted and
comma-separated, to the workflow parameter `changed_page_ids`. Its stamp removes exactly the ids it passed, so ids
that arrive after the fire read them wait for the next fire. A held or refused fire keeps every id.

D6. **An on-index schedule needs a workflow that takes the changed pages.** Saving or updating an `on index`
schedule is refused when its workflow declares no `changed_page_ids` parameter. A fire whose newest workflow version
declares none is refused and rests, and the schedule keeps its ids.

## Consequences

- This reverses root ADR 0020 D3 for the on-index cadence alone. The ids of what an index changed now reach the
  run. On-refresh and after-run fires still run with the schedule's own stored arguments.
- Migration 0073 adds the column `pending_page_ids` to `public.schedules`.
- Studio and the harness now meet at three edges: the run value read of root ADR 0019, the harness forward of root
  ADR 0020, and the harness index forward.
- No stored `PagesLoaded` row is moved forward. Every one was closed undelivered, so none is decoded again.
- A docs edit that changes no page the stance workflow admits still starts a run. That run costs one judge call,
  the approval of an empty list.

## Options considered

- **Arm by the card's knowledge-base selectors, as the on-refresh cadence does.** Refused. The stance schedule
  selects two knowledge bases and watches only one.
- **Keep an earlier fire, as the other event cadences do.** Refused. Many edits made one after another would start
  one run each. The quiet period collects them into one.
- **List every page an index wrote, the embedding backfill included.** Refused. A page the backfill widened in
  did not change, and a full index would list every page.
