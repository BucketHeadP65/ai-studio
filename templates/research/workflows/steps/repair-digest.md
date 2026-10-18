---
title: Repair one source's digest
summary: One source's defective digest pages fixed in place, skipped with a reason, or named for the owner, never flagged.
version: 1
step:
  name: repair_digest
  instructions: |
    You hold one repair item and the collection. The item names a source_id, the page_ids found defective, every page of that source this run read with its path (pages), the one defect to repair, and the evidence for it. Change only pages of this item's source, and list in written every page id you changed.

    When the defect is remove_source: change nothing. Answer outcome needs_owner, with a detail naming the one act the owner can take: fire `wf.source-remove` with source set to the item's source_id.

    When the defect is off_purpose or duplicate: call skip_digest once, with `knowledge_base` set to the collection, page_id set to the first of page_ids, and reason set to one sentence from the evidence that starts with Skipped:. It skips every digest page of the source and answers which. Answer outcome repaired with written set to the ids it names, or outcome failed with its refusal as the detail.

    Every write_note call passes `path` set to that page's path from pages, so the rewrite replaces the file you read. Write one page per write_note call and one page per program, never several pages in one answer, and read the mirror once and keep it in this conversation.

    When the defect is misquoted or mislabelled_claim: read the mirror once with read_source (`knowledge_base` the collection, source_id the item's source_id). For each id in page_ids, read the page whole with read_notes (`knowledge_base` the collection, path from pages), then rewrite it through write_note under the same id. Keep the frontmatter exactly as the file has it, every field (id, digest_of, the source key and hash fields, created, part_of, status, aliases). Keep every section but the one that holds the claims. Pass the claims as the payload's claims list, never inside a section body, each with its anchor as written (`^c3` stays `^c3`, because flags cite claims by anchor). A misquoted claim gets a quote copied word for word from one chunk's text and that chunk's loc. When a quote holds only a section marker such as `§7` and its loc holds the source's words, write the claim as it stands: write_note swaps the two and names the swap in its warnings. A mislabelled claim gets the registered claim type that says who makes the claim. A claim no chunk supports is kept exactly as it stands, never invented and never dropped, since dropping it removes its anchor. Answer outcome failed with that claim's anchor named in the detail.

    When the defect is empty: read the mirror with read_source and every page in pages with read_notes, then rewrite each page under its own id from the source: the summary in one sentence, each section from the source, and one claim per finding with a quote copied word for word from a chunk and that chunk's loc. Keep every frontmatter field and every existing anchor.

    When a page in page_ids has no path in pages, or read_notes answers exists false, this run did not read it: change nothing on it, and answer outcome needs_owner naming it.

    write_note answers where the page landed and how the index came out. The item is repaired only when every write landed and no answer names a validation error on one of this item's pages. When write_note refuses, fix exactly what it names and write again, at most twice for one page, then answer outcome failed with the refusal as the detail. Never write a page of another source, a new page, or a placeholder.

    The tools run inside a sandbox: call them directly with plain arguments, and `import asyncio` first if you gather calls in parallel (the import is required, the module is not preloaded). Build each payload as a Python dict literal with frontmatter, sections and claims as its three keys, and a link to another page as the string '[[page.id]]', brackets included. Answer with source_id and page_ids copied from the item, the defect, the outcome, written, and a detail of one or two sentences saying what changed.
  inputs:
    item: {like: list_repairs, element: true}
    collection:
      json_schema: {type: string}
  output:
    json_schema:
      type: object
      title: RepairResult
      required: [source_id, page_ids, defect, outcome, written, detail]
      properties:
        source_id: {type: string}
        page_ids:
          type: array
          items: {type: string}
        defect:
          enum: [off_purpose, misquoted, mislabelled_claim, empty, duplicate, remove_source]
          type: string
        outcome:
          enum: [repaired, failed, needs_owner]
          type: string
        written:
          type: array
          items: {type: string}
        detail: {type: string}
  tools: [read_notes, read_source, write_note, skip_digest]
---

Use once per item of a flag pass's repair plan, inside a map after the gate. Do not use to flag anything, to change a page of another source, or to remove a source: removal is the owner's own `wf.source-remove` fire.
