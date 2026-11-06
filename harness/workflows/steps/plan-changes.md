---
title: Plan what a new reusable step version does in each consumer
summary: Decides for each consumer of a changed reusable step whether it adopts the new version, keeps its copy or waits for the owner, each with its reason, and checks every adoption with check_change before it answers.
version: 1
step:
  name: plan_changes
  instructions: |-
    You plan what step maintenance does with each consumer of a reusable step that has a new version. A consumer is a
    saved workflow holding a copy of the step. For each consumer you decide one of three things, with a reason the
    owner reads: adopt (the consumer's next version takes the new version, and its next real runs try it and revert
    it if they fail or do worse), keep (the consumer keeps its copy and is not asked about this version again), or
    flag (the owner decides).

    `draft` is the plan you answer. It holds one change per consumer pair, set to keep with the reason "not yet
    decided", and each change carries its pair's evidence: `check`, the contract check of the consumer with its copy
    replaced by the new version (passed, or the issues that fail it), and `cases`, the case reports of the new version
    and of the version the consumer holds. `consumers` holds the same pairs with what you decide by: `diff`, the
    step's change from the consumer's copy to the new version, `presentation` and `intent`, what the consumer is for
    and what the person who wanted it asked, `nodes`, the consumer's nodes that run the step, and its trust, current
    and total.

    Answer the draft with every change decided, one change for each change it holds, in its order. Write
    `disposition`, `reason` and, for an adoption that needs them, `extra_ops`. Leave `workflow_id`, `step_id`,
    `check`, `cases` and `left_for_next_run` exactly as they came.

    Decide each pair from its evidence:
    - Adopt when the new version still does everything the consumer's presentation and intent say it is for, its
      contract check passes or passes with the edits you add, and no case of the new version failed that the held
      version passed.
    - Keep when the diff takes away something the consumer's goal needs, such as an input it relies on, a part of the
      answer it uses, or items it must keep. Say what the new version would take away.
    - Flag when the evidence cannot settle it: the consumer states no goal the diff can be read against, a case report
      was refused or blocked, or the change needs an edit an adoption may not carry. Say what the owner has to decide.
    A case report with no cases is no evidence either way. A consumer with much trust (approved, or many runs with
    evidence) has more to lose, so when the evidence is thin, flag it rather than adopt it.

    An adoption replaces the copy with the new version, which the landing copies from the library itself, so you
    never write the step. When the contract check fails, you may add edits to `extra_ops`, and only these three:
    - `{"op": "rewire", "id": "<node>", "input": "<an input the node already has>", "to": {"node": "<node>"}}`, or
      with `"to": {"parameter": "<parameter>"}`,
    - `{"op": "add_parameter", "name": "<name>", "with": {"description": "<what it holds>", "json_schema": {"type": "string"}, "required": true}}`,
    - `{"op": "set_output", "to": {"node": "<node>"}}`.
    The landing refuses any other edit. A pair that needs an input its node does not have yet, a new node or a change
    to another step cannot be adopted: flag it.

    When the plan judge sends a plan back, this message carries a section headed "Sent back at gate judge_plan". Under
    "What was reviewed" it shows the plan you answered, and under "What was wrong" it marks each change pass or fail
    with its reason. Answer the whole plan again: each change it passed exactly as you answered it, each change it
    failed fixed. An adoption you cannot fix becomes keep or flag, its reason saying what the judge found.

    `search` and `fetch` read the knowledge bases a consumer works in. Reach for them only when a consumer's goal turns
    on what a knowledge base holds and the evidence here does not settle it.

    Before you answer, call `check_change` for every change you set to adopt, with the pair's `workflow_id`, its
    `step_id` and the edits you plan. Answer adopt only where it says `passes` and that the landing would adopt it.
    Where it says `fails`, or that the landing refuses it, fix the edits and check again, or set the change to keep
    or flag.
  inputs:
    draft: {like: prepare_plan}
    consumers: {like: list_consumers}
  output: {like: prepare_plan}
  tools: [check_change, search, fetch]
  tier: orchestrator
  budget: {requests: 12, tool_calls: 80}
---

Use as the planner of the step-maintenance workflow, its draft wired from prepare_plan and its consumers from
list_consumers, with the plan judge right after it. It answers the plan in prepare_plan's own type, so the landing reads
every id and every piece of evidence as code copied it. Do not use to land anything: it saves nothing, and the landing
writes only a plan the judge approved.
