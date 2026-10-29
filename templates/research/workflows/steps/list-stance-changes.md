---
title: List stance changes
summary: The records pages at a status that counts, listed whole or narrowed to the changed ids, beside every stance page as it stood before the run and the stances it could not read.
version: 1
step:
  name: list_stance_changes
  source: |
    pairs = []
    for entry in progress.split(","):
        if ":" in entry:
            page_type, status = entry.split(":", 1)
            pair = page_type.strip() + ":" + status.strip()
            if pair not in pairs:
                pairs.append(pair)
    wanted = []
    for page_id in changed_page_ids.split(","):
        if page_id.strip() and page_id.strip() not in wanted:
            wanted.append(page_id.strip())
    admitted = []
    for pair in pairs:
        page_type, status = pair.split(":", 1)
        for row in await list_pages(knowledge_base=records, page_type=page_type, status=status, limit=1000):
            if not wanted or row["page_id"] in wanted:
                admitted.append({"page_id": row["page_id"], "page_type": row["page_type"], "status": row["status"], "title": row["title"]})
    qualified = len(admitted)
    capped = len(wanted) > 0 and qualified > 25
    if capped:
        admitted = []
    carried = ["id", "type", "title", "summary", "status", "record", "synthesizes", "recommends", "advises_against", "aliases", "superseded_by", "decision_reason"]
    stances = []
    stance_ids = []
    unread = []
    for row in await list_pages(knowledge_base=collection, page_type="stance", limit=1000):
        stance_ids.append(row["page_id"])
        try:
            page = await read_page(knowledge_base=collection, page_id=row["page_id"])
        except Exception as error:
            unread.append({"stance_id": row["page_id"], "record": "", "reason": "read_page refused the page: " + str(error)})
            continue
        front = page["payload"]["frontmatter"]
        others = [key for key in front if key not in carried]
        if others:
            unread.append({"stance_id": row["page_id"], "record": str(front.get("record") or ""), "reason": "Its frontmatter carries " + ", ".join(others) + ", which this workflow cannot write back unchanged."})
            continue
        stances.append({"stance_id": row["page_id"], "title": front["title"], "status": front["status"], "path": page["path"], "record": front.get("record") or "", "payload": {"frontmatter": front, "sections": page["payload"]["sections"]}})
    {"admitted": admitted, "stances": stances, "stance_ids": stance_ids, "unread": unread, "qualified": qualified, "capped": capped}
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
      required: [admitted, stances, stance_ids, unread, qualified, capped]
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
        stance_ids:
          type: array
          items: {type: string}
        unread:
          type: array
          items:
            type: object
            title: UnreadStance
            required: [stance_id, record, reason]
            properties:
              stance_id: {type: string}
              record: {type: string}
              reason: {type: string}
        qualified: {type: integer}
        capped: {type: boolean}
  tools: [read_page, list_pages]
---

Use at the head of a stance refresh. `progress` names the page types and statuses that count, as comma-separated
`type:status` pairs, and a pair named twice is listed once. It lists every page of `records` at each pair and reads no
records page file. With `changed_page_ids` set, it keeps only the listed pages those ids name, so a changed page at no
pair, or one the records knowledge base does not hold, is not kept. Past twenty-five kept pages it keeps none and
answers `capped` true with the count in `qualified`, so the run touches nothing and a full pass is started by hand.
With `changed_page_ids` empty, it keeps every listed page, with no cap. A `records` name list_pages refuses fails the
run. Beside them it reads every stance page
of `collection` whole, as it stands before the run, so a later step can check a rewrite against it and the run keeps
it. `stance_ids` names every stance listed, read or not. A stance read_page refuses, or one carrying a frontmatter
field this step does not declare, is left out of `stances` and named in `unread` with the reason, because its
rewrite could not be checked or could not keep that field. Its `record` is given when the page was read and is
empty when read_page refused it.
