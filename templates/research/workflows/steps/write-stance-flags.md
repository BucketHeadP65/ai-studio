---
title: Write stance flags
summary: Each approved stance proposal written as a pending flag page with its records pages and its findings, a flag the owner already decided left alone, and every refused write named rather than stopping the rest.
version: 1
step:
  name: write_stance_flags
  source: |
    held = {}
    listed = True
    refusal = ""
    try:
        for row in await list_pages(knowledge_base=collection, page_type="flag", limit=10000):
            held[row["page_id"]] = row["status"]
    except Exception as error:
        listed = False
        refusal = "list_pages refused the flags, so no flag was written: " + str(error)
    written = []
    kept_decided = []
    failed = []
    for proposal in proposals:
        stance_id = proposal["stance_id"]
        name = stance_id[len("stance."):] if stance_id.startswith("stance.") else stance_id
        flag_id = "flag.stance-" + name
        if not listed:
            failed.append({"flag_id": flag_id, "reason": refusal})
            continue
        status = held.get(flag_id)
        if status is not None and status != "pending":
            kept_decided.append(flag_id)
            continue
        reasons = {}
        for entry in proposal["reasons"]:
            reasons[entry["page_id"]] = entry["reason"]
        why = ["The stance refresh proposed this stance from these records pages. They live in another knowledge base, so each is named by its id.", ""]
        for page in proposal["pages"]:
            why.append("- `" + page["page_id"] + "` (" + page["page_type"] + ", " + page["status"] + "): " + reasons.get(page["page_id"], page["title"]))
        evidence = []
        about = []
        supports = 0
        for finding in proposal["findings"]:
            evidence.append("- [[" + finding["claim"] + "]] " + finding["side"] + ": " + finding["how"])
            digest = "[[" + finding["claim"].split("#")[0] + "]]"
            if digest not in about:
                about.append(digest)
            if finding["side"] == "supports":
                supports = supports + 1
        contests = len(proposal["findings"]) - supports
        if proposal["contests_only"]:
            summary = "A proposed stance the literature argues against: all " + str(contests) + " of its findings contest it."
        else:
            summary = "A proposed stance with " + str(supports) + " findings that support it and " + str(contests) + " that contest it."
        frontmatter = {"id": flag_id, "type": "flag", "title": proposal["title"], "summary": summary, "status": "pending", "flag_kind": "stance", "about": about}
        if status == "pending":
            created = None
            try:
                page = await read_page(knowledge_base=collection, page_id=flag_id)
                created = page["payload"]["frontmatter"].get("created")
            except Exception as error:
                created = None
            if created:
                frontmatter["created"] = created
        proposal_text = "Accepting this flag asks for a new stance page that holds this stance: " + proposal["title"] + ". It is drafted by hand from the evidence below."
        sections = [
            {"heading": "Why", "body": "\n".join(why)},
            {"heading": "Proposal", "body": proposal_text},
            {"heading": "Evidence", "body": "\n".join(evidence)},
        ]
        try:
            await write_note(knowledge_base=collection, payload={"frontmatter": frontmatter, "sections": sections})
        except Exception as error:
            failed.append({"flag_id": flag_id, "reason": "write_note refused the page: " + str(error)})
            continue
        written.append(flag_id)
    {"written": written, "kept_decided": kept_decided, "failed": failed}
  inputs:
    proposals: {like: list_proposals}
    collection:
      json_schema: {type: string}
  output:
    json_schema:
      type: object
      title: StanceFlagsWritten
      required: [written, kept_decided, failed]
      properties:
        written:
          type: array
          description: The flag ids written as pending flags.
          items: {type: string}
        kept_decided:
          type: array
          description: The flag ids the owner already accepted, dismissed or completed, which were left as they are.
          items: {type: string}
        failed:
          type: array
          items:
            type: object
            title: StanceFlagNotWritten
            required: [flag_id, reason]
            properties:
              flag_id: {type: string}
              reason: {type: string}
  tools: [list_pages, read_page, write_note]
---

Use as the tail of a stance refresh, after its proposals are judged and listed, with `collection` set to the research
knowledge base. Each proposal becomes the flag `flag.stance-` followed by its stance id without `stance.`, of kind
`stance` at status `pending`, in the flag type's own folder. Its title is the proposed stance. `about` links each digest
a finding cites. Why names each records page by its id in backticks, with the reason placement gave, since a records
page lives in another knowledge base and a link to it would not resolve. Proposal says that accepting the flag asks for
a new stance page. Evidence lists each finding as a claim link, its side and its sentence. A flag whose findings all
contest the stance says so in its summary. A flag at `pending` is written again with the run's newer findings and keeps
its `created` date. A flag the owner accepted, dismissed or completed is never written and is answered in
`kept_decided`. A write `write_note` refuses is caught and answered in `failed` with the reason, and every other flag
still lands. When `list_pages` refuses the flags, no flag is written, so none the owner decided is ever replaced, and
each is answered in `failed`.
