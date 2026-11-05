---
title: Refresh stances from the docs
description: >-
  Reads the records pages that changed, places each against the base's stance pages, rewrites and writes the stances
  they change under a judge, keeping their Evidence and Alternatives, then writes each new stance they propose, with
  the findings on it, as a pending flag.
uses:
  - st.list_stance_changes.v2
  - st.place_stance_pages.v6
  - st.refresh_stance.v4
  - st.check_stances.v4
  - st.back_proposal.v4
  - st.judge_proposal.v2
  - st.write_stances.v6
  - st.write_stance_flags.v5
presentation:
  purpose: >-
    Keep every stance page saying what the system does today. A stance holds the system's position on one topic as
    its status, and the records knowledge base is where that position is decided and shipped, so a stance nobody
    refreshes from it goes stale as soon as the work moves on.
  produces: >-
    Each stance a changed records page bears on, rewritten in place: its status following the records, and its
    summary, TL;DR, Position and Why brought up to date with every changed sentence naming the records page behind
    it. Its Evidence and Alternatives stay exactly as they were. A topic no stance covers is proposed as a new stance,
    and never drafted, only when its page takes a position on a question research literature addresses. An
    implementation choice is skipped with a reason that says so. The run card lists the stances written, the ones
    refused with the check each failed, where every changed page was placed, every stance as it was before the run,
    every stance the run could not read, and every placement under a stance the base does not list. A judge reads each
    stance proposal alone and says which of its findings bear. Each proposal left with findings that bear from two of
    the base's digests (none at status skipped) is shown among what the run left behind and written as a pending flag
    of kind stance, `flag.stance-` and the proposed id without `stance.`, with its records pages, placement's reason
    for each, each finding as a claim link with its side and one sentence on how, and each claim the judge refused with
    its reason. One whose findings all contest
    it is marked as one the literature argues against. Every dropped proposal is listed with why. A pending flag is
    written again with newer findings, and a flag you accepted, dismissed or completed, read from its file, is left as
    it is and listed. The proposals start only after every rewrite is written, so a run whose proposals fail keeps its
    rewrites. Each rewrite is written to its file at once. A knowledge base indexed once per run indexes them when the
    run ends. Under the opt-in Temporal engine writes are never held for the run, so even such a knowledge base indexes
    after each write.
  owner_participation: >-
    Nothing during a run, unless a judge cannot decide. A schedule starts it when the records knowledge base is
    indexed, and you start a full pass by hand by leaving the changed pages empty. A judge, not you, rejects a rewrite
    whose sentences no records page supports or whose status the records do not give. A judge per proposal drops a
    finding whose claim does not bear on its position, and a proposal that repeats a stance, without asking you. The
    rewrite judge that cannot decide asks you, and the run waits for your answer, so a schedule over this workflow carries a gate policy that fails after a
    wait (fail_after:<seconds>). Every rewrite lands directly. To undo one, put the page back from the run's stances as
    they were before it. A proposed new stance is yours to draft by hand: accept, dismiss or complete its stance flag
    in the research knowledge base, and the run never writes a flag you decided again.
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
    - {label: "Placements under a stance the base does not list, left out of every rewrite (recorded)", path: values.list_unplaced}
    - {label: "Stance flags written, left as the owner decided them, and refused, with the stance proposals dropped and why, and the placements under no listed stance (recorded)", path: values.write_stance_flags}
workflow:
  name: research-stance-refresh
  version: 20
  description: List the records pages that changed at a status that counts, place them against the stances in groups of five, name every placement under a stance the base does not list, rewrite each existing stance they change as its changes alone, judge every draft, merge each rewrite into the page as it was read and write it, then back every new stance they propose with the claims of the base that support or contest it, keep only the findings code confirms on the base's digests, judge each proposal alone, list every proposal left with findings that bear from two digests with its pages as placement made it and every dropped one with why, and write each listed one as a pending stance flag, drafting no stance and never writing a flag the owner decided.
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
    - name: list_unplaced
      source: |
        unplaced = []
        for answer in placements:
            for placed in answer["placements"]:
                if placed["placement"] == "stance" and placed["stance_id"] not in changes["stance_ids"]:
                    unplaced.append({"page_id": placed["page_id"], "stance_id": placed["stance_id"], "title": placed["title"], "reason": placed["reason"]})
        unplaced
      inputs:
        changes: {like: list_stance_changes}
        placements: {like: place_stance_pages, many: true}
      output:
        json_schema:
          type: array
          items:
            type: object
            title: UnplacedPlacement
            description: A placement under an existing stance whose id no stance page of the base carries, which changes no stance.
            required: [page_id, stance_id, title, reason]
            properties:
              page_id: {type: string}
              stance_id: {type: string}
              title: {type: string}
              reason: {type: string}
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
                    proposals[stance_id] = {"stance_id": stance_id, "title": placed["title"], "pages": [], "reasons": [], "stance_ids": changes["stance_ids"]}
                if placed["page_id"] not in [held["page_id"] for held in proposals[stance_id]["pages"]]:
                    proposals[stance_id]["pages"].append(page)
                    proposals[stance_id]["reasons"].append({"page_id": placed["page_id"], "reason": placed["reason"]})
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
            required: [stance_id, title, pages, reasons, stance_ids]
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
              reasons:
                type: array
                description: The reason placement gave for each page, by its page id.
                items:
                  type: object
                  title: PlacementReason
                  required: [page_id, reason]
                  properties:
                    page_id: {type: string}
                    reason: {type: string}
              stance_ids:
                type: array
                description: The ids of every stance the base lists, which a judge's duplicate must name.
                items: {type: string}
    - name: check_findings
      source: |
        digest_types = ["paper", "talk", "article", "post"]
        read = {}
        kept = []
        for backing in backings:
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
        listed = []
        dropped = []
        for made in proposals:
            wanted = {page["page_id"]: True for page in made["pages"]}
            matching = []
            for held in backings:
                if held["stance_id"] != made["stance_id"] or held["title"] != made["title"]:
                    continue
                if {page["page_id"]: True for page in held["pages"]} != wanted:
                    continue
                matching.append(held)
            if not matching:
                reason = "No backing of it passed the code checks: its worker failed, changed the proposal's id, title or pages, or cited claims from fewer than two digests the base holds."
                dropped.append({"stance_id": made["stance_id"], "title": made["title"], "reasons": [reason]})
                continue
            verdicts = {}
            duplicate = ""
            judged = False
            for judgement in judgements:
                if judgement["stance_id"] != made["stance_id"] or judgement["title"] != made["title"]:
                    continue
                if {page["page_id"]: True for page in judgement["pages"]} != wanted:
                    continue
                judged = True
                named = judgement.get("duplicates") or ""
                if named and named != made["stance_id"] and named in made["stance_ids"]:
                    duplicate = named
                for verdict in judgement["findings"]:
                    claim = verdict["claim"].strip()
                    if claim.startswith("[[") and claim.endswith("]]"):
                        claim = claim[2:-2]
                    claim = claim.split("|")[0].strip()
                    if "/" in claim and ":" in claim.split("/")[0]:
                        claim = claim.split("/", 1)[1]
                    if "#" in claim:
                        page_id, anchor = claim.split("#", 1)
                        anchor = anchor.strip()
                        if not anchor.startswith("^"):
                            anchor = "^" + anchor
                        claim = page_id.strip() + "#" + anchor
                    if claim not in verdicts:
                        verdicts[claim] = []
                    verdicts[claim].append(verdict)
            if not judged:
                reason = "The judge gave no verdict: its judge item failed or was skipped before it answered."
                dropped.append({"stance_id": made["stance_id"], "title": made["title"], "reasons": [reason]})
                continue
            if duplicate:
                reason = "The judge found it repeats the existing stance " + duplicate + "."
                dropped.append({"stance_id": made["stance_id"], "title": made["title"], "reasons": [reason]})
                continue
            order = []
            first = {}
            sides = {}
            for held in matching:
                for finding in held["findings"]:
                    claim = finding["claim"]
                    if claim not in first:
                        order.append(claim)
                        first[claim] = finding
                        sides[claim] = {}
                    sides[claim][finding["side"]] = True
            findings = []
            reasons = []
            refused_claims = []
            for claim in order:
                if len(sides[claim]) > 1:
                    reasons.append(claim + " was given opposite sides by its backings.")
                    continue
                said = verdicts.get(claim, [])
                if not said:
                    reasons.append(claim + " has no verdict from the judge.")
                    continue
                refused = [verdict for verdict in said if not verdict["bears"]]
                if refused:
                    reasons.append(claim + " does not bear: " + refused[0]["why"])
                    refused_claims.append({"claim": claim, "why": refused[0]["why"]})
                    continue
                findings.append(first[claim])
            digests = {finding["claim"].split("#")[0]: True for finding in findings}
            if len(digests) < 2:
                dropped.append({"stance_id": made["stance_id"], "title": made["title"], "reasons": ["Fewer than two digests are left with findings that bear."] + reasons})
                continue
            contests_only = True
            for finding in findings:
                if finding["side"] != "contests":
                    contests_only = False
            listed.append({"stance_id": made["stance_id"], "title": made["title"], "pages": made["pages"], "reasons": made["reasons"], "findings": findings, "refused": refused_claims, "contests_only": contests_only})
        {"proposals": listed, "dropped": dropped}
      inputs:
        proposals: {like: merge_proposals}
        backings: {like: check_findings}
        judgements: {like: judge_proposal, many: true}
      output:
        json_schema:
          type: object
          title: StanceProposalsListed
          required: [proposals, dropped]
          properties:
            proposals:
              type: array
              description: Each stance proposal that keeps findings that bear from at least two digests.
              items:
                type: object
                title: ListedStanceProposal
                required: [stance_id, title, pages, reasons, findings, refused, contests_only]
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
                  reasons:
                    type: array
                    items:
                      type: object
                      title: PlacementReason
                      required: [page_id, reason]
                      properties:
                        page_id: {type: string}
                        reason: {type: string}
                  findings:
                    type: array
                    items:
                      type: object
                      title: CheckedFinding
                      required: [claim, claim_text, side, how]
                      properties:
                        claim: {type: string}
                        claim_text: {type: string}
                        side:
                          enum: [supports, contests]
                          type: string
                        how: {type: string}
                  refused:
                    type: array
                    description: Each claim of its backings the judge said does not bear, with the judge's reason.
                    items:
                      type: object
                      title: RefusedFinding
                      required: [claim, why]
                      properties:
                        claim: {type: string}
                        why: {type: string}
                  contests_only: {type: boolean}
            dropped:
              type: array
              description: Each stance proposal the run dropped, with why.
              items:
                type: object
                title: DroppedStanceProposal
                required: [stance_id, title, reasons]
                properties:
                  stance_id: {type: string}
                  title: {type: string}
                  reasons:
                    type: array
                    items: {type: string}
  result:
    summary: The stances the changed records pages bear on, rewritten and written, with each refused page named beside them and each new stance they propose listed with its pages and the findings from the literature that take a side on it, and written as a pending stance flag.
    primary_work: refresh_stances
    empty_primary: noop
    required_gates:
      - {gate_id: check_stances, accepted_sources: [judge]}
    artifacts:
      - id: proposed_stances
        label: New stances proposed, each with the records pages behind it and the findings from the literature that bear on it
        source: {node: list_proposals}
        field: proposals
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
      id: list_unplaced
      work: list_unplaced
      inputs:
        changes: {node: list_changes}
        placements: {node: place_pages}
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
    - kind: map
      id: judge_proposals
      turns: [judge_proposal]
      over: {node: check_findings}
      each: backing
      inputs:
        collection: {parameter: collection}
      max_parallel: 4
      on_error: skip
      abort_after_repeats: 3
    - kind: run
      id: list_proposals
      work: list_proposals
      inputs:
        proposals: {node: merge_proposals}
        backings: {node: check_findings}
        judgements: {node: judge_proposals}
    - kind: run
      id: write_stance_flags
      work: write_stance_flags
      inputs:
        listed: {node: list_proposals}
        unplaced: {node: list_unplaced}
        collection: {parameter: collection}
  output: {node: write_stance_flags}
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
could not. `list_unplaced` names every placement under an existing stance whose id no stance page of the base
carries, such as a misspelt one, with its page, the id, the title and the reason. Such a placement changes no stance
and proposes none, and the run's output carries it.

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
digests, and marks one whose findings all contest its position with `contests_only`. `judge_proposals` runs one judge
per backing, four at a time, and never judges the list as a whole. Each judge answers, per finding by its claim,
whether it bears on the position as the title states it and one sentence on why, and for the proposal the id of an
existing stance it repeats, or null. It copies the proposal's id, title and pages. It reads with `read_page` and
`search` and writes nothing. A judge item that
fails is skipped and drops only its own proposal, while three judge items in a row failing with the same error abort
the run as failed, after every rewrite is written, so a broken judge model never reads as a run with no proposals.
There is no redo: `back_proposals` runs once, and a weak finding is dropped, not repaired. With no proposal the map runs nothing, so the proposals make no model call.

`list_proposals` accounts for every proposal `merge_proposals` made, in this order. A proposal with no backing of the
same stance_id and title and the same records pages in any order is dropped, since its backing worker failed, changed
its copy, or code left it claims from fewer than two digests. A judge's answer counts for a proposal only when it
carries the same stance_id and title and the same records pages, the match a backing gets, so a judge of a copy under
another title never touches the honest proposal. One with no such answer is dropped, since its judge item failed. One
its judge says repeats a stance is dropped, but only when the id it names is a stance the base lists other than the
proposal's own, which `merge_proposals` carries on each proposal as `stance_ids`. Of the rest, each claim of its
matching backings is kept once, a claim two backings give opposite sides is left out, and a claim is kept only when
every judge that answered it says it bears. A judge's claim written as a wikilink, a search ref or an anchor with no
caret counts as its bare reference, as `check_findings` counts a backing's. A proposal left with findings from fewer
than two digests is dropped with each finding's reason. A kept proposal carries each claim its judge refused, with the
reason, as `refused`, and `contests_only` counts only the findings that remain. The map collects its answers as they finish, so
nothing is paired by position. A backing that matches no proposal, such as one whose worker reworded its copy, added or
left out a page, or took another proposal's id, puts nothing on the card, and the honest backing of that proposal
still does. It answers `proposals`, the run's `proposed_stances` artifact, and `dropped`, each dropped proposal with
its reasons, and writes no page.

`write_stance_flags` writes each listed proposal to the research knowledge base as a flag page, `flag.stance-` and the
proposed id without `stance.`, at status `pending` and of kind `stance`, in the flag type's own folder. Its title is
the proposed stance and `about` links each digest a finding cites. Why names each records page by its id in plain text
with the reason placement gave, because a link into the records knowledge base would not resolve here. Proposal says
that accepting the flag asks for a new stance page. Evidence lists each finding as a claim link, such as
`[[paper.ace-2025#^c8]]`, with its side and its sentence, then, under "Refused by the judge", each claim the judge
refused, as a claim link with the judge's reason. A flag whose findings all contest the stance says so in its summary. It lists the base's flags first and decides on each listed flag from its file, read with `read_page`, never
from the index, so a decision the owner made before the base was indexed again still holds. A flag of another kind
under the same id, such as an idea flag whose title makes the same id, is left as it is and named in `failed`. A flag
whose file is not at `pending`, one the owner accepted, dismissed or completed, is never written and is answered in
`kept_decided`. A pending stance flag is written again at the path it was read from, so a flag the owner moved stays
one file, with the newer findings and its `created` date. A listed flag `read_page` refuses is named in `failed` and
not written. A flag the index does not list is written to `Wiki/Flags`, at the very path `read_notes` checked, unless
a file already sits there, which is named in `failed` and left as it is. A write the base refuses is named in `failed` with the reason
and never fails the run, and when the flags cannot be listed no flag is written. Its answer, with the dropped proposals
as `dropped` and the placements `list_unplaced` named as `unplaced` beside the flags, is the run's output.

A run whose backing map or judge map aborts fails after every rewrite is written. Each rewrite is written to its file
at once. A knowledge base indexed on each write indexes it then. One indexed once per run indexes the run's writes when the run
ends, fails or pauses. No step after `write_stances` waits for a person, so nothing written waits on one. Under the
opt-in Temporal engine writes are never held for the run, so even a knowledge base indexed
once per run indexes after each write. Three consecutive identical failures in any map, the judge map among them,
abort the run as failed.
