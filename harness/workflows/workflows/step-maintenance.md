---
title: Keep consumers current with a new reusable step version
description: >-
  Checks a new reusable step version against every workflow holding a copy of the step, adopts it on live trial where
  a judge approves the plan, declines or flags the rest, tells the owner, and drafts a repository change for a step its
  consumers split over.
uses: [st.plan_changes.v1, st.judge_plan.v1, st.draft_repository_change.v1, st.judge_repository_change.v1]
presentation:
  purpose: >-
    Keep every workflow built on a reusable step current when the step gets a new version, without a person
    re-saving each one. A consumer adopts the new version only where it still serves what the consumer is for.
  produces: >-
    Each adopted consumer's next version, on live trial, its next real runs reverting it when they fail or do worse.
    A decline for each consumer that keeps its copy, a maintenance flag for each one the owner decides, one notice of
    what changed and why, and a repository change for a coding agent when the consumers split.
  owner_participation: >-
    Nothing during a run. A judge, not you, approves the plan before anything lands. You decide the flagged consumers
    and the repository changes afterwards, from Home or with harness flags.
  not_for: >-
    Not for changing a reusable step itself, which reaches the library only through an import of its file. Not for a
    workflow that holds no copy of a reusable step.
  inputs:
    - {key: changed_step_ids, label: Changed step versions, control: text}
  result_fields:
    - {label: What landed, path: values.land_changes}
    - {label: Repository changes handed over, path: values.hand_over_repository_changes}
workflow:
  name: step-maintenance
  version: 1
  description: >-
    List each changed step's behind or diverged consumers, check their contracts and run the step's cases, plan
    adopt, keep or flag per consumer under a judge, land the plan, report it, and draft and judge a repository change
    per step whose consumers split.
  parameters:
    changed_step_ids:
      description: The changed reusable step versions, by id (st.<name>.v<version>), comma-separated.
      json_schema: {type: string, minLength: 1}
      required: true
  inline:
    - name: pass_changed_ids
      source: changed_step_ids
      inputs:
        decisions: {like: plan_decisions}
        changed_step_ids: {json_schema: {type: string, minLength: 1}}
      output: {json_schema: {type: string}}
  result:
    summary: Each consumer's adoption on live trial, decline or flag, and the repository changes handed over.
    primary_work: land_changes
    required_gates:
      - {gate_id: judge_plan, accepted_sources: [judge]}
  nodes:
    - kind: run
      id: list_consumers
      work: list_consumers
      inputs: {changed_step_ids: {parameter: changed_step_ids}}
    - kind: run
      id: check_contracts
      work: check_contracts
      inputs: {consumers: {node: list_consumers}}
    - kind: run
      id: run_step_cases
      work: run_step_cases
      inputs: {changed_step_ids: {parameter: changed_step_ids}}
    - kind: run
      id: prepare_plan
      work: prepare_plan
      inputs:
        consumers: {node: list_consumers}
        checks: {node: check_contracts}
        cases: {node: run_step_cases}
    - kind: run
      id: plan_changes
      work: plan_changes
      inputs:
        draft: {node: prepare_plan}
        consumers: {node: list_consumers}
    - kind: run
      id: pass_changed_ids
      work: pass_changed_ids
      inputs:
        decisions: {node: plan_changes}
        changed_step_ids: {parameter: changed_step_ids}
    - kind: run
      id: relist_consumers
      work: list_consumers
      inputs: {changed_step_ids: {node: pass_changed_ids}}
    - kind: run
      id: recheck_contracts
      work: check_contracts
      inputs: {consumers: {node: relist_consumers}}
    - kind: run
      id: rerun_step_cases
      work: run_step_cases
      inputs: {changed_step_ids: {node: pass_changed_ids}}
    - kind: judge
      id: judge_plan
      work: judge_plan
      reviews: {node: plan_changes}
      inputs:
        consumers: {node: relist_consumers}
        checks: {node: recheck_contracts}
        cases: {node: rerun_step_cases}
      approved_when: outcome
      rejected_to: plan_changes
      max_attempts: 3
    - kind: run
      id: land_changes
      work: land_changes
      inputs:
        plan: {node: prepare_plan}
        decisions: {node: judge_plan}
    - kind: run
      id: report_changes
      work: report_changes
      inputs: {landed: {node: land_changes}}
    - kind: run
      id: select_disagreements
      work: select_disagreements
      inputs:
        landed: {node: land_changes}
        consumers: {node: list_consumers}
    - kind: map
      id: draft_repository_changes
      turns: [draft_repository_change]
      over: {node: select_disagreements}
      each: change
      max_parallel: 2
      on_error: skip
    - kind: map
      id: judge_repository_changes
      turns: [judge_repository_change]
      over: {node: draft_repository_changes}
      each: draft
      inputs: {disagreements: {node: select_disagreements}}
      max_parallel: 2
      on_error: skip
    - kind: run
      id: hand_over_repository_changes
      work: hand_over_repository_changes
      inputs:
        report: {node: report_changes}
        disagreements: {node: select_disagreements}
        drafts: {node: draft_repository_changes}
        verdicts: {node: judge_repository_changes}
  output: {node: hand_over_repository_changes}
---

Use on the `on step change` schedule, which fires it with the reusable step versions saved during the quiet period, or
by hand with `changed_step_ids` naming the versions to check. Each named step is taken at its newest saved version.

The stages: `list_consumers` takes at most twenty consumer pairs per run and names the rest for the next run.
`check_contracts` and `run_step_cases` gather the evidence side by side, and `prepare_plan` copies it into one undecided
change per pair. The planner, `plan_changes`, decides adopt, keep or flag per pair on the orchestrator tier, checking
every adoption with `check_change`, and answers only its decisions, so it never retypes the evidence. Once the plan exists, `pass_changed_ids` hands the run's own `changed_step_ids` to
`relist_consumers`, `recheck_contracts` and `rerun_step_cases`, the same registered steps run again, and `judge_plan`
reviews the decisions against what they answer, marking each one pass or fail.
A rejection sends the plan back to the planner with that feedback and re-runs everything after the planner, so each
plan is judged on evidence made after it. The third rejection ends the run with nothing landed, which raises a
maintenance flag per changed step. The second pass reads the library and the evidence log again, and runs again the
cases of a step version whose first case report held an errored case, since such a report is not kept, one model call
per case. `land_changes` merges the approved decisions into the plan
`prepare_plan` drafted, a change no decision names writing nothing so the next run offers it again, then saves each adoption as the
consumer's next version on live trial, declines each kept copy and flags each flagged pair, landing every writable
change before it fails naming each refusal. Only then do `report_changes` and the repository
branch start: one drafter per step whose consumers split, one judge per draft, and the hand-over, which writes each
kept draft as a maintenance flag for a coding agent and answers for the run.

Do not use to change a reusable step: a step's new version comes from its file through an import, and this workflow
only brings that version to the workflows holding a copy.
