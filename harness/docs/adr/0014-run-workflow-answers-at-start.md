---
id: harness-adr-0014-run-workflow-answers-at-start
aliases: ["harness-adr-0014-run-workflow-answers-at-start"]
type: adr
app: harness
status: accepted
tags: []
title: "run_workflow answers at start; the settlement delivers the outcome as its own turn"
---

# 14. run_workflow answers at start; the settlement delivers the outcome as its own turn

Date: 2026-08-31

## Status

Accepted

## Context

The chat door's `run_workflow` awaited its run to the end: the tool call stayed open for the
whole graph — minutes, or hours across a parked gate — and the orchestrator's turn was blocked
inside it. Three costs, all observed live:

1. **The session went unresponsive.** A message sent while a run was going rode the steer inbox,
   which only drains before a model request — and a turn blocked inside one tool call makes no
   model requests. The person watched a spinner for as long as the run took.
2. **The turn's cancellation was entangled with the run's.** A dropped stream or an interrupted
   chat cancelled into the tool, needing a shield and three cancellation arms to keep the run
   alive (ADR 0005).
3. **Orchestrator gates could not decide from a chat-started run.** The decider takes the
   session's turn lock, which the blocked turn held for the run's whole life — so `orchestrate`
   had to be refused on live chat runs.

The book-closing half was already turn-independent: `settle()` runs as its own shielded task,
landing the attestation, the archive, and the journal close however the turn ends. And
`features/running/feedback.py` already fires headless turns of the session's own agent (rejection
investigation, orchestrator gate decisions).

## Decision

`run_workflow` answers the moment the run is going, with a started report (steps, gates on the
way, "you will get one message when it settles"). The settlement task is exposed as
`session.active.settlement`; the button path nulls it at launch so a chat predecessor's book is
never read as a button run's. When the settlement answers, one headless turn of the session's own
agent (`feedback.settled_turn`) carries the outcome sentence — finished with its gate account,
failed, stopped, or paused — back into the conversation. Unlike a rejection turn, the settled
turn waits out a busy session rather than skipping: the outcome is owed to the conversation.

A failed run is prompt text now, not a `ModelRetry` — there is no open tool call left to retry
into. The three cancellation arms (pause, stop, outside cancellation) stay, read at settle time.

## Consequences

- Chat stays responsive during a run: a send finds no turn in flight, gets an ordinary turn, and
  the orchestrator can talk, `check_runs`, or `stop_run` mid-run.
- The turn lock frees when the starting turn ends, so orchestrator-decided gates no longer
  deadlock against a chat-started run. The chat UI still refuses switching a live run into
  `orchestrate`; lifting that is now possible and deliberately separate.
- The settled turn lands in the transcript like a rejection turn does: an open tab sees it on its
  next transcript read, not as a live stream.
- Anything needing to observe the closed book awaits `session.active.settlement` (tests, chiefly);
  the ending itself never depends on a listener.
