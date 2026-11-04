---
title: Write stances
summary: Each judged stance rewrite merged into its page as it was read and written, every refusal named rather than stopping the rest.
version: 6
step:
  name: write_stances
  source: |
    read = {}
    for item in work:
        read[item["stance_id"]] = {"path": item["path"], "payload": item["current"]}
    written = []
    failed = []
    for draft in drafts:
        stance_id = draft["stance_id"]
        rewrite = draft["changes"]
        before = read.get(stance_id)
        problem = ""
        if before is None:
            problem = "no stance " + stance_id + " was read before this run, so its page cannot be checked"
        else:
            named = [section["heading"] for section in rewrite["sections"]]
            held = [section["heading"] for section in before["payload"]["sections"]]
            twice = [heading for heading in named if named.count(heading) > 1]
            absent = [heading for heading in named if heading not in held]
            if twice:
                problem = "the draft changes the " + twice[0] + " section twice"
            elif absent:
                problem = "the draft changes the " + absent[0] + " section, which the page as read before this run does not carry"
        if problem:
            failed.append({"stance_id": stance_id, "reason": problem})
            continue
        bodies = {}
        for section in rewrite["sections"]:
            bodies[section["heading"]] = section["body"]
        sections = []
        for section in before["payload"]["sections"]:
            if section["heading"] in bodies:
                sections.append({"heading": section["heading"], "level": section["level"], "body": bodies[section["heading"]]})
            else:
                sections.append(section)
        frontmatter = dict(before["payload"]["frontmatter"])
        frontmatter["status"] = rewrite["status"]
        frontmatter["summary"] = rewrite["summary"]
        carried = {key: value for key, value in frontmatter.items() if value is not None}
        try:
            await write_note(knowledge_base=collection, payload={"frontmatter": carried, "sections": sections}, path=before["path"])
        except Exception as error:
            failed.append({"stance_id": stance_id, "reason": "write_note refused the page: " + str(error)})
            continue
        written.append(stance_id)
    {"written": written, "failed": failed}
  inputs:
    drafts: {like: refresh_stance, many: true}
    work: {like: merge_work}
    collection:
      json_schema: {type: string}
  output:
    json_schema:
      type: object
      title: StancesWritten
      required: [written, failed]
      properties:
        written:
          type: array
          items: {type: string}
        failed:
          type: array
          items:
            type: object
            title: StanceNotWritten
            required: [stance_id, reason]
            properties:
              stance_id: {type: string}
              reason: {type: string}
  tools: [write_note]
---

Use after the judge of a stance refresh's drafts, with `work` wired from the same run's merge_work. Each stance is
built from its page as merge_work carries it, read before the run: its status and summary are the draft's, each
section the draft names takes the draft's body at the place and level the page gave it, and every other section and
frontmatter field stays as read. Its id, its Evidence and its Alternatives come from the page as read, so no draft
can change them. It is written to the path it was read from. A draft that names a section twice, or names a section
the page does not carry, is refused, and so is a draft of a stance merge_work holds no item for. A draft that fails a
check is not written, and a page write_note refuses is caught. Both are answered in `failed` with the reason, and
every other draft still lands. A frontmatter field held as null is left out of the page. It writes no page for a
proposed new stance.
