---
title: Refresh stances from the docs
description: >-
  Reads the records pages that changed, places each against the base's stance pages, rewrites the stances they change
  under a judge, merges each rewrite into its page as read, keeping its Evidence and Alternatives, and lists the new
  stances they propose on two digests or more.
uses: [st.list_stance_changes.v2, st.place_stance_pages.v4, st.refresh_stance.v4, st.check_stances.v3, st.write_stances.v4]
presentation:
  purpose: >-
    Keep every stance page saying what the system does today. A stance holds the system's position on one topic as
    its status, and the records knowledge base is where that position is decided and shipped, so a stance nobody
    refreshes from it goes stale as soon as the work moves on.
  produces: >-
    Each stance a changed records page bears on, rewritten in place: its status following the records, and its
    summary, TL;DR, Position and Why brought up to date with every changed sentence naming the records page behind
    it. Its Evidence and Alternatives stay exactly as they were. A topic no stance covers is proposed as a new stance,
    and never drafted, only when its page takes a position on a question research literature addresses and at least
    two of the base's digests (its paper, talk, article and post pages) bear on that position. An implementation
    choice with no research question behind it is skipped with a reason that says so. The run card lists the stances
    written, the ones refused with the check each failed, where every changed page was placed, every stance as it was
    before the run, and every stance the run could not read with the reason. It shows every proposed new stance with
    the records pages and the digests behind it among what the run left behind, collected as soon as the pages are
    placed, so a run that fails while rewriting still carries them.
  owner_participation: >-
    Nothing during a run, unless the judge cannot decide. A schedule starts it when the records knowledge base is
    indexed, and you start a full pass by hand by leaving the changed pages empty. A judge, not you, rejects a rewrite
    whose sentences no records page supports or whose status the records do not give. A judge that cannot decide asks
    you, and the run waits for your answer, so a schedule over this workflow carries a gate policy that fails after a
    wait (fail_after:<seconds>). Every rewrite lands directly. To undo one, put the page back from the run's stances as
    they were before it. A proposed new stance is yours to draft by hand.
  not_for: >-
    Not for adding literature to a stance, which comes from the digests and the flag pass. Not for editing a
    stance's Evidence or Alternatives, which this run never changes. Not for more than twenty-five changed pages at
    once: such a run keeps none of them, and a full pass started by hand covers them instead.
  inputs:
    - {key: collection, label: Knowledge base holding the stances, control: knowledge-base}
    - {key: records, label: Knowledge base the stances follow, control: text}
    - {key: progress, label: Page types and statuses that count, control: text}
    - {key: changed_page_ids, label: Only these changed pages, control: text}
  result_fields:
    - {label: "Stances written, refused and proposed (recorded)", path: values.write_stances}
    - {label: "What the run read: each stance as it was, the stances it could not read, and whether it kept nothing past the cap of 25 (recorded)", path: values.list_changes}
    - {label: Where each changed page was placed (recorded), path: values.place_pages}
workflow:
  name: research-stance-refresh
  version: 7
  description: List the records pages that changed at a status that counts, place them against the stances in groups of five, rewrite each existing stance they change as its changes alone, judge every draft, merge each rewrite into the page as it was read and write it, and list every new stance they propose with its pages and the digests it rests on, drafting none and dropping any that rests on fewer than two digests the base holds.
  parameters:
    collection:
      description: The knowledge base holding the stance pages, by name.
      json_schema: {type: string, minLength: 1}
    records:
      description: The knowledge base the stances follow, named by its registered name.
      json_schema: {type: string, minLength: 1}
    progress:
      description: Which page types at which status count, as comma-separated type:status pairs.
      json_schema: {type: string, minLength: 1}
    changed_page_ids:
      description: Optional comma-separated page ids of the records knowledge base. Empty means a full pass over every page progress admits. Set means only those pages, and more than twenty-five admitted keep none.
      json_schema: {type: string}
      required: false
  inline:
    - name: group_pages
      source: |
        named = [stance["record"] for stance in [*changes["stances"], *changes["unread"]] if stance["record"]]
        loose = [page for page in changes["admitted"] if page["page_id"] not in named]
        groups = []
        start = 0
        while start < len(loose):
            groups.append({"group_id": len(groups), "pages": loose[start:start + 5]})
            start = start + 5
        groups
      inputs:
        changes: {like: list_stance_changes}
      output:
        json_schema:
          type: array
          items:
            type: object
            title: PageGroup
            required: [group_id, pages]
            properties:
              group_id: {type: integer}
              pages:
                type: array
                items:
                  type: object
                  title: GroupedPage
                  required: [page_id, page_type, status, title]
                  properties:
                    page_id: {type: string}
                    page_type: {type: string}
                    status: {type: string}
                    title: {type: string}
    - name: merge_work
      source: |
        admitted = {}
        for page in changes["admitted"]:
            admitted[page["page_id"]] = page
        read = {}
        for stance in changes["stances"]:
            read[stance["stance_id"]] = stance
        items = {}
        for stance in changes["stances"]:
            if stance["record"] in admitted:
                items[stance["stance_id"]] = {"stance_id": stance["stance_id"], "title": stance["title"], "path": stance["path"], "pages": [admitted[stance["record"]]], "current": stance["payload"]}
        for answer in placements:
            for placed in answer["placements"]:
                stance = read.get(placed["stance_id"])
                page = admitted.get(placed["page_id"])
                if page is None or stance is None or placed["placement"] == "skip":
                    continue
                stance_id = stance["stance_id"]
                if stance_id not in items:
                    items[stance_id] = {"stance_id": stance_id, "title": stance["title"], "path": stance["path"], "pages": [], "current": stance["payload"]}
                if placed["page_id"] not in [held["page_id"] for held in items[stance_id]["pages"]]:
                    items[stance_id]["pages"].append(page)
        list(items.values())
      inputs:
        changes: {like: list_stance_changes}
        placements: {like: place_stance_pages, many: true}
      output:
        json_schema:
          type: array
          items:
            type: object
            title: StanceWork
            required: [stance_id, title, path, pages, current]
            properties:
              stance_id: {type: string}
              title: {type: string}
              path: {type: string}
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
              current:
                type: object
                title: StancePayload
                description: The stance page as it stood before the run.
                required: [frontmatter, sections]
                properties:
                  frontmatter:
                    type: object
                    title: StanceFrontmatter
                    required: [id, type, title, status]
                    properties:
                      id: {type: string}
                      type: {type: string}
                      title: {type: string}
                      summary: {type: string}
                      status: {type: string}
                      record: {type: string}
                      synthesizes:
                        type: array
                        items: {type: string}
                      recommends:
                        type: array
                        items: {type: string}
                      advises_against:
                        type: array
                        items: {type: string}
                      aliases:
                        type: array
                        items: {type: string}
                      superseded_by: {type: string}
                      decision_reason: {type: string}
                      created: {type: string}
                  sections:
                    type: array
                    items:
                      type: object
                      title: StanceSection
                      required: [heading, level, body]
                      properties:
                        heading: {type: string}
                        level: {type: integer}
                        body: {type: string}
    - name: merge_proposals
      source: |
        admitted = {}
        for page in changes["admitted"]:
            admitted[page["page_id"]] = page
        proposals = {}
        for answer in placements:
            for placed in answer["placements"]:
                stance_id = placed["stance_id"]
                page = admitted.get(placed["page_id"])
                if page is None or placed["placement"] != "new" or not stance_id or stance_id in changes["stance_ids"]:
                    continue
                if stance_id not in proposals:
                    proposals[stance_id] = {"stance_id": stance_id, "title": placed["title"], "pages": [], "research_pages": []}
                if placed["page_id"] not in [held["page_id"] for held in proposals[stance_id]["pages"]]:
                    proposals[stance_id]["pages"].append(page)
                for research_id in placed["research_pages"]:
                    if research_id not in proposals[stance_id]["research_pages"]:
                        proposals[stance_id]["research_pages"].append(research_id)
        digests = {}
        if proposals:
            for page_type in ["paper", "talk", "article", "post"]:
                for row in await list_pages(knowledge_base=collection, page_type=page_type, limit=10000):
                    digests[row["page_id"]] = True
        kept = []
        for proposal in proposals.values():
            held = [research_id for research_id in proposal["research_pages"] if research_id in digests]
            if len(held) >= 2:
                kept.append({"stance_id": proposal["stance_id"], "title": proposal["title"], "pages": proposal["pages"], "research_pages": held})
        kept
      inputs:
        changes: {like: list_stance_changes}
        placements: {like: place_stance_pages, many: true}
        collection:
          json_schema: {type: string}
      output:
        json_schema:
          type: array
          items:
            type: object
            title: ProposedStance
            required: [stance_id, title, pages, research_pages]
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
              research_pages:
                type: array
                description: The digests of the base the proposal rests on, at least two.
                items: {type: string}
      tools: [list_pages]
  result:
    summary: The stances the changed records pages bear on, rewritten and written, with each refused page named beside them and each new stance they propose listed with its pages and the digests it rests on.
    primary_work: refresh_stances
    empty_primary: noop
    required_gates:
      - {gate_id: check_stances, accepted_sources: [judge]}
    artifacts:
      - id: proposed_stances
        label: New stances proposed, each with the records pages and the digests behind it
        source: {node: merge_proposals}
        media_type: application/json
  nodes:
    - kind: run
      id: list_changes
      work: list_stance_changes
      inputs:
        collection: {parameter: collection}
        records: {parameter: records}
        progress: {parameter: progress}
        changed_page_ids: {parameter: changed_page_ids}
    - kind: run
      id: group_pages
      work: group_pages
      inputs:
        changes: {node: list_changes}
    - kind: map
      id: place_pages
      turns: [place_stance_pages]
      over: {node: group_pages}
      each: group
      inputs:
        collection: {parameter: collection}
        records: {parameter: records}
      max_parallel: 4
      on_error: skip
      abort_after_repeats: 3
    - kind: run
      id: merge_work
      work: merge_work
      inputs:
        changes: {node: list_changes}
        placements: {node: place_pages}
    - kind: run
      id: merge_proposals
      work: merge_proposals
      inputs:
        changes: {node: list_changes}
        placements: {node: place_pages}
        collection: {parameter: collection}
    - kind: map
      id: refresh_stances
      turns: [refresh_stance]
      over: {node: merge_work}
      each: item
      inputs:
        collection: {parameter: collection}
        records: {parameter: records}
      max_parallel: 2
      on_error: skip
      abort_after_repeats: 3
    - kind: judge
      id: check_stances
      work: check_stances
      reviews: {node: refresh_stances}
      inputs:
        collection: {parameter: collection}
        records: {parameter: records}
      approved_when: outcome
      rejected_to: refresh_stances
    - kind: run
      id: write_stances
      work: write_stances
      inputs:
        drafts: {node: check_stances}
        changes: {node: list_changes}
        proposals: {node: merge_proposals}
        collection: {parameter: collection}
  output: {node: write_stances}
---

Use on an `on index` schedule over the records knowledge base, with `collection` set to the research knowledge base,
`records` to the records one and `progress` to the page types and statuses that count. Use by hand with
`changed_page_ids` empty for a full pass. Run now on that schedule while it has collected no changed page passes an
empty `changed_page_ids` too, so it also starts a full pass. Not for editing a stance's evidence, which the digests and the flag pass
grow.

The stages: `list_changes` lists every records page `progress` admits and keeps the changed ones, or every one for
a full pass, and reads every stance page as it stands. A stance it cannot read, or one carrying a
field it does not declare, is named with the reason and left alone. Past twenty-five changed pages it keeps none, so
both maps run over nothing. `group_pages` puts the admitted pages no stance names as its record into groups of
five, counting the stances it could not read. `place_pages` runs one worker per group, four at a time, each placing its pages against an existing stance,
a new stance, or a skip. A worker proposes a new stance only for a page that takes a position on a question research
literature addresses, and names the digests bearing on it that it found with `search`. An implementation choice with
no research question behind it is a skip. `merge_work` makes one item per stance the run read with every page that
bears on it, its record among them. `merge_proposals` makes one proposal per proposed new stance id with every page
behind it and every digest its placements name, combining the groups that proposed the same id. It lists the base's
digests, its paper, talk, article and post pages, with `list_pages`, keeps only the named ids among them, so a stance
page or an id the base does not hold never counts, and drops a proposal left with fewer than two. Its list is the
run's `proposed_stances` artifact. It waits on neither the refresh map nor the judge, so a run either of them fails
still carries it. An id a listed stance carries
makes no proposal: a page placed under it joins that stance's item when the run read the stance, and is left alone
when it could not. `refresh_stances` runs one worker per item, two at a time, and no worker drafts a proposal. Each
answers only its stance's changes:
the status, the summary and the sections it rewrites among TL;DR, Position, Why and Open Questions, checked against
the base's grammar as the whole page they make, and names any finding it left because only a change to Evidence or
Alternatives would fix it. `check_stances` is the judge, and a rejection sends the refresh round again with its
feedback. An empty list is approved without a read. `write_stances` merges each approved rewrite into the page as it
was read, keeping every section and frontmatter field the rewrite does not name, and writes it to the path it was
read from. It refuses a rewrite that names a section twice or names one the page does not carry, catches each
refusal, and answers which were written, which failed which check, and every proposal, for which it writes no page.
Three consecutive identical failures in either map abort the run as failed.
