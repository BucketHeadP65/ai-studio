---
title: Link news digests
description: >-
  Links the entities of every digest that has none yet: every claim's subject and speaker and every named thing the
  article mentions to one entity page. Run it after every digest run.
uses: [st.find_undigested.v4, st.digest_source.v11, st.link_entities.v2]
presentation:
  purpose: >-
    Give every digest its entity links. The digest workflow only writes digests, so a digest has no entity links until
    this pass reads it back from its file and links it. It also picks up any digest an earlier pass could not link.
  produces: >-
    Every claim's subject and speaker linked to an entity page and the mentions list naming every entity, on each
    digest that had no mentions link, new entity pages for names the base did not hold, and a count of the digests
    that gained links and the entities they link.
  owner_participation: >-
    You pick the knowledge base. Nothing stops for you once the run starts. Checking the entity pages for duplicates
    afterwards is yours.
  not_for: >-
    Not for writing a digest, which the digest workflow does. Not for linking a digest to earlier digests on the same
    story, which the relate workflow does once every digest is linked. Not for a digest that already carries a mentions
    link, which this pass leaves alone. Not for a digest the digest run has not finished writing: a base that indexes
    once per run finds it only after that run ends.
  inputs:
    - {key: collection, label: Knowledge base, control: knowledge-base}
  result_fields:
    - {label: Digests that gained links (recorded), path: values.report.linked}
    - {label: Entities linked (recorded), path: values.report.entities_linked}
workflow:
  name: news-link
  version: 1
  description: List every active digest of the base with no mentions link, link each one's entities through its file, count the digests that gained links and the entities they link.
  parameters:
    collection:
      description: The knowledge base to work in, by collection name.
      json_schema: {type: string, minLength: 1}
  inline:
    - name: list_unlinked_digests
      source: |
        pages = await list_pages(knowledge_base=collection, page_type="digest", status="active", limit=1000, lacking_link="mentions")
        [{"digest_id": p["page_id"], "status": "written"} for p in pages]
      inputs:
        collection: {json_schema: {type: string}}
      output: {like: digest_source, many: true}
      tools: [list_pages]
    - name: count_linked_digests
      source: |
        {"linked": len([d for d in digests if d["entities_linked"] > 0]), "entities_linked": sum(d["entities_linked"] for d in digests)}
      inputs:
        digests: {like: link_entities, many: true}
      output:
        json_schema:
          type: object
          title: LinkedDigestCount
          properties:
            linked: {type: integer}
            entities_linked: {type: integer}
          required: [linked, entities_linked]
  result:
    summary: Entity links on every digest that had none.
    primary_work: link_digests
    empty_primary: noop
    artifacts:
      - id: pass_report
        label: What the pass recorded
        source: {node: report}
        media_type: application/json
  nodes:
  - id: list_digests
    kind: run
    work: list_unlinked_digests
    inputs:
      collection: {parameter: collection}
  - id: link_digests
    each: digest
    kind: map
    over: {node: list_digests}
    turns: [link_entities]
    inputs:
      collection: {parameter: collection}
    on_error: skip
    max_parallel: 12
    abort_after_repeats: 3
  - id: report
    kind: run
    work: count_linked_digests
    inputs:
      digests: {node: link_digests}
  output: {node: report}
---

Use by hand on a news base after every digest run has ended, and before the relate workflow, so every digest carries
its entity links when the relate pass reads it. A base that indexes once per run indexes the digests only when the
digest run ends, and this pass lists only indexed digests.

The stages: `list_digests` lists every active digest page of the base that has no mentions link, up to 1000, skipped
digests left out. `link_digests` runs one worker per digest, up to twelve in flight at once. Each reads the digest with
read_page as a ready write_note payload, sends every name it needs linked to resolve_entities in one call, sets the link
fields on that payload and writes it back, so nothing else on the page changes. `report` counts the digests that gained
links and the entities they link. A digest whose turn fails, or that read_page cannot read whole, is left out of every
count, stays unlinked, and the next run of this pass lists it again. Three consecutive identical failures abort the run
as failed. A base that indexes once per run indexes every page this run wrote when the run ends.
