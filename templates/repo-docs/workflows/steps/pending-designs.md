---
title: List pending designs
summary: The executed designs in one base that no ADR absorbs yet, from the base's promote-initiative policy entry.
version: 1
step:
  name: pending_designs
  source: |
    work = await compounding_work(knowledge_base=collection, name="promote-initiative")
    [{"page_id": r["page_id"], "title": r["title"]} for r in work["rows"]]
  inputs:
    collection: {json_schema: {type: string}}
  output:
    json_schema:
      type: array
      items:
        type: object
        title: PendingDesign
        properties:
          page_id: {type: string}
          title: {type: string}
        required: [page_id, title]
  tools: [compounding_work]
---

Use at the head of a promote initiative pass. Do not use to list every design page in the base, only the ones the
promote-initiative policy entry admits this run, capped by that entry, so a pass stays one batch.
