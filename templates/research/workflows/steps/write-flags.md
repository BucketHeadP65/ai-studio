---
title: Write flags
summary: Every approved flag of the gate's object becomes a pending flag page, and the count is returned. Defects are never written here.
version: 4
step:
  name: write_flags
  source: |
    written = 0
    refused = []
    for f in approved["flags"]:
        slug = "".join(c if c.isalnum() else "-" for c in f["title"].lower()).strip("-")[:60]
        about = []
        for ref in f["about"]:
            page = str(ref).strip()
            if page.startswith("[[") and page.endswith("]]"):
                page = page[2:-2]
            page = page.split("|")[0].strip()
            if page and page not in about:
                about.append(page)
        payload = {
            "frontmatter": {"id": "flag." + slug, "type": "flag", "title": f["title"], "summary": f["summary"], "status": "pending", "flag_kind": f["flag_kind"], "about": ["[[" + page + "]]" for page in about]},
            "sections": [{"heading": "Why", "body": f["why"]}, {"heading": "Proposal", "body": f["proposal"]}, {"heading": "Evidence", "body": f.get("evidence", "")}],
        }
        try:
            await write_note(knowledge_base=collection, payload=payload)
            written = written + 1
        except Exception as error:
            refused.append("flag." + slug + ": " + str(error))
    if refused:
        noun = "flag" if written == 1 else "flags"
        raise Exception("wrote " + str(written) + " " + noun + ", refused " + str(len(refused)) + ": " + " | ".join(refused))
    written
  inputs:
    approved: {like: flag_ideas}
    collection:
      json_schema: {type: string}
  output:
    json_schema: {type: integer}
  tools: [write_note]
---

Use after a gate has reviewed flag_ideas, never before: it writes whatever it is handed. Each `about` page is linked once, as a plain unit link, however the drafter wrote it. A flag the base refuses never keeps the others from landing: every other flag is written first, then the step fails naming each refused flag and why. Do not use to update a flag's status. That is the owner's act through the base's page. It reads only the object's flags. Its defects go to the repair map.
