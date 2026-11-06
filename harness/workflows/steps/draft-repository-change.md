---
title: Draft a repository change for a step its consumers split over
summary: Drafts a new version of a reusable step with a parameter its consumers set differently, or a split into two steps, as step files and file edits for a coding agent, checked with check_change before it answers.
version: 1
step:
  name: draft_repository_change
  instructions: |-
    You draft a change to a reusable step's files for a coding agent to apply. Step maintenance just landed a new
    version of the step, and its consumers split: some adopted the new version, others kept their copy or wait for the
    owner, because the new version does not serve every consumer's goal. A reusable step reaches the library only from
    the repository's files, so nothing you draft lands by itself. A judge reads your draft next, and a draft it keeps
    is handed to a coding agent.

    `change` names the step and its new version, holds that version whole in `step_yaml` (the mapping under `step` in
    a step file), and holds both sides: `adopted`, the consumers that took the new version, and `kept`, those that
    kept their copy or wait for the owner. Each consumer carries the planner's reason, the nodes that run the step, the
    diff from its copy to the new version, and what it is for (its presentation and intent).

    Draft whichever of two changes lets every consumer on both sides keep what its goal needs:
    - a generalization: a new version of the step with a parameter each consumer sets to what its goal needs, or
    - a split: two steps under their own names, each serving one side.

    Answer `change` with `step`, `step_id`, `step_yaml`, `adopted` and `kept` exactly as they came, and fill:
    - `proposal`: what the change does, what each consumer sets or which step it runs, and why each consumer's goal
      holds, in a few sentences.
    - `step_files`: each new or changed step file, `path` set to `steps/<step name with hyphens>.md` and `text` to the
      whole file: frontmatter holding `title`, `summary`, `version` and `step`, then a paragraph saying when to use the
      step. Start each `step` from `step_yaml` and change only what the proposal says. A changed step's version is one
      above the version `step_id` names, and a new step's is 1.
    - `file_edits`: one entry per consumer whose workflow must change (an input wired to the new parameter, a node
      running the new step), `path` set to `workflows/<workflow name>.md` and `text` to the lines that change, as they
      should read.

    `search` and `fetch` read the knowledge bases a consumer works in, for a goal that turns on what one holds.

    Check the draft before you answer. For each consumer on both sides, call `check_change` with its `workflow_id`,
    the change's `step_id` and the edits that apply your draft to it: `{"op": "replace_inline_step", "name": "<step>",
    "with": <the mapping under step in your step file>}` for a changed step, `{"op": "add_inline_step", "with": <the
    new step's mapping>}` for a split, `{"op": "replace_node", "id": "<node>", "with": <the whole node as it should
    read>}` for a node that runs another step or takes another input, and `add_parameter` for a parameter the consumer
    adds. Read only the contract check in its answer: a draft is never adopted, so its refusal as an adoption does not
    apply. Fix each issue the check names and check again, and answer only a draft whose every check passes. Where it
    says `cannot check`, the consumer or the step version could not be read: check that you passed the ids as `change`
    names them, and if you did, say in the proposal which consumer could not be checked.
  inputs:
    change: {like: select_disagreements, element: true}
  output: {like: select_disagreements, element: true}
  tools: [check_change, search, fetch]
  tier: orchestrator
  budget: {requests: 10, tool_calls: 40}
---

Use as the one turn of the map over select_disagreements in the step-maintenance workflow, one worker per step whose
consumers split, after the landing. It answers one element of select_disagreements' own type, its evidence kept as
code filled it. Do not use to change a consumer or a step in the library: a reusable step reaches the library only
through an import of the repository's files, so its draft waits for a judge and then a coding agent.
