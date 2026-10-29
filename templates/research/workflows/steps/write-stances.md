---
title: Write stances
summary: Each judged stance draft becomes its page, written only when its id, Evidence and Alternatives match the page as it was read, and every refusal is named rather than stopping the rest.
version: 1
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
        payload = draft["payload"]
        page_id = payload["frontmatter"]["id"]
        before = read.get(stance_id)
        path = draft["path"]
        problem = ""
        if before is None and not draft["new"]:
            problem = "no stance " + stance_id + " was read before this run, so its page cannot be checked"
        elif before is None and page_id != stance_id:
            problem = "the page id " + page_id + " is not the proposed id " + stance_id
        elif before is not None:
            path = before["path"]
            was_id = before["payload"]["frontmatter"]["id"]
            if page_id != was_id:
                problem = "the page id changed from " + was_id + " to " + page_id
            for heading in ["Evidence", "Alternatives"]:
                was = [section for section in before["payload"]["sections"] if section["heading"] == heading]
                now = [section for section in payload["sections"] if section["heading"] == heading]
                if not problem and was != now:
                    problem = "the " + heading + " section differs from the page as it was read before this run"
        if problem:
            failed.append({"stance_id": stance_id, "reason": problem})
            continue
        frontmatter = {key: value for key, value in payload["frontmatter"].items() if value is not None}
        try:
            await write_note(knowledge_base=collection, payload={"frontmatter": frontmatter, "sections": payload["sections"]}, path=path)
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
list_stance_changes. An existing stance is written to the path it was read from, and only when its page id is
unchanged and its Evidence and Alternatives sections are byte for byte the page as it was read before the run. A new
stance must keep the id it was proposed under and may not take the id of a stance that exists. A draft that fails a
check is not written, and a page write_note refuses is caught. Both are answered in `failed` with the reason, and
every other draft still lands. A frontmatter field the draft holds as null is left out of the page.
