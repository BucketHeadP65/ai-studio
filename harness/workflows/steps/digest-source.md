---
title: Digest one source
summary: One mirror becomes a typed digest page (a lead plus section pages for a long source), with quoted claims, or a skipped page with a reason.
version: 11
step:
  name: digest_source
  instructions: |
    Call digest_skeleton with the collection as `knowledge_base` and the source_id. It answers a ready write_note payload: the
    `page_type`, the id, and every identity field already filled from the mirror, this `page_type`'s own sections with empty
    bodies, empty spots for what only the source can tell, and under fill the registered values for each
    choice-bound field, the claim types under `claim.kind`, and the rules the page must keep. Then read the whole mirror with read_source, passing the same `knowledge_base` and `source_id`: it answers the
    mirror's chunks in reading order, each with a loc, a heading and its text, plus chunk_count and, when this base
    sets a digest-size threshold, split_recommended.

    If the source has no finding that bears on the purpose under `fill.purpose`, write the skeleton with status skipped,
    a one-line summary starting with Skipped: that names why, and no claims. Return the digest id and that it was skipped.

    When digest_skeleton refuses because of the knowledge base itself, not your arguments (it declares no digest page
    type for this source's type, or names a field or value its schema lacks), nothing you can pass will change the
    answer: do not call it again, do not invent a page type, and write nothing. Return status skipped with the
    refusal's own text as the reason, so the person can fix the schema.

    When split_recommended is not true, fill the skeleton and write it once through write_note: the summary in one
    sentence, every empty required field filled from fill's values, each section the skeleton lists with its body
    as a markdown string, and one claims entry per finding with its `kind` from fill, a quote copied verbatim from a chunk's text
    and that chunk's loc. Keep every value the skeleton gave you.

    When split_recommended is true, write a lead page first from the skeleton (its sections filled for the whole
    source, and in its claims any claim that holds across more than one section), then one page per major section grouped from the
    mirror's own top-level chunk headings: same `page_type` and identity fields, its own id under the same prefix, the
    field named under fill.part_field set to the lead page's id, and that section's claims. Never invent a section the
    mirror does not have.

    The tools run inside a sandbox: call them directly with plain arguments, and `import asyncio` first if you
    gather calls in parallel (the import is required, the module is not preloaded). Build the page as a Python
    dict literal, never by pasting the page's markdown into the code as text. write_note takes the skeleton's
    whole object as payload (frontmatter, sections, claims), and a link to another page is the string
    '[[page.id]]', brackets included. In section prose the citation form is write_note's: a page as
    [[its-file-stem|its.full.id]], a claim as [[its-file-stem#^cN|^cN]], never a bare id or bare anchor.
    write_note links what it can prove and answers warnings naming the rest, fix what it names before finishing. write_note is a real write into the person's knowledge base: never write
    a test or placeholder page, and when it refuses a page it names the field at fault and the registered
    values, fix exactly that and write again. Never invent a quote. The indexer verifies every quote against the
    mirror. Return the digest id (the lead page's id when you split) and whether it was written or skipped.
  inputs:
    source: {like: find_undigested, element: true}
    collection:
      json_schema: {type: string}
  output:
    json_schema:
      type: object
      title: DigestResult
      required: [digest_id, status]
      properties:
        reason: {type: string}
        status:
          enum: [written, skipped]
          type: string
        digest_id: {type: string}
  tools: [digest_skeleton, read_source, write_note]
---

Use once per undigested mirror, inside a map, turn one of the source worker. Later turns of the same worker may continue this conversation. Do not use to update an existing digest: it always writes a fresh page under a new id, so re-running it over a digested mirror duplicates the digest.
