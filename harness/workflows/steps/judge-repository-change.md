---
title: Judge a drafted repository change
summary: Reads one drafted change to a reusable step's files against the goal of every consumer on both sides and answers keep or drop with its reason.
version: 1
step:
  name: judge_repository_change
  instructions: |-
    You judge one drafted change to a reusable step's files before it is handed to a coding agent. You did not draft
    it and you do not fix it. Step maintenance landed a new version of the step, and its consumers split: `adopted`
    took the new version, `kept` kept their copy or wait for the owner. The draft proposes a new version with a
    parameter the consumers set differently, or a split into two steps, so that every consumer keeps what its goal
    needs.

    `draft` holds the step, its new version, each side's consumers with the planner's reason, the nodes that run the
    step, the diff from the consumer's copy and what the consumer is for, and the drafted change: `proposal`,
    `step_files` and `file_edits`.

    Answer keep when all of these hold: the proposal says what the change does and how each consumer on both sides
    keeps what its presentation and intent say it needs, the step files do what the proposal says, each step file is
    a step file whose version is above the version the draft's `step_id` names, and each consumer the change touches
    has its file edit. Answer drop when any of them fails, when the drafted fields are empty, or when the change would
    take from one side what it gives the other. A dropped draft costs nothing: the consumers stay as the landing left
    them. Answer undecided only when the draft arrived cut off or unreadable, and say so.

    Answer with `step` set to the draft's `step`, your `decision`, and a `reason` of one or two sentences the owner
    reads, naming the consumer or the file that decided it. `search` and `fetch` read the knowledge bases a consumer
    works in, for a proposal that cites one. You write nothing and change no file.
  inputs:
    draft: {like: select_disagreements, element: true}
  output: {like: repository_verdict}
  tools: [search, fetch]
  tier: orchestrator
  budget: {requests: 4, tool_calls: 12}
---

Use as the one turn of the map over the drafted repository changes in the step-maintenance workflow, one judge per
draft, a worker apart from the drafter. It keeps or drops each draft on its own with no redo round, and the hand-over
writes each kept draft as a maintenance flag. Do not use to fix a draft or to apply one.
