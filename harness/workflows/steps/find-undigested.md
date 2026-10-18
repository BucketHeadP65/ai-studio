---
title: Find undigested sources
summary: The source mirrors in one base that no digest links yet, newest first, or exactly the ones the caller picked.
version: 4
step:
  name: find_undigested
  source: |
    picked = [s.strip() for s in picked_sources.split(",") if s.strip() != ""]
    if picked == []:
        work = await compounding_work(knowledge_base=collection, name="digest-sources")
        rows = [{"source_id": r["source_id"], "item_type": r["item_type"], "title": r["title"]} for r in work["rows"]]
    else:
        undigested = await list_undigested(knowledge_base=collection, source_ids=picked)
        rows = [{"source_id": r["source_id"], "item_type": r["item_type"], "title": r["title"]} for r in undigested]
    rows
  inputs:
    collection: {json_schema: {type: string}}
    picked_sources: {json_schema: {type: string}}
  output:
    json_schema:
      type: array
      items:
        type: object
        title: Undigested
        properties:
          source_id: {type: string}
          item_type: {type: string, enum: [video, paper, article, post]}
          title: {type: string}
        required: [source_id, item_type, title]
  tools: [compounding_work, list_undigested]
---

Use at the head of any pass over new sources in one base. With `picked_sources` empty it answers only the mirrors
the base's `digest-sources` policy entry admits this run, capped by that entry, so a pass stays one batch. With
`picked_sources` set (source ids, comma-separated) it answers exactly those, taken from the whole undigested list and
free of the policy cap: the caller chose, so the choice is the batch. An id that is not an undigested mirror is left
out silently, which also keeps a re-run from digesting a source twice. Do not use to list every mirror.
