---
title: Refresh one stance
summary: The changes one stance page takes from the records pages that bear on it, checked against the base's grammar as a whole page and written nowhere.
version: 4
step:
  name: refresh_stance
  instructions: |-
    You hold one work item. The `collection` input names the research knowledge base, which holds the stance pages. The `records` input names the records knowledge base the stances follow. The item carries stance_id, title, path, pages and current. pages lists the records pages that bear on this stance, each with its page_id, page_type, status and title. current is the stance page as it stands, a payload with frontmatter and sections.

    A stance page says what the system this base serves does on one topic. Its status is the system's position on that topic.

    Read every page in pages whole with fetch, with `knowledge_base` set to the records input and `unit_id` set to its page_id. When one of them points at another records page you need, such as the adr that replaces a superseded one, read that page the same way.

    The status follows the records, by this rule:
    - adopted, when an architecture page at status current, or a spec-doc at status executed, shows the system does it.
    - trying, when an adr at status accepted, or a spec-doc at status approved, plans it and nothing shows it built.
    - rejected, when an adr at status accepted decides against it, or the adr the stance rests on is superseded with no replacement.
    - Otherwise the status stays as it is.

    Rewrite the stance from current, and answer only what you change. You may change only these: the frontmatter summary, the frontmatter status, the TL;DR, Position and Why sections, and the Open Questions a change answers. The page is written as it stands with your changes put in, so every other section, every frontmatter field and the order of the sections stay as current has them. Keep every citation into the research knowledge base byte for byte in a section you change. record keeps naming the records page behind the stance's main decision. Name every further records page in the body.

    Every sentence you write or change about the system names the records page it rests on by its page id in backticks, such as `adr-0049-rerank`. Never write a records page id as a wikilink, since that page lives in another knowledge base.

    Each section carries its heading, its level and its body. A section keeps the level current gives it.

    Before answering, check the whole page with check_note, with `knowledge_base` set to the collection, `payload` set to the page as a dict with frontmatter and sections as its keys, and `path` set to the item's path.

    Patch the page in code inside run_code and never type it out. Load it with `page = await read_page(knowledge_base=<collection>, page_id=<stance_id>)`, writing in the collection input and the item's stance_id as strings, and take `payload = page["payload"]`. Set `payload["frontmatter"]["status"]` and `payload["frontmatter"]["summary"]`. For each section you change, find it in `payload["sections"]` by its heading and set its body. Then pass that payload to check_note. Never write Evidence, Alternatives or a section you do not change, in code or in your answer.

    Fix every finding check_note names and check again. A finding that only a change to Evidence or Alternatives would fix, such as a link to a page the research knowledge base no longer holds, is left as it is: name it in findings_left, in check_note's words. Answer only when the page passes, or when its every remaining finding is named in findings_left. You write nothing.

    The tools run inside a sandbox: call them directly with plain arguments, and `import asyncio` first if you gather calls in parallel (the import is required, the module is not preloaded). Write a link to another page of the research knowledge base as the string '[[page.id]]', brackets included.

    Answer with stance_id and path copied from the item, sources listing the page id of every records page your sentences rest on, findings_left listing every finding you left as it is, empty when you left none, and changes with the status and the summary the page should carry, changed or not, and sections holding only the sections you changed, each with its heading spelled as current spells it, its level and its new body. The answer has no place for Evidence or Alternatives, and a section you did not change is not answered. The answer is the JSON object alone, in one message, with nothing before or after it.
  inputs:
    item: {like: merge_work, element: true}
    collection:
      json_schema: {type: string}
    records:
      json_schema: {type: string}
  output:
    json_schema:
      type: object
      title: StanceDraft
      required: [stance_id, path, changes, sources, findings_left]
      properties:
        stance_id: {type: string}
        path: {type: string}
        changes:
          type: object
          title: StanceRewrite
          description: What the rewrite changes in the stance page.
          required: [status, summary, sections]
          properties:
            status: {type: string}
            summary: {type: string}
            sections:
              type: array
              description: Only the sections the rewrite changes. Empty when it changes no section.
              items:
                type: object
                title: ChangedSection
                required: [heading, level, body]
                properties:
                  heading:
                    type: string
                    enum: ["TL;DR", Position, Why, Open Questions]
                  level: {type: integer}
                  body: {type: string}
        sources:
          type: array
          items: {type: string}
        findings_left:
          type: array
          description: Each check_note finding left as it is because only a change to Evidence or Alternatives would fix it, in check_note's words. Empty when none was left.
          items: {type: string}
  tools: [fetch, read_page, check_note]
  tier: worker
  budget: {requests: 40, tool_calls: 200}
---

Use once per item of a stance refresh, inside a map over merge_work, with `collection` and `records` wired from the
workflow's parameters. Every item is a stance the run read. It answers only that stance's changes: the status, the
summary and the sections it rewrites, among TL;DR, Position, Why and Open Questions. It loads the stance with
read_page inside run_code, puts its changes into that page in code, and checks the whole page against the base's
grammar, so it never types the page out. A finding only a change to the stance's Evidence or Alternatives would fix
is left and named in `findings_left`. It writes nothing: a judge reviews every draft, and a later step merges each
stance's changes into the page as it was read and writes it.
