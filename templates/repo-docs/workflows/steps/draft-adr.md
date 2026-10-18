---
title: Draft one ADR
summary: One proposed ADR absorbing one executed design's durable decisions, grounded in its plan and analysis siblings and the base's own accepted ADRs, checked against the base's grammar before it is answered.
version: 3
step:
  name: draft_adr
  instructions: |
    Fetch the design page whole, then search the base for its plan and analysis siblings (same initiative folder in
    their ids or titles) and read them. Call knowledge_base_profile and follow the adr page type's guidance and sections. Write
    one ADR that absorbs this design, its durable decisions, not its task list. Its sections carry the headings
    Context, Decision, Consequences, in order, with an optional Options considered between Decision and
    Consequences. Search the base for an accepted ADR that already decides the same thing, and if one exists say so
    in Context and decide only what is new. Copy the design's page id into design_id. The slug is the decision in a
    few lowercase words joined by hyphens, with no adr prefix and no number. The title names the decision in plain
    words, with no ADR prefix, no number, and plain punctuation, no em dashes.

    Before answering, run check_note with the page this draft becomes: frontmatter id adr-0000-<your slug>, type
    adr, app root, status proposed, your title, summary, and absorbs [[<design_id>]], your sections, path
    adr/0000-<your slug>.md. The real number is assigned at write time, 0000 stands in. Fix every finding it names
    and check again. Answer only a draft whose page passes.
  inputs:
    design: {like: pending_designs, element: true}
    collection: {json_schema: {type: string}}
  output:
    json_schema:
      type: object
      title: DraftAdr
      properties:
        design_id: {type: string}
        slug: {type: string, pattern: "^[a-z0-9]+(-[a-z0-9]+)*$", maxLength: 60}
        title: {type: string}
        summary: {type: string}
        sections:
          type: array
          items:
            type: object
            title: Section
            properties:
              heading: {type: string}
              body: {type: string}
            required: [heading, body]
      required: [design_id, slug, title, summary, sections]
  tools: [fetch, search, knowledge_base_profile, check_note]
---

Use once per pending design, inside a map over pending_designs. Do not use to edit an accepted ADR. It only drafts a
proposed one that a gate and the owner still review.
