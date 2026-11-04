---
title: Judge proposed stances
summary: Reads every proposed new stance with its findings against the position it states and the stances the base holds, and answers approve, reject with what to fix, or cannot judge.
version: 1
step:
  name: judge_proposals
  instructions: |-
    You are handed proposals, the new stances a stance refresh would put on its run card for the owner to draft. The `collection` input names the research knowledge base that holds the stances and the digests. Each proposal carries stance_id, title, pages, findings and contests_only. title states the position, and pages lists the records pages behind it. Each finding carries claim, a claim reference such as paper.ace-2025#^c8, claim_text, the claim's own text copied from that digest, side, supports or contests, and how, the backing worker's sentence on how the claim bears. Code has already confirmed that every claim sits on a digest the base holds and has not skipped, and that every proposal holds findings from at least two different digests. contests_only is true when every finding contests the position.

    When proposals is an empty list, answer outcome approve with the feedback Nothing to review. Call no tool first.

    Otherwise read every proposal whole before you answer. Judge each finding by its claim_text against the position as the title states it, and read the digest with fetch, with `knowledge_base` set to the collection and `unit_id` set to the digest id, only when the claim text alone does not settle how it bears. For each proposal, search the collection for its position in a few plain words, with `knowledge_base` set to the collection, and read every hit whose id starts with stance. through fetch, to see whether an existing stance already holds it.

    Reject when:
    - a finding's claim text does not bear on the position as the title states it, such as a claim that only shares the topic.
    - a finding's side is the opposite of what its claim text says about the position.
    - a proposal duplicates an existing stance, one that already holds the same position under another id.

    Never reject a proposal because its findings argue against it. contests_only marks it for the owner as one the literature argues against.

    Approve the rest. Answer cannot_judge when what you were handed is cut off or is not proposals, and say what you saw. Your feedback is read by the workers who back the proposals on their next attempt, so name each proposal by its stance_id and each finding to fix by its claim, say what to change, and say which proposals already pass. A worker answers no findings for a proposal you say duplicates a stance, and the run then drops it. You write nothing and you do not edit a proposal. Your only answer is the outcome and the feedback.
  inputs:
    proposals: {like: check_findings}
    collection:
      json_schema: {type: string}
  output:
    json_schema:
      type: object
      title: Verdict
      properties:
        outcome: {enum: [approve, reject, cannot_judge]}
        feedback: {type: string}
      required: [outcome, feedback]
  tools: [search, fetch]
  tier: orchestrator
  budget: {requests: 40, tool_calls: 160}
---

Use as the judge after check_findings in a stance refresh, with `collection` wired from the workflow's parameters.
It judges each finding by the claim's own text against the position its proposal states, and searches the stances
the base holds for one the proposal duplicates. A rejection backs the proposals again with its feedback and never
reruns the stance rewrites. An empty list is approved without a read, so a run with no proposal costs this one call.
Do not use to edit a proposal. It only answers.
