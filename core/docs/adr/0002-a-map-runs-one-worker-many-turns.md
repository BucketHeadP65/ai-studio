---
id: core-adr-0002-a-map-runs-one-worker-many-turns
aliases: ["core-adr-0002-a-map-runs-one-worker-many-turns"]
type: adr
app: core
status: accepted
tags: []
title: "0002 — A map runs one worker through many turns on one conversation"
---

# 0002 — A map runs one worker through many turns on one conversation

## Status

Accepted, 2026-08-28.

## Context

A workflow map used to chain per-item work as separate steps (`work`, then `then`, with `then_each` and `then_inputs` wiring).
Every step got a fresh worker with a fresh context, so a three-step chain over 40 sources ran 81 workers, re-read each source up
to 4.7 times, and paid for the same tokens repeatedly: the measured baseline run held 2.94M input tokens at 30% cache reads,
with the last step costing 78x the first for reading the same material.

## Decision

`MapNode` carries `turns`, an ordered list of registered work names. One worker per item takes one turn per entry on one
conversation: the system prompt and the tool surface (the union of every turn's tools, and the union of every turn's output
tools) are fixed before turn one and never change, each turn arrives as the next user message, and later turns open on the
messages earlier turns already paid for, so the provider's prompt cache carries the shared prefix. A turn's answer is validated
against that turn's own output type inside the conversation; an answer in another turn's shape is sent back for correction
rather than failing the item.

The old fields are removed, not deprecated: `extra="forbid"` refuses them at parse time, and a stored document migrates once,
by hand, together with the process restart (the old build cannot read a migrated store and the new build cannot read an
unmigrated one).

Consequences the grammar and compiler commit to:

- The map's own output is the LAST turn's list, one row per item. Every turn of a multi-turn map is also observed and journaled
  under its own dotted id, `<node id>.<work>`, turn one included, so a wire may name an earlier turn (`FromNode.turn`) and a
  timeline can fold a turn's events into the map's lane. A single-turn map keeps the plain node id and publishes no dotted key.
- Every leaf call runs under a published `StepIdentity` (node, item key, `TurnFacts`), carried across process boundaries by the
  Temporal adapter.
- A map aborts after a run of consecutive same-class item failures (`abort_after_repeats`, default 3): identical failures back
  to back are a broken system, not bad items, and `on_error: skip` must not silently skip a whole corpus. The measured case
  (2026-08-26, `wf.research-digest.v6`): out of provider credit, a run skipped 33 of 40 items on HTTP 402, reported
  `completed`, spent 13.8M input tokens, and connected no digest to the base because every later turn was chained behind
  the skipped ones.
- A multi-turn map replays from its journal only when the memo holds its own key AND every turn's dotted key; anything partial
  discards the map's stale entries and reruns it whole, because a turn's conversation is not durable on its own.
- Migrating a stored spec changes its canonical JSON and so its content address (`spec_identity`); trust history filed under
  the old address stays in the ledger but no longer resolves for that artifact.

## Alternatives considered

Keeping per-turn tool surfaces (only that turn's tools mounted) was rejected: changing the tool block mid-conversation breaks
the cached prefix at the tools segment, which is most of what this design buys. Cross-run per-item resumption and compaction
inside a turn list were excluded as out of scope.
