---
title: Write stances
summary: Each judged stance draft becomes its page, an existing stance's changes merged into the page as it was read, and every refusal is named rather than stopping the rest.
version: 2
step:
  name: write_stances
  source: |
    read = {}
    for stance in changes["stances"]:
        read[stance["stance_id"]] = stance
    written = []
    failed = []
    for draft in drafts:
        stance_id = draft["stance_id"]
        rewrite = draft.get("changes")
        drafted = draft.get("payload")
        before = read.get(stance_id)
        path = ""
        page = None
        problem = ""
        if draft["new"]:
            if drafted is None:
                problem = "the draft of the new stance " + stance_id + " carries no page"
            elif drafted["frontmatter"]["id"] != stance_id:
                problem = "the page id " + drafted["frontmatter"]["id"] + " is not the proposed id " + stance_id
            elif stance_id in changes["stance_ids"]:
                problem = "a new stance may not take the id of the existing stance " + stance_id
            else:
                page = drafted
        elif before is None:
            problem = "no stance " + stance_id + " was read before this run, so its page cannot be checked"
        elif rewrite is None:
            problem = "the draft of the existing stance " + stance_id + " carries no changes"
        else:
            named = [section["heading"] for section in rewrite["sections"]]
            held = [section["heading"] for section in before["payload"]["sections"]]
            twice = [heading for heading in named if named.count(heading) > 1]
            absent = [heading for heading in named if heading not in held]
            if twice:
                problem = "the draft changes the " + twice[0] + " section twice"
            elif absent:
                problem = "the draft changes the " + absent[0] + " section, which the page as read before this run does not carry"
            else:
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
                path = before["path"]
                page = {"frontmatter": frontmatter, "sections": sections}
        if problem:
            failed.append({"stance_id": stance_id, "reason": problem})
            continue
        carried = {key: value for key, value in page["frontmatter"].items() if value is not None}
        try:
            await write_note(knowledge_base=collection, payload={"frontmatter": carried, "sections": page["sections"]}, path=path)
        except Exception as error:
            failed.append({"stance_id": stance_id, "reason": "write_note refused the page: " + str(error)})
            continue
        written.append(stance_id)
    {"written": written, "failed": failed}
  inputs:
    drafts: {like: refresh_stance, many: true}
    changes: {like: list_stance_changes}
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

Use as the tail of a stance refresh, after a judge has reviewed the drafts, with `changes` wired from the same run's
list_stance_changes. An existing stance is built from its page as list_stance_changes read it before the run: its
status and summary are the draft's, each section the draft names takes the draft's body at the place and level the
page gave it, and every other section and frontmatter field stays as read. Its id, its Evidence and its
Alternatives come from the page as read, so no draft can change them. It is written to the path it was read from. A draft of an existing stance that carries no
changes, names a section twice, or names a section the page does not carry is refused, and so is a draft of a stance
the run did not read. A new stance is written from the page its draft carries, refused when it carries none. It must
keep the id it was proposed under, and is refused when that id is the id of any stance the run listed, read or not.
It is written with no path, so write_note places it in the stance page type's own folder, and the path a draft
answers is never used. A draft that fails a check is not written, and a page write_note refuses is caught. Both are
answered in `failed` with the reason, and every other draft still lands. A frontmatter field held as null is left
out of the page.
