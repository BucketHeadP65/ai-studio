---
title: Write ADRs
summary: Each drafted ADR becomes a proposed page under the base's adr folder, numbered past the highest root ADR on file, naming the design it absorbs, and a refused draft never stops the writable ones.
version: 3
step:
  name: write_adrs
  source: |
    work = await compounding_work(knowledge_base=collection, name="promote-initiative")
    rows = await list_pages(knowledge_base=collection, page_type="adr")
    highest = 0
    for row in rows:
        page_id = row["page_id"]
        if page_id[:4] != "adr-":
            continue
        digits = page_id[4:8]
        if digits.isdigit() and int(digits) > highest:
            highest = int(digits)
    n = highest + 1
    count = 0
    refused = []
    for d in drafts:
        number = ("0000" + str(n))[-4:]
        slug = "".join(c if c.isalnum() else "-" for c in d["slug"].lower()).strip("-")
        if slug[:4] == "adr-":
            slug = slug[4:]
        slug = slug[:60].strip("-")
        page_id = "adr-" + number + "-" + slug
        payload = {
            "frontmatter": {"id": page_id, "aliases": [page_id], "type": "adr", "app": "root", "status": work["opening_status"], "title": d["title"], "summary": d["summary"][:200], "absorbs": ["[[" + d["design_id"] + "]]"]},
            "sections": [{"heading": s["heading"], "body": s["body"]} for s in d["sections"]],
        }
        try:
            await write_note(knowledge_base=collection, payload=payload, path="adr/" + number + "-" + slug + ".md")
        except Exception as error:
            refused.append(slug + ": " + str(error))
            continue
        n = n + 1
        count = count + 1
    if refused:
        raise RuntimeError(str(count) + " written, " + str(len(refused)) + " refused: " + "; ".join(refused))
    count
  inputs:
    drafts: {like: draft_adr, many: true}
    collection: {json_schema: {type: string}}
  output:
    json_schema: {type: integer}
  tools: [compounding_work, list_pages, write_note]
---

Use as the tail step of a promote initiative pass, after a gate has reviewed the drafts. The number counts only the
root chain, the ids that start with adr-, an app chain's ids carry the app's own prefix. The status each page opens
at is the promote-initiative policy entry's own opening value, read fresh each run. Do not use before the gate, it
writes whatever it is handed. A draft the grammar refuses is set aside, every writable draft still lands, and the
run then fails naming each refusal, so the refused designs stay pending and the next pass lists them again.
