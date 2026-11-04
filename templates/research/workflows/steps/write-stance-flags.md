---
title: Write stance flags
summary: Each approved stance proposal written as a pending flag page with its records pages and its findings, a flag the owner already decided left alone, and every refused write named rather than stopping the rest.
version: 2
step:
  name: write_stance_flags
  source: |
    indexed = {}
    listed = True
    refusal = ""
    try:
        for row in await list_pages(knowledge_base=collection, page_type="flag", limit=10000):
            indexed[row["page_id"]] = True
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
        created = ""
        if flag_id in indexed:
            try:
                page = await read_page(knowledge_base=collection, page_id=flag_id)
            except Exception as error:
                failed.append({"flag_id": flag_id, "reason": "read_page refused the flag, so it was left as it is: " + str(error)})
                continue
            front = page["payload"]["frontmatter"]
            if front.get("flag_kind") != "stance":
                failed.append({"flag_id": flag_id, "reason": "a flag of kind " + str(front.get("flag_kind")) + " already holds this id, so it was left as it is"})
                continue
            if front.get("status") != "pending":
                kept_decided.append(flag_id)
                continue
            path = page["path"]
            created = str(front.get("created") or "")
        else:
            target = "Wiki/Flags/" + flag_id[len("flag."):] + ".md"
            try:
                found = await read_notes(knowledge_base=collection, path=target)
            except Exception as error:
                failed.append({"flag_id": flag_id, "reason": "read_notes refused " + target + ", so no flag was written: " + str(error)})
                continue
            if found["exists"]:
                failed.append({"flag_id": flag_id, "reason": "a file the index does not list sits at " + target + ", so it was left as it is. Index the knowledge base and run again."})
                continue
            path = target
        reasons = {}
        for entry in proposal["reasons"]:
            reasons[entry["page_id"]] = entry["reason"]
        why = ["The stance refresh proposed this stance from these records pages. They live in another knowledge base, so each is named by its id.", ""]
        for page_entry in proposal["pages"]:
            why.append("- `" + page_entry["page_id"] + "` (" + page_entry["page_type"] + ", " + page_entry["status"] + "): " + reasons.get(page_entry["page_id"], page_entry["title"]))
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
            for_it = "1 finding that supports it" if supports == 1 else str(supports) + " findings that support it"
            against_it = "1 that contests it" if contests == 1 else str(contests) + " that contest it"
            summary = "A proposed stance with " + for_it + " and " + against_it + "."
        frontmatter = {"id": flag_id, "type": "flag", "title": proposal["title"], "summary": summary, "status": "pending", "flag_kind": "stance", "about": about}
        if created:
            frontmatter["created"] = created
        proposal_text = "Accepting this flag asks for a new stance page that holds this stance: " + proposal["title"] + ". It is drafted by hand from the evidence below."
        sections = [
            {"heading": "Why", "body": "\n".join(why)},
            {"heading": "Proposal", "body": proposal_text},
            {"heading": "Evidence", "body": "\n".join(evidence)},
        ]
        payload = {"frontmatter": frontmatter, "sections": sections}
        try:
            await write_note(knowledge_base=collection, payload=payload, path=path)
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
  tools: [list_pages, read_page, read_notes, write_note]
---

Use as the tail of a stance refresh, after its proposals are judged and listed, with `collection` set to the research
knowledge base. Each proposal becomes the flag `flag.stance-` followed by its stance id without `stance.`, of kind
`stance` at status `pending`. Its title is the proposed stance. `about` links each digest a finding cites, once. Why
names each records page by its id in backticks, with the reason placement gave, since a records page lives in another
knowledge base and a link to it would not resolve. Proposal says that accepting the flag asks for a new stance page.
Evidence lists each finding as a claim link, its side and its sentence. The summary counts the findings for and
against the stance, or says that the literature argues against it when they all contest it.

It lists the base's flags first, and decides on each listed flag from its file, read with `read_page`, never from the
index. A flag of another kind under the same id is left as it is and answered in `failed`. A flag whose file is not at
`pending`, one the owner accepted, dismissed or completed, is never written and is answered in `kept_decided`. A
pending stance flag is written again at the path it was read from, with the run's newer findings and its `created`
date. A flag the index lists that `read_page` refuses is answered in `failed` and not written. A flag the index does
not list is written to `Wiki/Flags`, at the very path `read_notes` checked, unless a file already sits there, which is
answered in `failed` and left as it is. A base whose flags live in another folder refuses that path, and each new flag
is answered in `failed`. A write `write_note` refuses is caught and answered in `failed` with the reason, and every
other flag still lands. When `list_pages` refuses the flags, no flag is written, and each is answered in `failed`.
