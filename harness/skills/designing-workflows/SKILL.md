---
name: designing-workflows
description: Use when designing, reviewing, or changing a workflow graph, a step file, or how a run fans out. Covers who does the work, what a worker carries between steps, what breaks the prompt cache, where validation sits, how nodes and wires are named, and what a step does when a run is failing.
---

# Designing workflows, the shape of a run

## Iron Law

> **A worker is a conversation, not a function call. Steps that work on the same thing share one conversation.
> Steps that work on different things get their own.**
> A fresh worker per step re-reads what the last worker already read, into a context the cache has never seen.

## 1. One thing, one worker, several turns

A map over N items with three steps is **N workers taking three turns each**, never 3N workers.

The steps become turns in one conversation, because the second and third turn already hold what the first read.
The worker keeps its own item's context and adds to it, so the source is read once and the model reasons about
its second step already knowing the first.

```
WRONG                                     RIGHT
map answers  -> worker A (reads letter)   map letters -> worker A turn 1 (reads the letter, drafts the answer)
map promises -> worker B (reads again)                           turn 2 (lists the promises made, letter in context)
map filing   -> worker C (reads again)                           turn 3 (files the copy, letter still in context)
3 contexts, letter read 3x, 0 cache hits  1 context, letter read once, turns 2 and 3 hit the cache
```

Forty letters to answer, three things to do with each. One clerk per letter reads it once and does all three. A clerk per
step per letter reads every letter three times, and the third clerk never knows what the first one found.

**When steps genuinely get their own worker:** they act on different things (one per letter versus one over the whole
mailbox), they must run under different tool grants for a safety reason, or one is expensive enough that a
failure should not carry the other's context into a retry.

## 2. What breaks the prompt cache

A provider caches on an exact prefix match from the first token. The prefix is **system prompt, then tools, then
history in order**. Change anything before the last turn and every later token is billed uncached.

Rules that follow, and they are not negotiable if a worker takes more than one turn:

- **Mount every tool the whole sequence needs, up front.** Adding a tool at turn 2 rewrites the tool block, which
  sits before the history, so the whole conversation re-prices. Grant turn 3's tools at turn 1 even though turn 1
  will not call them.
- **One system prompt for the whole sequence.** It says who the worker is and what it is working on, never what
  this particular turn should do.
- **A step's instructions are a user turn.** "Now list the promises this answer makes" is a message, not a
  new system prompt. This is what lets the role stay fixed while the instruction changes.
- **Never reorder or edit earlier turns.** Compaction, summarisation, or dropping a tool result invalidates
  everything after the edit. Append only.
- **Put the volatile part last.** Anything that changes per turn (a timestamp, a counter, feedback) goes at the
  end of the newest message, never near the top.

**How to check rather than assume:** read `cache_read_tokens` against `input_tokens` on the spend rows, turn by
turn. A multi-turn worker whose later turns show near-zero cache reads has a broken prefix, and the cause is almost
always a tool grant or an instruction that changed between turns. The run also warns once when a single turn's
request reads back less than half of its own established prefix, which catches a collapse inside one turn and
nothing between turns, so the spend rows stay the check that spans them.

## 3. Parallelism is about dependencies, not speed

Two nodes may run concurrently when neither reads the other's output. That is the only question.

- **Independent, same input** (two things to do with one letter): NOT two maps. One map, one worker, two turns.
  Two maps over the same input is the duplicated-read defect above, wearing the costume of parallelism.
- **Dependent, per item** (filing needs this letter's answer): list it as a later entry in the map's `turns`, so
  item 3 does not wait on item 1. This is right, and it is what `turns` is for.
- **Dependent, across all items** (a report over every answer): its own `run` node after the map. It genuinely
  needs the barrier.

`max_parallel` is a concurrency cap, not a design. Two maps at `max_parallel: 4` put 8 workers in flight and 8
conversations on the provider at once.

A map node names its per-item sequence once, as an ordered list of work names:

```yaml
- kind: map
  id: letters
  turns: [draft_answer, list_promises, file_copy]
  over: {node: unanswered}
  each: letter
  inputs: {mailbox: {parameter: mailbox}}
  max_parallel: 4
  on_error: fail
```

One worker per item takes one turn per entry, in order, on one conversation kept for the whole run and keyed by the
item, never by the step name: a map over forty letters keeps forty conversations, each opening its next turn on the
messages its own previous turn left behind. Every turn's prompt names the shape it answers with, and an answer in
another turn's shape is sent back for correction inside the same conversation. The map's collected output is
`list[<last turn's output>]`. Each turn's own output stays observable too, in the run's values under
`<node id>.<work name>`, so a later node can wire from one turn by name instead of only the last.

A run or map node, or a step file it names, may say `tier: orchestrator | worker | helper`. The node's model tier
wins when both name one, and naming neither runs on the worker model tier.

## 4. What a step passes to the next

Pass the **smallest identity** the next step can act on, not the payload.

The filing step takes `{letter_id, status}`, then fetches the letter itself. That is right: the id is stable, small,
and the fetch is cheap and current. Threading the whole letter through the graph would put it in the run's values
twice and stale on the first rewrite.

The exception is what the next turn already holds. Inside one conversation, pass nothing: it is in the context.

## 5. Failure is a design decision, and `skip` is usually the wrong one

`on_error: skip` means "this item was bad, the others are fine". It is right for a malformed source and wrong for
everything else.

A mail room whose postage meter runs dry at ten in the morning does not set the rest of the day's letters aside one by
one and report the day delivered. The first refused stamp says every stamp after it will be refused too. It stops, and it
says why.

- **A provider or auth error is not a bad item.** Out of credit, rate limited, model unavailable: the next item
  will fail the same way. Abort the run.
- **Repeated identical failures mean stop.** A map's `abort_after_repeats` counts consecutive items that fail
  with the same error class, resets on any item that succeeds, and ends the run as failed once it reaches the
  count, naming the error. The default is 3. Set it to `None` only for a map that genuinely wants unbounded
  skipping.
- **A run that skipped most of its work did not complete.** `abort_after_repeats` catches a systemic fault
  repeating identically. It does not catch a majority of items failing for varied reasons. An outcome that says
  `completed` over a majority of skips still teaches the owner to trust a run that failed.
- **A per-worker request limit is a backstop, not a budget.** A limit of 24 model calls for one document is a
  design that expects a loop. Ask why a worker needs 24 turns before raising it.

## 6. Validation sits where the fix is

Three checks guard a flow that writes into a governed destination, and each has one right step:

| Check | Wrong step | Right step |
|---|---|---|
| **Shape** (the fields and their types) | the judge | the step's declared output schema |
| **Grammar** (what the destination accepts: ids, required fields, links that resolve) | the landing program (no model there to react), or the judge (a model call spent on what a function knows) | the drafting step, as a checking tool run before answering |
| **Judgment** (is it true, is it good) | the drafting step grading itself | the judge |

Give the drafter the destination's own check as a tool and make its brief end on it: check, fix every finding, answer
only work that passes. The step that made the mistake is the one step that can fix it in place, the check that finds
nothing costs no model call, and the judge then spends its whole budget on judgment.

A deterministic landing step writes what was already validated, and still catches each item's refusal instead of dying
on the first: land every writable item, then fail the run naming each refusal. Nothing is silently dropped, and work
refused at landing shows up again on the next pass's pending list.

## 7. Cost is a property of the graph, and you can read it

Before landing a workflow, work out what one run costs at its widest: items times turns per item times tokens per
turn. Then check it against the spend rows after the first real run.

- A step that costs many times its sibling for the same input is doing redundant work. Find the duplicated read.
- Every unattended run needs a spend brake. A workflow that runs on a schedule with no ceiling is an
  unbounded bill waiting for a bad day.
- A workflow that queues its own future work needs a per-pass ceiling, not just a per-item one. N items each
  obeying a per-item cap can still add far more than the pass removed, and the queue grows every run.

## 8. Names

- A node id names the job the step does (`file_copies`), never its aftermath (`filed`).
- An input name names the value flowing in (`answers`, `mailbox`) and never repeats a node id.
- The node id, the work it names, and the wires around it each add a word the others do not.

## 9. Checklist before landing a workflow

- [ ] Does any two nodes read the same input and do different things with it? Fold them into one map's `turns`.
- [ ] Does every multi-turn worker mount all its tools at turn 1?
- [ ] Is each step's instruction a user turn, with one system prompt for the sequence?
- [ ] Does each `on_error` distinguish a bad item from a broken system?
- [ ] Is `abort_after_repeats` left at its default of 3, or set deliberately if the map needs something else?
- [ ] Does the run's outcome tell the truth when most items were skipped?
- [ ] Is there a spend ceiling on the run, not only on a step?
- [ ] If the workflow queues future work, is there a per-pass ceiling?
- [ ] Have you costed one run at its widest, and checked it against the spend rows afterwards?
- [ ] Do the node ids read as jobs, and does no input name repeat a node id?
- [ ] Does each check run at its own step: shape at the output schema, grammar at the drafter's checking tool,
      judgment at the judge, and does the landing step catch refusals per item?
