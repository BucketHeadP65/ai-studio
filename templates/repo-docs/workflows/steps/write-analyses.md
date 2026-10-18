---
title: Write analyses and resolve threads
summary: Each drafted analysis becomes a written page under its initiative's analysis.md, and every thread it folded is marked resolved with where it went.
version: 1
step:
  name: write_analyses
  source: |
    status_by_folder = {}
    for i in initiatives:
        status_by_folder[i["folder"]] = i["opening_status"]
    written = []
    for d in drafts:
        folder = d["folder"]
        if folder not in status_by_folder:
            continue
        name = folder.split("/")[1]
        slug = name
        if len(name) > 11 and name[10] == "-" and name[:4].isdigit():
            slug = name[11:]
        payload = {
            "frontmatter": {"id": "bs." + slug, "aliases": [slug, "bs." + slug], "type": "brainstorming-doc", "title": d["title"], "summary": d["summary"][:200], "status": status_by_folder[folder]},
            "sections": [{"heading": s["heading"], "body": s["body"]} for s in d["sections"]],
        }
        page_id = await write_note(knowledge_base=collection, payload=payload, path=folder + "/analysis.md")
        resolved = 0
        for f in d["folded"]:
            await resolve_note(knowledge_base=collection, path=f["path"], note_id=f["note_id"], reply="Folded into analysis.md as " + f["finding"] + ".")
            resolved = resolved + 1
        written.append({"folder": folder, "page_id": page_id, "resolved": resolved})
    written
  inputs:
    drafts: {like: fold_notes, many: true}
    initiatives: {like: initiative_threads}
    collection: {json_schema: {type: string}}
  output:
    json_schema:
      type: array
      items:
        type: object
        title: WrittenAnalysis
        properties:
          folder: {type: string}
          page_id: {type: string}
          resolved: {type: integer}
        required: [folder, page_id, resolved]
  tools: [write_note, resolve_note]
---

Use as the step after fold_notes, over its whole drafts list together with the initiatives list. It writes each
draft under its own initiative's analysis.md and resolves every thread the draft folded, replying with where it
went. Do not use with a drafts list gathered from a different initiatives list, the status each page gets comes
from matching the two by folder.
