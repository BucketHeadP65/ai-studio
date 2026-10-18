---
title: Write flags
summary: Every approved flag of the gate's object becomes a pending flag page, and the count is returned. Defects are never written here.
version: 3
step:
  name: write_flags
  source: |
    ids = []
    for f in approved["flags"]:
        slug = "".join(c if c.isalnum() else "-" for c in f["title"].lower()).strip("-")[:60]
        payload = {
            "frontmatter": {"id": "flag." + slug, "type": "flag", "title": f["title"], "summary": f["summary"], "status": "pending", "flag_kind": f["flag_kind"], "about": ["[[" + a + "]]" for a in f["about"]]},
            "sections": [{"heading": "Why", "body": f["why"]}, {"heading": "Proposal", "body": f["proposal"]}, {"heading": "Evidence", "body": f.get("evidence", "")}],
        }
        ids.append(await write_note(knowledge_base=collection, payload=payload))
    len(ids)
  inputs:
    approved: {like: flag_ideas}
    collection:
      json_schema: {type: string}
  output:
    json_schema: {type: integer}
  tools: [write_note]
---

Use after a gate has reviewed flag_ideas, never before: it writes whatever it is handed. Do not use to update a flag's status. That is the owner's act through the base's page. It reads only the object's flags. Its defects go to the repair map.
