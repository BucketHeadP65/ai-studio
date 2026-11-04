---
title: Back one proposed stance with findings
summary: The claims of the research knowledge base that support or contest one proposed new stance, each named by its reference with one sentence on how it bears, and no claim text typed.
version: 1
step:
  name: back_proposal
  instructions: |-
    You hold one proposal for a new stance. The `collection` input names the research knowledge base. The proposal carries stance_id, title and pages. title states the position the proposal takes, and pages lists the records pages behind it, each with its page_id, page_type, status and title. Other workers back the other proposals in parallel, so back only your own.

    A stance rests on the research literature the collection holds as digests: the pages of type paper, talk, article and post, whose ids start with paper., talk., art. and post. Every other page of the collection, a stance page among them, is not a digest, and a digest at status skipped holds no finding for this base and backs nothing. A digest holds its claims in its Findings section. Each claim carries an anchor such as ^c8, and a claim is cited by the digest's id, a #, and its anchor, such as paper.ace-2025#^c8.

    Search the collection for the position in two or three plain phrasings, with `knowledge_base` set to the collection. A hit's page id is its page_id when it has one, else its id. Read each digest among the hits that may bear on the position with read_page, with `knowledge_base` set to the collection and `page_id` set to its id. Its payload carries its frontmatter, with its type and status, and its claims, each with its anchor and its text.

    A claim takes a side when it supports the position or contests it: a result, a measurement or an argument that makes the position more or less likely to hold. A claim that only shares the topic, or bears on a neighbouring question, takes no side and is not a finding.

    Answer one finding for each claim that takes a side. claim is its reference, the digest's id and the claim's anchor exactly as read_page gave them, such as paper.ace-2025#^c8. side is supports or contests. how is one sentence on how the claim bears on the position as the title states it. Never type a claim's text: the run copies it from the page. Never type an id or an anchor from memory or build one from a title.

    The run keeps a proposal only when its findings come from at least two different digests, each holding the claim its finding names, at a status other than skipped, and drops the rest. A proposal whose findings all contest its position is kept and shown as one the literature argues against, so a claim against the position is as much a finding as one for it. Answer findings as an empty list when the literature you found takes no side.

    A reviewer may have rejected an earlier attempt. Its feedback names each proposal and finding to fix. Fix every finding it names for your proposal. When it says your proposal duplicates an existing stance, or that no finding you can find bears on it, answer findings as an empty list.

    The tools run inside a sandbox: call them directly with plain arguments, and `import asyncio` first if you gather calls in parallel (the import is required, the module is not preloaded). You write nothing. Answer stance_id, title and pages copied exactly from the proposal, and findings.
  inputs:
    proposal: {like: merge_proposals, element: true}
    collection:
      json_schema: {type: string}
  output:
    json_schema:
      type: object
      title: BackedProposal
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
          description: Each claim that takes a side on the position. Empty when none does.
          items:
            type: object
            title: ClaimFinding
            required: [claim, side, how]
            properties:
              claim:
                type: string
                description: The claim's reference, the digest id and the claim's anchor, such as paper.ace-2025#^c8.
              side:
                enum: [supports, contests]
                type: string
              how:
                type: string
                description: One sentence on how the claim bears on the position.
  tools: [search, fetch, read_page]
  tier: worker
  budget: {requests: 20, tool_calls: 120}
---

Use once per proposal of a stance refresh, inside a map over merge_proposals, with `collection` wired from the
workflow's parameters. It searches the research knowledge base, reads the digests that may bear on the proposal's
position and names each claim that supports or contests it by its reference, with one sentence on how. It types no
claim text. A later program reads every cited digest, keeps only the claims the base holds on a digest that is not
skipped, copies each claim's text from the page and drops a proposal left with findings from fewer than two
digests. A judge then reviews what is left. It writes nothing.
