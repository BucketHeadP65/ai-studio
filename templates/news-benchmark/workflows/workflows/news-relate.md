---
title: Relate news digests
description: >-
  Links every digest of a news base to the earlier digests that report the same story, each link justified from both
  pages, once every article has its digest.
uses: [st.relate_digest.v2]
presentation:
  purpose: >-
    Let a question that spans several articles walk from a later report of a story to the earlier ones. Digests are
    written many at once and indexed only when their run ends, so while one is written it cannot find the other
    articles on its story. This pass runs after every digest exists and is indexed, and relates them all.
  produces: >-
    Follow-up links from each digest to the earlier digests on the same story, each added only when one sentence that
    holds from both pages justifies it. A link already on a digest is never removed, so running the pass again only
    adds what it finds.
  owner_participation: >-
    You pick the knowledge base. Nothing stops for you once the run starts. Reading the links afterwards, and removing
    one you disagree with by hand, is yours.
  not_for: >-
    Not for linking a digest to its entities, which the link workflow, news-link, does after each digest run. Not for a
    base whose digests are not all written, linked and indexed yet, whose newest articles would find nothing to follow
    up. Not for editing the pages a digest links to.
  inputs:
    - {key: collection, label: Knowledge base, control: knowledge-base}
  result_fields:
    - {label: Digests read (recorded), path: values.report.related}
    - {label: Digests that gained a link (recorded), path: values.report.linked}
    - {label: Links added (recorded), path: values.report.edges_added}
workflow:
  name: news-relate
  version: 2
  description: List every active digest of the base, relate each one to the earlier digests on the same story through the schema's own link fields, count the digests read and the links added.
  parameters:
    collection:
      description: The knowledge base to work in, by collection name.
      json_schema: {type: string, minLength: 1}
  inline:
    - name: list_digests
      source: |
        pages = await list_pages(knowledge_base=collection, page_type="digest", status="active", limit=1000)
        [p["page_id"] for p in pages]
      inputs:
        collection: {json_schema: {type: string}}
      output:
        json_schema:
          type: array
          items: {type: string}
      tools: [list_pages]
    - name: count_related_digests
      source: |
        linked = [r for r in results if r["edges_added"] > 0]
        {"related": len(results), "linked": len(linked), "edges_added": sum(r["edges_added"] for r in results)}
      inputs:
        results: {like: relate_digest, many: true}
      output:
        json_schema:
          type: object
          title: RelatedDigestCount
          properties:
            related: {type: integer}
            linked: {type: integer}
            edges_added: {type: integer}
          required: [related, linked, edges_added]
  result:
    summary: Follow-up links from each digest to the earlier digests on the same story.
    primary_work: relate_digests
    empty_primary: noop
    artifacts:
      - id: pass_report
        label: What the pass recorded
        source: {node: report}
        media_type: application/json
      - id: relate_results
        label: The links each digest gained
        source: {node: relate_digests}
        media_type: application/json
  nodes:
  - id: list_digests
    kind: run
    work: list_digests
    inputs:
      collection: {parameter: collection}
  - id: relate_digests
    each: digest_id
    kind: map
    over: {node: list_digests}
    turns: [relate_digest]
    inputs:
      collection: {parameter: collection}
    on_error: skip
    max_parallel: 12
    abort_after_repeats: 3
  - id: report
    kind: run
    work: count_related_digests
    inputs:
      results: {node: relate_digests}
  output: {node: report}
---

Use by hand on a news base, once the digest workflow has written every article and the link workflow has linked and
indexed every digest. Run it again after a later digest and link run to relate the new digests too.

The stages: `list_digests` lists every active digest page of the base by id, up to 1000, skipped digests left out.
`relate_digests` runs one worker per digest, up to twelve in flight at once. Each reads the base's profile, searches
for earlier digests on the same story, reads both pages and adds a follow-up link only when one sentence justifies it
from both. A digest whose turn fails is left out and the rest carry on. Three consecutive identical failures abort the
run as failed. `report` counts the digests read, the ones that gained a link and the links added, a failed digest
left out of every count. A base that indexes once per run indexes every link this run wrote when the run ends. How
many links each digest gained is in the run's values under relate_digests.
