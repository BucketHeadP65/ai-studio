---
title: Place changed pages against the stances
summary: For each records page of one group, the stance page it changes, a new stance to propose on at least two digests, or a skip with its reason.
version: 4
step:
  name: place_stance_pages
  instructions: |-
    You hold one group of pages from the records knowledge base, the one the `records` input names. The `collection` input names the research knowledge base, which holds the stance pages. The group is an object with a group_id and a pages list. Each page carries its page_id, page_type, status and title. Other workers place the other groups in parallel, so place only the pages of your own group.

    A stance page says what the system this base serves does on one topic. Its id starts with stance. and its status is the system's position on that topic: considering, trying, adopted or rejected. Its record field names the records page behind its main decision. A stance rests on the research literature the collection holds as digests: the pages of type paper, talk, article and post, whose ids start with paper., talk., art. and post. Every other page of the collection, a stance page among them, is not a digest.

    Call knowledge_base_profile once, with `knowledge_base` set to the collection. It describes the stance page type and its guidance.

    Then, for each page of your group: read it whole with fetch, with `knowledge_base` set to the records input and `unit_id` set to its page_id. Search the collection for its topic in a few plain words, with `knowledge_base` set to the collection. Read every hit whose id starts with stance. through fetch, with `knowledge_base` set to the collection. Search once more in other words before you decide that no stance covers the topic. When no stance covers it and the page takes a position on a research question, search the collection for the literature on that question in two or three plain phrasings, with `knowledge_base` set to the collection. A hit's page id is its page_id when it has one, else its id.

    A page changes a stance when it would move that stance's status by the rule below, or when it changes what the stance says the system does. The rule:
    - adopted, when an architecture page at status current, or a spec-doc at status executed, shows the system does it.
    - trying, when an adr at status accepted, or a spec-doc at status approved, plans it and nothing shows it built.
    - rejected, when an adr at status accepted decides against it, or the adr the stance rests on is superseded with no replacement.
    - Otherwise the status stays as it is.

    Answer exactly one placement for each page, with its page_id copied from the group.
    - placement stance: the page changes an existing stance. stance_id is that stance's id and title is its title. research_pages is an empty list.
    - placement new: no stance covers the topic, and the page takes a position on a question the research literature addresses, such as retrieval, memory, evaluation or agent design: a position papers could support or contest. research_pages holds the page ids of the digests that bear on that position, at least two, each copied exactly from a search hit of the collection you saw while placing this group. Never type an id from memory or build one from a title. stance_id is a proposed id: stance. followed by a few lowercase words joined by hyphens, such as stance.agent-memory. title states the position in plain words. Give every page of your group on the same new topic the same proposed id. A worker of another group may propose the same stance, so choose the id the topic most plainly carries.
    - placement skip: the page changes no stance and proposes none. Tooling, housekeeping and a change too small to move a position are skips. So is an implementation choice no research question stands behind, such as a file layout, an export format or how a source's files are linked, and a position your searches found fewer than two digests for. stance_id and title are empty strings, and research_pages is an empty list.
    Every placement carries a reason of one sentence. A skip's reason says why the page changes no stance, and for an implementation choice it says that no research question stands behind it.

    The run keeps a proposed stance only when at least two of its research_pages are digests the collection holds, counted across every page proposed under the same id, and drops the rest. A stance page or an id the collection does not hold never counts.

    The tools run inside a sandbox: call them directly with plain arguments, and `import asyncio` first if you gather calls in parallel (the import is required, the module is not preloaded). You write nothing. Answer one object with a placements list that holds one placement for each page of your group.
  inputs:
    group: {like: group_pages, element: true}
    collection:
      json_schema: {type: string}
    records:
      json_schema: {type: string}
  output:
    json_schema:
      type: object
      title: Placements
      required: [placements]
      properties:
        placements:
          type: array
          items:
            type: object
            title: Placement
            required: [page_id, placement, stance_id, title, reason, research_pages]
            properties:
              page_id: {type: string}
              placement:
                enum: [stance, new, skip]
                type: string
              stance_id: {type: string}
              title: {type: string}
              reason: {type: string}
              research_pages:
                type: array
                items: {type: string}
  tools: [search, fetch, knowledge_base_profile]
  tier: worker
  budget: {requests: 30, tool_calls: 200}
---

Use once per group of a stance refresh, inside a map over group_pages, with `collection` and `records` wired from the
workflow's parameters. The group holds only the changed pages no stance names as its record, since a stance whose
record changed is refreshed without a placement. It writes nothing in either knowledge base and never drafts a
stance. Two later merges turn the placements into one rewrite item per stance the run read and one proposal per
proposed new stance id. A proposal keeps the digests its placements cite that the collection holds, and one left with
fewer than two is dropped. The run drafts no proposal.
