---
name: orchestrating-a-run
description: >-
  Run a workflow honestly: read the workflow before running it, say which steps it declared and what they were told to do, report what a run cost, leave a waiting gate to the person, and when a run names you as its gate, decide on what you read in full, never on a guess.
---

# Orchestrating a run

There is one way to make work happen here: hand a validated graph to the machinery. Even one step
is a one-node workflow (a step you declared, checked, and ran) which costs a few extra lines of
YAML and buys the three things an ad-hoc call cannot: it was checked before it ran, the person can
read what it will do, and it is still there afterwards to re-run or save.

## `run_workflow`: a plan the machinery executes

Starts a **validated graph** running in the background and answers immediately. Independent
steps run in parallel because nothing wires them together. A step consuming two others waits for
both. Where the graph has a gate, the run waits there and asks the person, in the interface,
before anything irreversible happens. When the run settles (finished, failed, stopped, or
paused) you get one message in this session saying how. Do not wait for it or poll in a loop:
say the run is going and end your turn. `check_runs` answers "how is it going?" when asked.

```
run_workflow(workflow_id="wf.orient-then-audit-a-contested-claim",
             arguments={"entity": "ScaNN", "question": "Is the 40% latency claim replicated?"})
```

Reach for this when the work has shape (several steps, some of which can happen at once, and a
decision a human must own). You are not the orchestrator in this mode: you hand over a plan, and
the validated graph runs it. That is deliberate. A plan that exists as data can be read before it
runs, checked before it runs, and reused afterwards. A plan that exists only as your intentions
cannot be any of those things.

## Talking to a waiting worker

`message_worker(node_id, message)` sends a side message to the worker running at `node_id` in
this session's own live run -- only while the run is waiting at a gate, never while it is
executing (nothing else guards the step's own conversation against being written by both at
once). The worker sees the message joined to its own history, exactly as its next rerun would.
Any feedback already queued for that rerun is held aside for the length of this call and put
back afterward, so an ad hoc message never steals it.

## Scheduling a workflow

`schedule_workflow(workflow_id, cadence, ...)` creates a standing schedule that fires
`workflow_id` on its own from now on. `update_schedule`/`delete_schedule`/`run_schedule_now`
change, remove, or immediately fire one, and `schedules` lists every standing schedule -- call it
before creating a new one, to see whether one already covers the same workflow.

- `cadence`: `every <N><s, m, h, or d>`, a 5-field cron expression, `on refresh` (fires once after each
  refresh of the knowledge base the arguments select that brought in new sources), or `after <workflow name>`
  (fires once after a run of that workflow over the same knowledge base ends succeeded or partial). The last two
  never fire by the clock, keep their arm through an overlap or a blackout whatever `catch_up` says, and rest
  again after the fire. A schedule cannot fire after its own workflow.
- `gate_mode`: `wait` opens a gate decided by a person (the default). `orchestrate` hands it to
  this schedule's own orchestrator instead. `auto` passes every gate with nobody asked, only on
  the person's explicit instruction. `gate_policy` (`wait`, or `fail_after, <seconds>`) governs
  how long a `wait` gate holds before failing the run.
- `capacity_key`/`capacity_limit`: name a run budget shared across every schedule naming the same
  key, and how many of them may run at once.
- `blackout`: one or more daily quiet hours (`HH:MM-HH:MM`, comma separated) this schedule will
  not fire inside.
- `catch_up`: whether a fire held by an overlap or a blackout window fires once immediately once
  it clears (`true`), or waits for the cadence's next normal turn (`false`).

## Rules that keep this honest

- **Never run something you have not read.** `fetch_workflow` first if it came from the
  library, so you can tell the person what is about to happen, including where they will be asked.
- **A waiting gate means a human decides, not you.** When a run pauses, say plainly what is waiting and
  what rejecting will do (it reruns from the gate's `rejected_to` step, not from the beginning).
  Do not guess their answer and do not nudge. A run under `gate_mode: orchestrate`, or a declared
  orchestrator gate, is the one case where the gate is yours, and the section below is how you hold it.
- **Say what a run cost.** Report which steps ran, what came back, and where the output landed,
  file paths included. Work you cannot point at is work nobody can check.
- **A refusal is information.** If a saved workflow will not run here, it is naming a step this
  process does not have, and this process registers none, so an old workflow written against
  someone's registry of steps cannot run as written. Say which step, and offer to declare an equivalent from
  the retrieval tools this run does have. Do not substitute a lookalike and hope.
- **Every step here was written by a workflow, so always read them out.** Steps are declared by the
  workflow: built and typed before the run starts, briefed by text the workflow carries rather than
  by code anybody reviewed. When you read a workflow out before running it, say what each step was told
  to do and which tools it holds. The person is about to authorise exactly that, and it is theirs to
  know before the run, not after.

## When the gate is yours

A gate turn opens with the charge, the gate id, and what is under review, in full. You are asked
for one thing, `approve` or `reject` with feedback, and the run acts on it at once: an approval
releases the value to the next step (files get written, people get notified), a rejection reruns
the gate's `rejected_to` step with your feedback in front of it. Hold it like this.

- **Decide on what is in front of you.** Read the whole value, then judge it against the charge. If
  the charge names something to check in the base (an ADR that already decides this, a page that
  already exists), one search or one fetch settles it. That is the whole investigation.
- **Approve nothing you did not read in full.** If you could not read it, reject it and say exactly
  what you could not read. A rejection costs one rerun. An approval with a caveat costs whatever the
  next step does with the value, and nobody reads the caveat.
- **The budget is not a vote.** A gate turn is a few requests: read, check one thing, answer. When
  the budget is nearly gone, answer with what you know, reject with the reason or name what you could
  not verify. Running out never means approve.
- **Only the verdict changes the run.** Do not `run_workflow`, `message_worker`, or touch a schedule
  from inside a gate turn. The workflow already has a branch for each answer you can give.
