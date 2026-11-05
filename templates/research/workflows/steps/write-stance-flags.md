---
title: Write stance flags
summary: Each listed stance proposal written as a pending flag page with its records pages and its findings, a flag the owner already decided left alone, and every refused write named rather than stopping the rest, then each pending flag the grouping found the same idea as another, and each one whose proposal the judge dropped, removed and named with its id, title and path, beside the dropped proposals, the digests the run could not read, the cited ids the base does not hold, and the placements under no listed stance.
version: 14
step:
  name: write_stance_flags
  source: |
    indexed = {}
    flags_listed = True
    listing = ""
    refusal = ""
    try:
        for row in await list_pages(knowledge_base=collection, page_type="flag", limit=10000):
            indexed[row["page_id"]] = row["status"]
    except Exception as error:
        flags_listed = False
        listing = str(error)
        refusal = "list_pages refused the flags, so no flag was written: " + listing
    written = []
    kept_decided = []
    failed = []
    for proposal in listed["proposals"]:
        stance_id = proposal["stance_id"]
        name = stance_id[len("stance."):] if stance_id.startswith("stance.") else stance_id
        flag_id = "flag.stance-" + name
        if not flags_listed:
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
        if proposal["refused"]:
            evidence.append("")
            evidence.append("Left out:")
            evidence.append("")
            for refusal in proposal["refused"]:
                evidence.append("- `" + refusal["claim"] + "`: " + refusal["why"])
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
    removing = []
    for duplicate in grouped["duplicates"]:
        removing.append({"flag_id": duplicate["flag_id"], "title": duplicate["title"], "reason": "It repeats the idea of " + duplicate["kept"] + "."})
    for dropped in listed["dropped"]:
        if not dropped["dropped_by_judge"]:
            continue
        stance_id = dropped["stance_id"]
        name = stance_id[len("stance."):] if stance_id.startswith("stance.") else stance_id
        flag_id = "flag.stance-" + name
        reason = "The judge found fewer than two digests with findings that bear."
        if dropped["reasons"][0].startswith("The judge found it repeats the existing stance "):
            reason = dropped["reasons"][0]
        if not flags_listed:
            removing.append({"flag_id": flag_id, "title": dropped["title"], "reason": reason, "unlisted": True})
        elif indexed.get(flag_id) == "pending":
            removing.append({"flag_id": flag_id, "title": dropped["title"], "reason": reason})
    removed = []
    refused_removals = []
    asked = {}
    for entry in removing:
        flag_id = entry["flag_id"]
        if flag_id in asked or flag_id in written:
            continue
        asked[flag_id] = True
        if entry.get("unlisted"):
            refused_removals.append({"flag_id": flag_id, "title": entry["title"], "reason": "list_pages refused the flags, so its flag was not looked for: " + listing})
            continue
        try:
            page = await read_page(knowledge_base=collection, page_id=flag_id)
        except Exception as error:
            refused_removals.append({"flag_id": flag_id, "title": entry["title"], "reason": "read_page refused the flag, so it was left as it is: " + str(error)})
            continue
        front = page["payload"]["frontmatter"]
        title = str(front.get("title") or entry["title"])
        if front.get("flag_kind") != "stance":
            refused_removals.append({"flag_id": flag_id, "title": title, "reason": "a flag of kind " + str(front.get("flag_kind")) + " holds this id, so it was left as it is"})
            continue
        if front.get("status") != "pending":
            if flag_id not in kept_decided:
                kept_decided.append(flag_id)
            continue
        try:
            await remove_undecided_page(knowledge_base=collection, page_id=flag_id, reason=entry["reason"])
        except Exception as error:
            refused_removals.append({"flag_id": flag_id, "title": title, "reason": "remove_undecided_page refused it, so it was left as it is: " + str(error)})
            continue
        removed.append({"flag_id": flag_id, "title": title, "path": page["path"], "reason": entry["reason"]})
    {"written": written, "kept_decided": kept_decided, "failed": failed, "removed": removed, "refused_removals": refused_removals, "dropped": listed["dropped"], "unread": listed["unread"], "missing": listed["missing"], "unplaced": unplaced}
  inputs:
    listed: {like: list_proposals}
    unplaced: {like: list_unplaced}
    grouped: {like: merge_same_ideas}
    collection:
      json_schema: {type: string}
  output:
    json_schema:
      type: object
      title: StanceFlagsWritten
      required: [written, kept_decided, failed, removed, refused_removals, dropped, unread, missing, unplaced]
      properties:
        written:
          type: array
          description: The flag ids written as pending flags.
          items: {type: string}
        kept_decided:
          type: array
          description: The flag ids the owner already accepted, dismissed or completed, which were left as they are, whether the run would have written them or removed them.
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
        removed:
          type: array
          description: Each pending stance flag removed, by its id, title and the path its file was read from, with why. The base keeps no copy of a removed file.
          items:
            type: object
            title: StanceFlagRemoved
            required: [flag_id, title, path, reason]
            properties:
              flag_id: {type: string}
              title: {type: string}
              path: {type: string}
              reason: {type: string}
        refused_removals:
          type: array
          description: Each flag the run asked to remove and left as it is, with why. A later step fails the run naming each one.
          items:
            type: object
            title: StanceFlagNotRemoved
            required: [flag_id, title, reason]
            properties:
              flag_id: {type: string}
              title: {type: string}
              reason: {type: string}
        dropped:
          type: array
          description: The stance proposals the run dropped, each with why, as `list_proposals` handed them.
          items:
            type: object
            title: DroppedStanceProposal
            required: [stance_id, title, reasons, dropped_by_judge]
            properties:
              stance_id: {type: string}
              title: {type: string}
              reasons:
                type: array
                items: {type: string}
              dropped_by_judge: {type: boolean}
        unread:
          type: array
          description: Each digest the base holds that the run could not read, with read_page's refusal, as `list_proposals` handed them.
          items:
            type: object
            title: UnreadDigest
            required: [digest, reason]
            properties:
              digest: {type: string}
              reason: {type: string}
        missing:
          type: array
          description: Each cited id the base does not hold, with read_page's refusal, as `list_proposals` handed them.
          items:
            type: object
            title: MissingId
            required: [digest, reason]
            properties:
              digest: {type: string}
              reason: {type: string}
        unplaced:
          type: array
          description: The placements under a stance the base does not list, as `list_unplaced` handed them.
          items:
            type: object
            title: UnplacedPlacement
            required: [page_id, stance_id, title, reason]
            properties:
              page_id: {type: string}
              stance_id: {type: string}
              title: {type: string}
              reason: {type: string}
  tools: [list_pages, read_page, read_notes, write_note, remove_undecided_page]
---

Use as the tail of a stance refresh, after its proposals are judged and listed, with `listed` wired from
`list_proposals` and `collection` set to the research knowledge base. Each proposal in its `proposals` becomes the
flag `flag.stance-` followed by its stance id without `stance.`, of kind `stance` at status `pending`. Its title is the
proposed stance. `about` links each digest a finding cites, once. Why names each records page by its id in backticks,
with the reason placement gave, since a records page lives in another knowledge base and a link to it would not
resolve. Proposal says that accepting the flag asks for a new stance page. Evidence lists each finding as a claim
link, its side and its sentence, then, under "Left out", each claim of its backings that was not kept, in backticks so
no link to it is indexed, with why: the judge said it does not bear, gave it no verdict, or its backings gave it
opposite sides. The summary counts the findings for and against the stance, or says that the
literature argues against it when they all contest it.

It lists the base's flags first, and decides on each listed flag from its file, read with `read_page`, never from the
index. A flag of another kind under the same id is left as it is and answered in `failed`. A flag whose file is not at
`pending`, one the owner accepted, dismissed or completed, is never written and is answered in `kept_decided`. A
pending stance flag is written again at the path it was read from, with the run's newer findings and its `created`
date. A flag the index lists that `read_page` refuses is answered in `failed` and not written. A flag the index does
not list is written to `Wiki/Flags`, at the very path `read_notes` checked, unless a file already sits there, which is
answered in `failed` and left as it is. A base whose flags live in another folder refuses that path, and each new flag
is answered in `failed`. A write `write_note` refuses is caught and answered in `failed` with the reason, and every
other flag still lands. When `list_pages` refuses the flags, no flag is written, and each is answered in `failed`.
Its answer is the run's output, so it also answers, unchanged, the proposals `list_proposals` dropped, each with why,
in `dropped`, the digests the base holds that the run could not read in `unread` and the cited ids the base does not
hold in `missing`, each with read_page's refusal, and the placements `list_unplaced` named, each under a stance id no
stance page carries, in `unplaced`.

Once every flag is written it removes flags, with `grouped` wired from the same run's `merge_same_ideas`: each pending flag
the grouping found the same idea as the flag a group keeps, and the pending flag of each proposal `list_proposals`
dropped as `dropped_by_judge`, the judge's own reading of the idea: a repeat of a listed stance, or its verdicts leaving
findings from fewer than two digests. A drop the judge never read keeps its flag. A flag the index
does not list at `pending` has nothing to remove for a dropped proposal. Before each removal it reads the flag's file
with `read_page` and removes it with `remove_undecided_page` only while that file is a stance flag at `pending`, so a
flag the owner decided is never removed, nor one this run wrote. Each removal is caught on its own. Every removed flag
is answered in `removed` with its id, its title, the path its file was read from and why, in one sentence that is also
the reason `remove_undecided_page` is given: "It repeats the idea of" the flag a group keeps, "The judge found it repeats
the existing stance" and the stance's id, or "The judge found fewer than two digests with findings that bear."
A flag whose file is no longer at `pending`, one the owner decided before the index caught up, is left as it is and
answered in `kept_decided`, as a decided flag it would have written over is. Every other flag it asked to remove and
left as it is, because `read_page` refused it, another kind holds its id, `remove_undecided_page` refused it, or the
flags could not be listed, is answered in `refused_removals` with why, for the step after it to fail the run on.
