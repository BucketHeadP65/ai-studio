---
title: Check drafted ADRs
summary: Reads every drafted ADR whole and answers approve, reject with the reason, or cannot judge, for the owner.
version: 1
step:
  name: check_adrs
  instructions: |
    You are handed drafted ADRs. Read every draft whole before answering. Reject when a draft restates its design without
    naming a decision, when its sections are not Context, Decision, Consequences with an optional Options considered, or
    when the base already has an accepted ADR deciding the same thing. Search the base by the draft's title and by its
    design_id, and read the ADR you find. Approve the rest as proposed ADRs for the owner. Answer cannot_judge when what
    you were handed is empty, cut, or not ADR drafts, and say what you saw. Your feedback names each draft by slug and
    says what to change.
  inputs:
    drafts: {like: draft_adr, many: true}
    collection: {json_schema: {type: string}}
  output:
    json_schema:
      type: object
      title: Verdict
      properties:
        outcome: {enum: [approve, reject, cannot_judge]}
        feedback: {type: string}
      required: [outcome, feedback]
  tools: [search, fetch, list_pages]
  tier: orchestrator
  budget: {requests: 8, tool_calls: 12}
---

Use as the judge after a map of draft_adr, with collection wired from the workflow parameter. Do not use to edit a draft,
it only answers.
