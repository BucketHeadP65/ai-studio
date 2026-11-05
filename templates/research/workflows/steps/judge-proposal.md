---
title: Judge one stance proposal
summary: Whether each finding of one backed stance proposal bears on its position, with one sentence on why, and the existing stance it repeats, if any, under the proposal's own id, title and pages.
version: 2
step:
  name: judge_proposal
  instructions: |-
    You hold one backed stance proposal. The `collection` input names the research knowledge base, which holds the stances and the digests. The backing carries stance_id, title, pages, findings and contests_only. title states the position the proposal takes, and pages lists the records pages behind it. Each finding carries claim, a claim reference such as paper.ace-2025#^c8, claim_text, the claim's own text copied from its digest, side, supports or contests, and how, the backing worker's sentence on how it bears. Code has already confirmed that every claim sits on a digest the base holds and has not skipped. Other judges read the other proposals in parallel, so judge only your own.

    For each finding, decide whether its claim bears on the position as the title states it. bears is true when the claim's text makes the position more or less likely to hold, in the direction its side says. It is false when the claim only shares the topic, bears on a neighbouring question, or points the other way from its side. Judge by claim_text. Read the digest with read_page, with `knowledge_base` set to the collection and `page_id` set to the digest id, the part of the claim before the #, only when the claim text alone does not settle it. why is one sentence on why the claim bears or does not.

    Then search the collection for the position in a few plain words, with `knowledge_base` set to the collection, and read each hit whose id starts with stance. with read_page. duplicates is the id of an existing stance that already holds the same position. Leave it null when no stance does. A stance on a neighbouring or narrower position is not a duplicate.

    The run keeps only the findings you judge to bear, and drops a proposal left with findings from fewer than two digests, or one you say duplicates a stance, naming your reasons. A weak finding is dropped, never sent back to be repaired.

    The tools run inside a sandbox: call them directly with plain arguments, and `import asyncio` first if you gather calls in parallel (the import is required, the module is not preloaded). You write nothing. Answer stance_id, title and pages copied exactly from the backing, so your answer is matched to the proposal you judged, one entry in findings for every finding of the backing, each with its claim copied exactly, bears and why, and duplicates. Never add a claim the backing does not hold.
  inputs:
    backing: {like: check_findings, element: true}
    collection:
      json_schema: {type: string}
  output:
    json_schema:
      type: object
      title: ProposalJudgement
      required: [stance_id, title, pages, findings]
      properties:
        stance_id: {type: string}
        title: {type: string}
        pages:
          type: array
          items:
            type: object
            title: BearingPage
            required: [page_id, page_type, status, title]
            properties:
              page_id: {type: string}
              page_type: {type: string}
              status: {type: string}
              title: {type: string}
        findings:
          type: array
          items:
            type: object
            title: FindingJudgement
            required: [claim, bears, why]
            properties:
              claim:
                type: string
                description: The finding's claim reference, copied exactly from the backing.
              bears: {type: boolean}
              why:
                type: string
                description: One sentence on why the claim bears on the position or does not.
        duplicates:
          type: string
          description: The id of an existing stance that already holds this position. Null when none does.
  tools: [read_page, search]
  tier: orchestrator
  budget: {requests: 15, tool_calls: 60}
---

Use once per backing of a stance refresh, inside a map over check_findings, with `collection` wired from the
workflow's parameters. It answers, for each finding, whether its claim bears on the proposal's position and why, and
for the proposal the id of an existing stance it repeats, or null. It copies the proposal's id, title and pages, so its
answer is matched to the proposal it judged. It judges one proposal and never the list, so a weak proposal never costs
the others their place. It writes nothing. A later program keeps the findings judged to bear, names each refused one
with its reason, and drops a proposal left with findings from fewer than two digests, or one that repeats a stance
the base lists.
