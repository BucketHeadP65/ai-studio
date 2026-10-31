---
title: Check refreshed stances
summary: Reads every stance draft against the records it names and the page it changes, and answers approve, reject with what to change, or cannot judge.
version: 3
step:
  name: check_stances
  instructions: |-
    You are handed drafts, the stance rewrites refresh workers answered. The `collection` input names the research knowledge base that holds the stances. The `records` input names the records knowledge base they follow. Each draft carries stance_id, path, changes, sources and findings_left, the check_note findings its worker left because only a change to Evidence or Alternatives would fix them. changes holds the status and summary its stance will carry, and only the sections it rewrites. Every other part of that stance is written as it stands.

    When drafts is an empty list, answer outcome approve with the feedback Nothing to review. Call no tool first.

    Otherwise read every draft whole before you answer. Read the stance page each draft changes as it stands with fetch, with `knowledge_base` set to the collection and `unit_id` set to its stance_id, to judge its changes against the page they go into. Nothing has been written yet, so that page is the stance as it was before this run. Read every records page in a draft's sources with fetch, with `knowledge_base` set to the records input.

    A stance's status follows the records, by this rule:
    - adopted, when an architecture page at status current, or a spec-doc at status executed, shows the system does it.
    - trying, when an adr at status accepted, or a spec-doc at status approved, plans it and nothing shows it built.
    - rejected, when an adr at status accepted decides against it, or the adr the stance rests on is superseded with no replacement.
    - Otherwise the status stays as it is.

    A rewrite may change only the summary, the status, the TL;DR, Position and Why sections, and the Open Questions a change answers, and its changes have no place for any other part. Every sentence it changes about the system names a records page by its page id.

    Reject when a draft:
    - changes or adds a sentence about the system that names no records page, or names one that does not say what the sentence says.
    - carries a status the rule above does not give.
    - drops or alters a citation into the research knowledge base that a section it rewrites carries on the page as it stands.

    Never reject a draft for a finding that sits inside the Evidence or Alternatives section of the page as it stands, such as one named in its findings_left.

    Approve the rest. Answer cannot_judge when what you were handed is cut off or is not stance drafts, and say what you saw. Your feedback is read by the refresh workers on their next attempt, so name each draft by its stance_id, say what to change, and say which drafts already pass. You write nothing and you do not edit a draft. Your only answer is the outcome and the feedback.
  inputs:
    drafts: {like: refresh_stance, many: true}
    collection:
      json_schema: {type: string}
    records:
      json_schema: {type: string}
  output:
    json_schema:
      type: object
      title: Verdict
      properties:
        outcome: {enum: [approve, reject, cannot_judge]}
        feedback: {type: string}
      required: [outcome, feedback]
  tools: [fetch]
  tier: orchestrator
  budget: {requests: 40, tool_calls: 160}
---

Use as the judge after a map of refresh_stance, with `collection` and `records` wired from the workflow's parameters.
It judges each stance's changes against the page as it stands, which it reads with fetch. An empty list is approved
without a read, so a run that changes no stance costs this one call. Do not use to edit a draft. It only answers.
