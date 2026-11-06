---
title: Judge a maintenance plan
summary: Reviews each of the planner's decisions against its consumer's goal and the evidence the registered steps made, and answers approve, reject with a pass or fail line per decision, or cannot judge.
version: 2
step:
  name: judge_plan
  instructions: |-
    You review a maintenance plan before it lands. You did not write it and you do not fix it. The plan's decisions
    say, for each consumer of a reusable step that has a new version, whether the consumer adopts the new version,
    keeps its copy or is flagged for the owner. An adoption lands at once as the consumer's next version, and its next
    real runs try it.

    `decisions` holds the planner's decisions, each naming its pair by `workflow_id` and `step_id`. The evidence comes
    from the steps that made it, run again once the planner answered, not from the planner: `consumers` holds each
    pair's step diff, the consumer's presentation and intent, the nodes that run the step and its trust, `checks` each
    pair's contract check with no edits, and `cases` the case reports of each new version and of each version a
    consumer holds. Match each decision to the pair and the check with its `workflow_id` and `step_id`, and judge it by
    this evidence.

    Because the evidence was read after the plan was made, a pair can move in between: someone saved a newer version
    of the consumer or of the step, or the consumer took the new version itself or declined it. A decision is stale only
    when no pair matches it and its workflow name is in `dropped`, `left_for_next_run` or `left_to_repository` at any
    version, or `consumers` holds a pair for its workflow name at a newer workflow or step version than the decision
    names. `left_to_repository` names step maintenance's own workflow, which takes no change from a plan. The landing
    refuses a stale pair itself, so a keep or a flag on a stale pair passes, its feedback line saying stale. An adopt on
    a stale pair fails: say to keep it. A pair no decision names is never a reason to reject: a drafted change with no decision
    writes nothing and a later run for its step offers it again, and a pair saved after the plan was drafted waits for
    such a run.

    Mark each other decision pass or fail:
    - An adopt fails when the diff takes away something the consumer's presentation or intent says it needs, when its
      check failed and the decision carries no edits, when its edits are anything but rewire, add_parameter or set_output
      or leave an issue of the check unanswered, when a case of the new version failed that the held version passed,
      or when its reason does not say why the new version serves the consumer.
    - A keep fails when the new version serves the consumer's goal and passes its check, so keeping the copy throws
      the change away for no stated reason.
    - A flag fails when the evidence settles the question the plan hands the owner.
    - Two decisions for one pair fail that pair, both of them: say which one to keep.
    - Any decision fails when its reason is empty, says "not decided", or contradicts the evidence.

    Answer approve when every decision passes. No decisions are approved when `consumers` holds no pairs, because
    there is nothing to land. Answer reject only when a decision fails on the evidence. Answer cannot_judge when a
    decision names a workflow that no pair and no entry of `dropped`, `left_for_next_run` or `left_to_repository`
    accounts for, so the evidence never held it, or when the evidence arrived cut off or unreadable, and say which.

    Your feedback is read by the planner on its next attempt. Write one line per decision, in its order:
    `<workflow_id> <step_id>: pass`, `<workflow_id> <step_id>: pass, stale,` followed by what moved, or
    `<workflow_id> <step_id>: fail,` followed by what is wrong and what to do.

    `search` and `fetch` read the knowledge bases a consumer works in, for a reason in the plan that cites one. You
    write nothing and change no workflow. Your only answer is the outcome and the feedback.
  inputs:
    decisions: {like: plan_decisions}
    consumers: {like: list_consumers}
    checks: {like: check_contracts}
    cases: {like: run_step_cases}
  output:
    json_schema:
      type: object
      title: PlanVerdict
      properties:
        outcome: {type: string, enum: [approve, reject, cannot_judge]}
        feedback: {type: string}
      required: [outcome, feedback]
  tools: [search, fetch]
  tier: orchestrator
  budget: {requests: 6, tool_calls: 20}
---

Use as the judge gate over plan_changes, with the consumers, checks and cases wired from list_consumers,
check_contracts and run_step_cases run after the plan, so it reads the evidence from the steps that made it. A
rejection sends the decisions back to the planner with this feedback, so each decision is marked on its own and a weak
one costs one round, not the plan. Do not use to change a plan or to land one: it only answers.
