---
title: List initiative threads
summary: Every open owner thread on an initiative's files, grouped by initiative folder, with the folder's current analysis text when one exists.
version: 1
step:
  name: initiative_threads
  source: |
    work = await compounding_work(knowledge_base=collection, name="notes-to-analysis")
    groups = []
    seen = {}
    for r in work["rows"]:
        parts = r["path"].split("/")
        if len(parts) < 3 or parts[0] != "initiatives":
            continue
        folder = parts[0] + "/" + parts[1]
        if folder not in seen:
            analysis = await read_notes(knowledge_base=collection, path=folder + "/analysis.md")
            group = {"folder": folder, "opening_status": work["opening_status"], "analysis_exists": analysis["exists"], "analysis_text": analysis["text"], "files": []}
            seen[folder] = group
            groups.append(group)
        read = await read_notes(knowledge_base=collection, path=r["path"])
        seen[folder]["files"].append({"path": r["path"], "text": read["text"], "threads": read["threads"]})
    groups
  inputs:
    collection: {json_schema: {type: string}}
  output:
    json_schema:
      type: array
      items:
        type: object
        title: Initiative
        properties:
          folder: {type: string}
          opening_status: {type: string}
          analysis_exists: {type: boolean}
          analysis_text: {type: string}
          files:
            type: array
            items:
              type: object
              title: AnnotatedFile
              properties:
                path: {type: string}
                text: {type: string}
                threads:
                  type: array
                  items:
                    type: object
                    title: Thread
                    properties:
                      note_id: {type: string}
                      status: {type: string}
                      text: {type: string}
                      anchor: {type: string}
                      created_at: {type: string}
                      replies:
                        type: array
                        items:
                          type: object
                          title: ThreadReply
                          properties:
                            author: {type: string}
                            text: {type: string}
                            at: {type: string}
                          required: [author, text, at]
                    required: [note_id, status, text, anchor, created_at]
              required: [path, text, threads]
        required: [folder, opening_status, analysis_exists, analysis_text, files]
  tools: [compounding_work, read_notes]
---

Use at the head of a notes to analysis pass, one call per collection. It groups every file the base's
notes-to-analysis policy entry admits this run by its initiative folder, reading each file's open threads and the
folder's own analysis once. Do not use to list every annotated file in the base, only the ones the policy entry
admits this run, capped by that entry, so a pass stays one batch.
