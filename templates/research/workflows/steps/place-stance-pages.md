---
title: Place changed pages against the stances
summary: For each records page of one group, the stance page it changes, a new stance to draft, or a skip with its reason.
version: 1
step:
  name: place_stance_pages
  instructions: |-
    You hold one group of pages from the records knowledge base, the one the `records` input names. The `collection` input names the research knowledge base, which holds the stance pages. The group is an object with a group_id and a pages list. Each page carries its page_id, page_type, status and title. Other workers place the other groups in parallel, so place only the pages of your own group.

    A stance page says what the system this base serves does on one topic. Its id starts with stance. and its status is the system's position on that topic: considering, trying, adopted or rejected. Its record field names the records page behind its main decision.

    Call knowledge_base_profile once, with `knowledge_base` set to the collection. It describes the stance page type and its guidance.

    Then, for each page of your group: read it whole with fetch, with `knowledge_base` set to the records input and `unit_id` set to its page_id. Search the collection for its topic in a few plain words, with `knowledge_base` set to the collection. Read every hit whose id starts with stance. through fetch, with `knowledge_base` set to the collection. Search once more in other words before you decide that no stance covers the topic.

    A page changes a stance when it would move that stance's status by the rule below, or when it changes what the stance says the system does. The rule:
    - adopted, when an architecture page at status current, or a spec-doc at status executed, shows the system does it.
    - trying, when an adr at status accepted, or a spec-doc at status approved, plans it and nothing shows it built.
    - rejected, when an adr at status accepted decides against it, or the adr the stance rests on is superseded with no replacement.
    - Otherwise the status stays as it is.

    Answer exactly one placement for each page, with its page_id copied from the group.
    - placement stance: the page changes an existing stance. stance_id is that stance's id and title is its title.
    - placement new: no stance covers the topic, and the page decides or ships something the system does that deserves a stance of its own. stance_id is a proposed id: stance. followed by a few lowercase words joined by hyphens, such as stance.agent-memory. title states the position in plain words. Give every page of your group on the same new topic the same proposed id. A worker of another group may propose the same stance, so choose the id the topic most plainly carries.
    - placement skip: the page changes no stance, such as tooling, housekeeping or a change too small to move a position. stance_id and title are empty strings.
    Every placement carries a reason of one sentence. A skip's reason says why the page changes no stance.

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
            required: [page_id, placement, stance_id, title, reason]
            properties:
              page_id: {type: string}
              placement:
                enum: [stance, new, skip]
                type: string
              stance_id: {type: string}
              title: {type: string}
              reason: {type: string}
  tools: [search, fetch, knowledge_base_profile]
  tier: worker
  budget: {requests: 30, tool_calls: 60}
---

Use once per group of a stance refresh, inside a map over group_pages, with `collection` and `records` wired from the
workflow's parameters. The group holds only the changed pages no stance names as its record, since a stance whose
record changed is refreshed without a placement. It writes nothing in either knowledge base and never drafts a
stance. A later merge turns the placements into one item per stance.
