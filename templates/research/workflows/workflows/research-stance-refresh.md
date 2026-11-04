---
title: Refresh stances from the docs
description: >-
  Reads the records pages that changed, places each against the base's stance pages, rewrites and writes the stances
  they change under a judge, keeping their Evidence and Alternatives, then lists each new stance they propose with the
  findings on it, under a second judge.
uses:
  - st.list_stance_changes.v2
  - st.place_stance_pages.v6
  - st.refresh_stance.v4
  - st.check_stances.v4
  - st.back_proposal.v2
  - st.judge_proposals.v1
  - st.write_stances.v6
presentation:
  purpose: >-
    Keep every stance page saying what the system does today. A stance holds the system's position on one topic as
    its status, and the records knowledge base is where that position is decided and shipped, so a stance nobody
    refreshes from it goes stale as soon as the work moves on.
  produces: >-
    Each stance a changed records page bears on, rewritten in place: its status following the records, and its
    summary, TL;DR, Position and Why brought up to date with every changed sentence naming the records page behind
    it. Its Evidence and Alternatives stay exactly as they were. A topic no stance covers is proposed as a new stance,
    and never drafted, only when its page takes a position on a question research literature addresses and claims of
    at least two of the base's digests (its paper, talk, article and post pages, none at status skipped) support or
    contest that position. An implementation choice with no research question behind it is skipped with a reason that
    says so. The run card lists the stances written, the ones refused with the check each failed, where every changed
    page was placed, every stance as it was before the run, and every stance the run could not read with the reason.
    Among what the run left behind it shows every proposed new stance a judge approved, with the records pages behind
    it and each finding: the claim's reference, the claim's own text, whether it supports or contests the position,
    and one sentence on how. A proposal whose findings all contest its position is marked as one the literature argues
    against. The proposals are backed and judged only after every rewrite is written, so a run whose proposals then
    fail still keeps its rewrites written and indexed.
  owner_participation: >-
    Nothing during a run, unless a judge cannot decide. A schedule starts it when the records knowledge base is
    indexed, and you start a full pass by hand by leaving the changed pages empty. A judge, not you, rejects a rewrite
    whose sentences no records page supports or whose status the records do not give. A second judge rejects a
    proposal's finding whose claim does not bear on its position, and a proposal that duplicates a stance. A judge
    that cannot decide asks you, and the run waits for your answer, so a schedule over this workflow carries a gate policy that fails after a
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
    - {label: "Stances written and refused (recorded)", path: values.write_stances}
    - {label: "What the run read: each stance as it was, the stances it could not read, and whether it kept nothing past the cap of 25 (recorded)", path: values.list_changes}
    - {label: Where each changed page was placed (recorded), path: values.place_pages}
workflow:
  name: research-stance-refresh
  version: 11
  description: List the records pages that changed at a status that counts, place them against the stances in groups of five, rewrite each existing stance they change as its changes alone, judge every draft, merge each rewrite into the page as it was read and write it, then back every new stance they propose with the claims of the base that support or contest it, keep only the findings code confirms on the base's digests, judge the proposals apart from the rewrites, and list every approved proposal with its pages and findings as placement made it, drafting none.
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
                    proposals[stance_id] = {"stance_id": stance_id, "title": placed["title"], "pages": []}
                if placed["page_id"] not in [held["page_id"] for held in proposals[stance_id]["pages"]]:
                    proposals[stance_id]["pages"].append(page)
        list(proposals.values())
      inputs:
        changes: {like: list_stance_changes}
        placements: {like: place_stance_pages, many: true}
        written: {like: write_stances}
      output:
        json_schema:
          type: array
          items:
            type: object
            title: Proposal
            required: [stance_id, title, pages]
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
    - name: check_findings
      source: |
        digest_types = ["paper", "talk", "article", "post"]
        read = {}
        named = []
        kept = []
        for backing in backings:
            if backing["stance_id"] in named:
                continue
            named.append(backing["stance_id"])
            findings = []
            digests = []
            for finding in backing["findings"]:
                claim = finding["claim"].strip()
                if claim.startswith("[[") and claim.endswith("]]"):
                    claim = claim[2:-2]
                claim = claim.split("|")[0].strip()
                if "/" in claim and ":" in claim.split("/")[0]:
                    claim = claim.split("/", 1)[1]
                if "#" not in claim:
                    continue
                page_id, anchor = claim.split("#", 1)
                page_id = page_id.strip()
                anchor = anchor.strip()
                if not anchor.startswith("^"):
                    anchor = "^" + anchor
                reference = page_id + "#" + anchor
                if not page_id or reference in [held["claim"] for held in findings]:
                    continue
                if page_id not in read:
                    try:
                        read[page_id] = await read_page(knowledge_base=collection, page_id=page_id)
                    except Exception as error:
                        read[page_id] = None
                page = read[page_id]
                if page is None:
                    continue
                front = page["payload"]["frontmatter"]
                if front.get("type") not in digest_types or front.get("status") == "skipped":
                    continue
                text = ""
                for held in page["payload"]["claims"]:
                    if held["anchor"] == anchor:
                        text = held["text"]
                if not text:
                    continue
                findings.append({"claim": reference, "claim_text": text, "side": finding["side"], "how": finding["how"]})
                if page_id not in digests:
                    digests.append(page_id)
            if len(digests) >= 2:
                contests_only = True
                for held in findings:
                    if held["side"] != "contests":
                        contests_only = False
                kept.append({"stance_id": backing["stance_id"], "title": backing["title"], "pages": backing["pages"], "findings": findings, "contests_only": contests_only})
        kept
      inputs:
        backings: {like: back_proposal, many: true}
        collection:
          json_schema: {type: string}
      output:
        json_schema:
          type: array
          items:
            type: object
            title: ProposedStance
            required: [stance_id, title, pages, findings, contests_only]
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
                description: The claims that take a side on the position, each on a digest the base holds at a status other than skipped, from at least two digests.
                items:
                  type: object
                  title: CheckedFinding
                  required: [claim, claim_text, side, how]
                  properties:
                    claim:
                      type: string
                      description: The claim's reference, the digest id and the claim's anchor, such as paper.ace-2025#^c8.
                    claim_text:
                      type: string
                      description: The claim's own text, copied from the digest.
                    side:
                      enum: [supports, contests]
                      type: string
                    how:
                      type: string
                      description: One sentence on how the claim bears on the position.
              contests_only:
                type: boolean
                description: True when every finding contests the position, so the literature argues against it.
      tools: [read_page]
    - name: list_proposals
      source: |
        merged = {}
        for proposal in proposals:
            merged[proposal["stance_id"]] = proposal
        listed = []
        for held in approved:
            made = merged.get(held["stance_id"])
            if made is None or made["title"] != held["title"] or made["pages"] != held["pages"]:
                continue
            listed.append(held)
        listed
      inputs:
        approved: {like: check_findings}
        proposals: {like: merge_proposals}
      output: {like: check_findings}
  result:
    summary: The stances the changed records pages bear on, rewritten and written, with each refused page named beside them and each new stance they propose listed with its pages and the findings from the literature that take a side on it.
    primary_work: refresh_stances
    empty_primary: noop
    required_gates:
      - {gate_id: check_stances, accepted_sources: [judge]}
      - {gate_id: judge_proposals, accepted_sources: [judge]}
    artifacts:
      - id: proposed_stances
        label: New stances proposed, each with the records pages behind it and the findings from the literature that take a side on it
        source: {node: list_proposals}
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
        work: {node: merge_work}
        collection: {parameter: collection}
    - kind: run
      id: merge_proposals
      work: merge_proposals
      inputs:
        changes: {node: list_changes}
        placements: {node: place_pages}
        written: {node: write_stances}
    - kind: map
      id: back_proposals
      turns: [back_proposal]
      over: {node: merge_proposals}
      each: proposal
      inputs:
        collection: {parameter: collection}
      max_parallel: 2
      on_error: skip
      abort_after_repeats: 3
    - kind: run
      id: check_findings
      work: check_findings
      inputs:
        backings: {node: back_proposals}
        collection: {parameter: collection}
    - kind: judge
      id: judge_proposals
      work: judge_proposals
      reviews: {node: check_findings}
      inputs:
        collection: {parameter: collection}
      approved_when: outcome
      rejected_to: back_proposals
    - kind: run
      id: list_proposals
      work: list_proposals
      inputs:
        approved: {node: judge_proposals}
        proposals: {node: merge_proposals}
  output: {node: list_proposals}
---

Use on an `on index` schedule over the records knowledge base, with `collection` set to the research knowledge base,
`records` to the records one and `progress` to the page types and statuses that count. Use by hand with
`changed_page_ids` empty for a full pass. Run now on that schedule while it has collected no changed page passes an
empty `changed_page_ids` too, so it also starts a full pass. Not for editing a stance's evidence, which the digests and the flag pass
grow.

The stages: `list_changes` lists every records page `progress` admits and keeps the changed ones, or every one for
a full pass, and reads every stance page as it stands. A stance it cannot read, or one carrying a field it does not
declare, is named with the reason and left alone. Past twenty-five changed pages it keeps none, so every map runs over
nothing. `group_pages` puts the admitted pages no stance names as its record into groups of five, counting the
stances it could not read. `place_pages` runs one worker per group, four at a time, each placing its pages against an
existing stance, a new stance, or a skip. A worker proposes a new stance, by an id and a title, only for a page that
takes a position on a question research literature addresses, one papers could support or contest, and cites no
literature. An implementation choice with no research question behind it is a skip. `merge_work` makes one item per
stance the run read with every page that bears on it, its record among them. An id a listed stance carries makes no
proposal: a page placed under it joins that stance's item when the run read the stance, and is left alone when it
could not.

`refresh_stances` runs one worker per item, two at a time, and no worker drafts a proposal. Each answers only its
stance's changes: the status, the summary and the sections it rewrites among TL;DR, Position, Why and Open Questions,
checked against the base's grammar as the whole page they make, and names any finding it left because only a change
to Evidence or Alternatives would fix it. `check_stances` is the judge, and a rejection sends the refresh round again
with its feedback. An empty list is approved without a read. `write_stances` merges each approved rewrite into the
page as `merge_work` carries it, read before the run, keeping every section and frontmatter field the rewrite does not
name, and writes it to the path it was read from. It refuses a rewrite that names a section twice or names one the
page does not carry, catches each refusal, and answers which were written and which failed which check.

The proposals start only once every rewrite is written. `merge_proposals` waits on `write_stances` and makes one
proposal per proposed new stance id with its title and every page behind it, combining the groups that proposed the
same id. `back_proposals` runs one worker per proposal, two at a time. Each searches the research knowledge base,
reads the digests that may bear on the position with `read_page`, and answers its findings: each claim that supports
or contests the position, by its reference such as `paper.ace-2025#^c8`, its side, and one sentence on how it bears.
It types no claim text, and copies the proposal's id, title and pages into its answer. `check_findings` reads every
cited digest once with `read_page` and keeps a finding only when the page is a digest, its paper, talk, article and
post pages, not at status skipped, and holds that claim anchor. It copies the claim's text from the page beside the
worker's sentence. A claim cited as a wikilink, such as `[[paper.x#^c3|label]]`, or as a search ref, such as
`page:research/paper.x#^c3` or `unit:research/paper.x#c3`, counts as `paper.x#^c3`. A digest `read_page` refuses drops
its own findings, and the run carries on. It drops a proposal left with findings from fewer than two different
digests, and marks one whose findings all contest its position with `contests_only`. `judge_proposals` is the
proposals' judge. It rejects a finding whose claim text does not bear on the position as its title states it, and a
proposal that duplicates an existing stance. A rejection backs every proposal again with its feedback and never
reruns the rewrites. An empty list is approved without a read, and still costs that judge one call.
`list_proposals` keeps an approved proposal only when its stance_id, title and pages are the ones `merge_proposals`
made, so a worker that rewords its copy puts nothing on the card. Its list is the run's output and its
`proposed_stances` artifact, and it writes no page.

A run whose proposals judge spends its last attempt, or cannot decide and is not answered, fails after every rewrite
is written and indexed, and so does one whose backing map aborts. Three consecutive identical failures in any map
abort the run as failed.
