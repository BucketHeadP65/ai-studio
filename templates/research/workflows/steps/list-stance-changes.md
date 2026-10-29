---
title: List stance changes
summary: The records pages at a status that counts, read from the changed ids or listed whole, beside every stance page as it stood before the run.
version: 1
step:
  name: list_stance_changes
  source: |
    pairs = []
    for entry in progress.split(","):
        if ":" in entry:
            page_type, status = entry.split(":", 1)
            pairs.append(page_type.strip() + ":" + status.strip())
    wanted = []
    for page_id in changed_page_ids.split(","):
        if page_id.strip() and page_id.strip() not in wanted:
            wanted.append(page_id.strip())
    admitted = []
    if wanted:
        for page_id in wanted:
            try:
                page = await read_page(knowledge_base=records, page_id=page_id)
            except Exception:
                continue
            front = page["payload"]["frontmatter"]
            if str(front.get("type")) + ":" + str(front.get("status")) in pairs:
                admitted.append({"page_id": page_id, "page_type": front["type"], "status": front["status"], "title": str(front.get("title", ""))})
    else:
        for pair in pairs:
            page_type, status = pair.split(":", 1)
            for row in await list_pages(knowledge_base=records, page_type=page_type, status=status, limit=1000):
                admitted.append({"page_id": row["page_id"], "page_type": row["page_type"], "status": row["status"], "title": row["title"]})
    qualified = len(admitted)
    capped = len(wanted) > 0 and qualified > 25
    if capped:
        admitted = []
    carried = ["id", "type", "title", "summary", "status", "record", "synthesizes", "recommends", "advises_against", "aliases", "superseded_by", "decision_reason"]
    stances = []
    for row in await list_pages(knowledge_base=collection, page_type="stance", limit=1000):
        try:
            page = await read_page(knowledge_base=collection, page_id=row["page_id"])
        except Exception:
            continue
        front = page["payload"]["frontmatter"]
        others = [key for key in front if key not in carried]
        if others:
            raise RuntimeError(row["page_id"] + " carries frontmatter this workflow cannot write back unchanged: " + ", ".join(others))
        stances.append({"stance_id": row["page_id"], "title": front["title"], "status": front["status"], "path": page["path"], "record": front.get("record") or "", "payload": {"frontmatter": front, "sections": page["payload"]["sections"]}})
    {"admitted": admitted, "stances": stances, "qualified": qualified, "capped": capped}
  inputs:
    collection:
      json_schema: {type: string}
    records:
      json_schema: {type: string}
    progress:
      json_schema: {type: string}
    changed_page_ids:
      json_schema: {type: string}
  output:
    json_schema:
      type: object
      title: StanceChanges
      required: [admitted, stances, qualified, capped]
      properties:
        admitted:
          type: array
          items:
            type: object
            title: ChangedPage
            required: [page_id, page_type, status, title]
            properties:
              page_id: {type: string}
              page_type: {type: string}
              status: {type: string}
              title: {type: string}
        stances:
          type: array
          items:
            type: object
            title: StanceAsRead
            required: [stance_id, title, status, path, record, payload]
            properties:
              stance_id: {type: string}
              title: {type: string}
              status: {type: string}
              path: {type: string}
              record: {type: string}
              payload:
                type: object
                title: StancePayload
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
        qualified: {type: integer}
        capped: {type: boolean}
  tools: [read_page, list_pages]
---

Use at the head of a stance refresh. `progress` names the page types and statuses that count, as comma-separated
`type:status` pairs. With `changed_page_ids` set, it reads exactly those pages of `records` and keeps the ones whose
type and status form a pair, skipping a page read_page cannot find. Past twenty-five kept pages it keeps none and
answers `capped` true with the count in `qualified`, so the run touches nothing and a full pass is started by hand.
With `changed_page_ids` empty, it lists every page of every pair, with no cap. Beside them it reads every stance page
of `collection` whole, as it stands before the run, so a later step can check a rewrite against it and the run keeps
it. A stance carrying a frontmatter field this step does not declare fails the run naming it, because its rewrite
could not keep that field.
