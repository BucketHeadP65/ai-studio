---
title: Recent digests
summary: The digest pages written in one base within a window, or exactly the ids named by `digest_ids` whatever their age, each with its source, its file and its validation findings, grouped into batches of five, skipped ones excluded.
version: 5
step:
  name: recent_digests
  source: |
    wanted = [s.strip() for s in digest_ids.split(",") if s.strip()] if digest_ids else []
    if wanted:
        work = await compounding_work(knowledge_base=collection, name="flag-ideas", page_ids=wanted)
    else:
        work = await compounding_work(knowledge_base=collection, name="flag-ideas")
    digests = [{"digest_id": r["page_id"], "page_type": r["page_type"], "title": r["title"], "source_id": r["source_id"], "path": r["path"], "findings": r["findings"]} for r in work["rows"]]
    size = 5
    groups = []
    i = 0
    gid = 0
    while i < len(digests):
        groups.append({"group_id": gid, "members": digests[i:i + size]})
        gid += 1
        i += size
    groups
  inputs:
    collection:
      json_schema: {type: string}
    digest_ids:
      json_schema: {type: string}
  output:
    json_schema:
      type: array
      items:
        type: object
        title: DigestGroup
        required: [group_id, members]
        properties:
          members:
            type: array
            items:
              type: object
              title: Digest
              required: [digest_id, page_type, title, source_id, path, findings]
              properties:
                page_type: {type: string}
                title: {type: string}
                digest_id: {type: string}
                source_id: {type: string}
                path: {type: string}
                findings:
                  type: array
                  items: {type: string}
          group_id: {type: integer}
  tools: [compounding_work]
---

Use at the head of a pass over what a base learned recently. It is a query, not a search: the base's `flag-ideas` policy entry decides the window and the cap, newest first. With `digest_ids` set it reads exactly those digests instead, whatever their age and whether a flag already cites them, because the caller chose them. Each digest carries the source it digests, its file and the rule names of its standing validation errors. Groups the result into batches of five so `flag_ideas` can run one worker per group. Do not use for a full inventory of digests: the window is the point.
