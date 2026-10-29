---
title: Refresh stances from the docs
description: >-
  Reads the records pages that changed, places each against the base's stance pages, rewrites the stances they change
  under a judge, and writes each page only when its Evidence and Alternatives came through untouched.
uses: [st.list_stance_changes.v1, st.place_stance_pages.v1, st.refresh_stance.v1, st.check_stances.v1, st.write_stances.v1]
presentation:
  purpose: >-
    Keep every stance page saying what the system does today. A stance holds the system's position on one topic as
    its status, and the records knowledge base is where that position is decided and shipped, so a stance nobody
    refreshes from it goes stale as soon as the work moves on.
  produces: >-
    Each stance a changed records page bears on, rewritten in place: its status following the records, and its
    summary, TL;DR, Position and Why brought up to date with every changed sentence naming the records page behind
    it. Its Evidence and Alternatives stay exactly as they were. A topic no stance covers gets a new stance page. The
    run card lists the stances written, the ones refused with the check each failed, where every changed page was
    placed, every stance as it was before the run, and every stance the run could not read with the reason.
  owner_participation: >-
    Nothing during a run. A schedule starts it when the records knowledge base is indexed, and you start a full pass
    by hand by leaving the changed pages empty. A judge, not you, rejects a rewrite whose sentences no records page
    supports or whose status the records do not give. Every rewrite lands directly. To undo one, put the page back
    from the run's stances as they were before it.
  not_for: >-
    Not for adding literature to a stance, which comes from the digests and the flag pass. Not for editing a
    stance's Evidence or Alternatives, which this run never changes. Not for more than twenty-five changed pages at
    once: such a run keeps none of them, and a full pass started by hand covers them instead.
  inputs:
    - {key: collection, label: Knowledge base holding the stances, control: knowledge-base}
    - {key: records, label: Knowledge base the stances follow, control: knowledge-base}
    - {key: progress, label: Page types and statuses that count, control: text}
    - {key: changed_page_ids, label: Only these changed pages, control: text}
  result_fields:
    - {label: Stances written and refused (recorded), path: values.write_stances}
    - {label: Stances as they were before the run (recorded), path: values.list_changes}
    - {label: Stances this run could not read (recorded), path: values.list_changes.unread}
    - {label: Where each changed page was placed (recorded), path: values.place_pages}
workflow:
  name: research-stance-refresh
  version: 1
  description: List the records pages that changed at a status that counts, place them against the stances in groups of five, rewrite each stance they change, judge every draft, then write each page that passes its check against the page as it was read.
  parameters:
    collection:
      description: The knowledge base holding the stance pages, by name.
      json_schema: {type: string, minLength: 1}
    records:
      description: The knowledge base the stances follow, by name.
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
                items[stance["stance_id"]] = {"stance_id": stance["stance_id"], "title": stance["title"], "path": stance["path"], "new": False, "pages": [admitted[stance["record"]]], "current": stance["payload"]}
        for answer in placements:
            for placed in answer["placements"]:
                stance_id = placed["stance_id"]
                page = admitted.get(placed["page_id"])
                if page is None or placed["placement"] == "skip" or not stance_id:
                    continue
                if stance_id not in items:
                    stance = read.get(stance_id)
                    if stance is not None:
                        items[stance_id] = {"stance_id": stance_id, "title": stance["title"], "path": stance["path"], "new": False, "pages": [], "current": stance["payload"]}
                    elif placed["placement"] == "new":
                        items[stance_id] = {"stance_id": stance_id, "title": placed["title"], "path": "", "new": True, "pages": [], "current": None}
                    else:
                        continue
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
            required: [stance_id, title, path, new, pages]
            properties:
              stance_id: {type: string}
              title: {type: string}
              path: {type: string}
              new: {type: boolean}
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
                description: The stance page as it stood before the run. Null for a new stance.
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
  result:
    summary: The stances the changed records pages bear on, rewritten and written, with each refused page named beside them.
    primary_work: refresh_stances
    empty_primary: noop
    required_gates:
      - {gate_id: check_stances, accepted_sources: [judge]}
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
        collection: {parameter: collection}
  output: {node: write_stances}
---

Use on an `on index` schedule over the records knowledge base, with `collection` set to the research knowledge base,
`records` to the records one and `progress` to the page types and statuses that count. Use by hand with
`changed_page_ids` empty for a full pass. Not for editing a stance's evidence, which the digests and the flag pass
grow.

The stages: `list_changes` reads each changed records page and keeps the ones `progress` admits, or lists every
admitted page for a full pass, and reads every stance page as it stands. A stance it cannot read, or one carrying a
field it does not declare, is named with the reason and left alone. Past twenty-five changed pages it keeps none, so
both maps run over nothing. `group_pages` puts the admitted pages no stance names as its record into groups of
five, counting the stances it could not read. `place_pages` runs one worker per group, four at a time, each placing its pages against an existing stance,
a new stance, or a skip. `merge_work` makes one item per stance with every page that bears on it, its record among
them, and one item per proposed new stance, combining the groups that proposed the same id. `refresh_stances` runs
one worker per item, two at a time, each rewriting its stance or drafting the new one and checking it against the
base's grammar. `check_stances` is the judge, and a rejection sends the refresh round again with its feedback. An
empty list is approved without a read. `write_stances` writes each approved page whose id, Evidence and Alternatives
match the page as it was read, refuses a new stance that takes the id of a stance that exists, catches each refusal,
and answers which were written and which failed which check.
Three consecutive identical failures in either map abort the run as failed.
