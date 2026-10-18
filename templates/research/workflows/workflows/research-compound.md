---
title: Flag ideas from new digests
description: >-
  Reads the digests a knowledge base has gained recently, repairs the defects it finds in the base's own pages, and
  proposes what the owner should see, each proposal evidenced from the base's own pages and left pending for the
  owner to decide on.
uses: [st.recent_digests.v5, st.flag_ideas.v6, st.write_flags.v3, st.merge_flags.v2, st.repair_digest.v1, st.finish.v2]
presentation:
  purpose: >-
    Turn what a knowledge base has just learned into a short list of things the owner should decide on, rather
    than leaving it sitting in the digests for nobody to read.
  produces: >-
    One pending flag page per proposal that survived the merge and the gate. Each names the digests it came from,
    says why it matters for this base's purpose, states one concrete proposal, and cites its evidence as links
    into the base. Beside them, one repair per source whose digest pages the index or the gate found defective: its
    pages skipped with a reason, its claims rewritten, or its pages rewritten, all listed on the run card.
  owner_participation: >-
    You pick the knowledge base, and the exact digests when you want only some of them. The run itself is
    unattended, and an orchestrator gate, not you, drops the proposals that restate a digest or name no
    evidence. Every flag it writes opens as pending, so deciding on them afterwards is yours and nobody else's. A
    repair the run must not do, such as removing a source, or could not finish is named on the run card and counted
    in the one notification. A run that only repairs does not notify.
  not_for: >-
    Not for changing a flag's status, which is the owner's own act on the page. Not for a base with no digests
    yet, which has nothing to read. Not for re-reading digests that have already been flagged, which the batch
    leaves out by itself.
  inputs:
    - {key: collection, label: Knowledge base, control: knowledge-base}
    - {key: digest_ids, label: Only these digests, control: text}
  result_fields:
    - {label: Flags written (recorded), path: values.write_flags}
    - {label: Repairs (recorded), path: values.repair_sources}
    - {label: Repairs that need you (recorded), path: values.count_owner_repairs}
  actions:
    - kind: record_owner_review
      id: flags_read
      label: Record that you have read the flags
      artifact_ids: [approved_flags]
workflow:
  name: research-compound
  version: 12
  description: Propose flags and defects over new digests in parallel groups of five, merge, gate, then write the flags and repair the defects side by side, and notify. digest_ids names exact digests, whatever their age.
  parameters:
    collection:
      description: The knowledge base to work in, by collection name.
      json_schema: {type: string, minLength: 1}
    digest_ids:
      description: Optional comma-separated digest page ids. Empty means every digest the flag-ideas batch holds. Set means exactly those digests, read whatever their age and whether a flag already cites them (a skipped page is left out).
      json_schema: {type: string}
      required: false
  inline:
    - name: list_repairs
      source: |
        members = {}
        for g in groups:
            for m in g["members"]:
                members[m["digest_id"]] = m
        order = ["remove_source", "off_purpose", "duplicate", "empty", "misquoted", "mislabelled_claim"]
        raised = []
        for d in approved["defects"]:
            for pid in d["page_ids"]:
                raised.append({"page_id": pid, "defect": d["defect"], "evidence": d["evidence"]})
        for m in members.values():
            if "quote-not-in-source" in m["findings"]:
                raised.append({"page_id": m["digest_id"], "defect": "misquoted", "evidence": "The index stands with quote-not-in-source on " + m["digest_id"] + "."})
        items = {}
        for r in raised:
            m = members.get(r["page_id"])
            key = m["source_id"] if m and m["source_id"] else r["page_id"]
            if key not in items:
                siblings = [x for x in members.values() if m and m["source_id"] and x["source_id"] == m["source_id"]] if m else []
                pages = siblings if siblings else ([m] if m else [])
                items[key] = {"source_id": m["source_id"] if m else "", "page_ids": [], "pages": [{"page_id": x["digest_id"], "path": x["path"]} for x in pages], "defect": r["defect"], "evidence": []}
            item = items[key]
            if r["page_id"] not in item["page_ids"]:
                item["page_ids"].append(r["page_id"])
            if order.index(r["defect"]) < order.index(item["defect"]):
                item["defect"] = r["defect"]
            if r["evidence"] not in item["evidence"]:
                item["evidence"].append(r["evidence"])
        for m in members.values():
            key = m["source_id"] if m["source_id"] else m["digest_id"]
            for rule in m["findings"]:
                if rule != "quote-not-in-source" and key in items:
                    items[key]["evidence"].append("The index also stands with " + rule + " on " + m["digest_id"] + ".")
        [{"source_id": it["source_id"], "page_ids": it["page_ids"], "pages": it["pages"], "defect": it["defect"], "evidence": " ".join(it["evidence"])} for it in items.values()]
      inputs:
        approved: {like: flag_ideas}
        groups: {like: recent_digests}
      output:
        json_schema:
          type: array
          items:
            type: object
            title: RepairItem
            required: [source_id, page_ids, pages, defect, evidence]
            properties:
              source_id: {type: string}
              page_ids:
                type: array
                items: {type: string}
              pages:
                type: array
                items:
                  type: object
                  title: RepairPage
                  required: [page_id, path]
                  properties:
                    page_id: {type: string}
                    path: {type: string}
              defect:
                enum: [off_purpose, misquoted, mislabelled_claim, empty, duplicate, remove_source]
                type: string
              evidence: {type: string}
    - name: count_owner_repairs
      source: |
        needing = [r for r in results if r["outcome"] != "repaired"]
        len(needing) + max(0, len(items) - len(results))
      inputs:
        items: {like: list_repairs}
        results: {like: repair_digest, many: true}
      output:
        json_schema: {type: integer}
  result:
    summary: The flags the gate approved, written as pending pages for the owner to decide on, and the repairs made to the base's own pages.
    primary_work: propose_flags
    empty_primary: noop
    required_gates:
      - {gate_id: check_flags, accepted_sources: [orchestrator]}
    artifacts:
      - id: approved_flags
        label: The flags the gate approved
        source: {node: check_flags}
        field: flags
        media_type: application/json
  nodes:
  - id: list_digests
    kind: run
    work: recent_digests
    inputs:
      collection: {parameter: collection}
      digest_ids: {parameter: digest_ids}
  - id: propose_flags
    each: group
    kind: map
    over: {node: list_digests}
    turns: [flag_ideas]
    inputs:
      collection: {parameter: collection}
    on_error: skip
    max_parallel: 4
    abort_after_repeats: 3
  - id: check_flags
    kind: orchestrator-gate
    charge: Review the merged object's two lists. Reject any flag that only restates a digest, names no evidence, or proposes something the base already holds (search it before deciding). Reject the object, naming each item to move, when a flag's proposal would only edit our own pages (it is a defect) or a defect is really a claim about the subject (it is a flag). A flag that cites a defective page stays a flag, since the page's content is still evidence. Reject a defect whose evidence does not show the page misstating its own source or falling outside the base's purpose, or whose page_ids are not digests this run read. Approve the rest, and say which items you dropped or moved and why.
    reviews: {node: merge_candidates}
    rejected_to: merge_candidates
  - id: write_flags
    kind: run
    work: write_flags
    inputs:
      approved: {node: check_flags}
      collection: {parameter: collection}
  - id: list_repairs
    kind: run
    work: list_repairs
    inputs:
      approved: {node: check_flags}
      groups: {node: list_digests}
  - id: repair_sources
    each: item
    kind: map
    over: {node: list_repairs}
    turns: [repair_digest]
    inputs:
      collection: {parameter: collection}
    on_error: skip
    max_parallel: 2
    abort_after_repeats: 3
  - id: count_owner_repairs
    kind: run
    work: count_owner_repairs
    inputs:
      items: {node: list_repairs}
      results: {node: repair_sources}
  - id: notify
    kind: run
    work: notify_owner
    inputs:
      written: {node: write_flags}
      needs_you: {node: count_owner_repairs}
      collection: {parameter: collection}
  - id: merge_candidates
    kind: run
    work: merge_flags
    inputs:
      candidates: {node: propose_flags}
      collection: {parameter: collection}
  - id: finish
    kind: run
    work: finish
    inputs:
      notified: {node: notify}
  output: {node: finish}
---

Use on a daily schedule after the digest pass, or by hand right after a targeted digest fire with digest_ids set to
those digests. Not for updating a flag's status. That is the owner's act.

The stages: `list_digests` takes the batch the base's flag-ideas policy entry admits, or exactly the digests named,
and groups it into fives. `propose_flags` runs one worker per group, four groups in flight at once, each reading its
own slice against what the base already holds. `merge_candidates` deduplicates and combines across groups, which is
where a connection no single group could see gets proposed. `check_flags` is the gate, and a rejection sends the
merge round again rather than the proposals.
Behind the gate two branches run side by side. `write_flags` writes the approved flags, every page pending.
`list_repairs` turns the approved defects, and every digest the index found misquoting its source, into one repair per
source, and `repair_sources` runs one worker per repair, two at a time, each skipping, rewriting or naming its source for the
owner. `count_owner_repairs` counts the repairs that need the owner, `notify` tells the owner when a flag was written or a
repair needs them, and `finish` closes the run. A repair whose worker fails is left out and still counted. Three
consecutive identical failures in either map abort the run as failed.
