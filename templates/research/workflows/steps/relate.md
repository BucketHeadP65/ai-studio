---
title: Relate a digest
summary: Typed edges from one new digest to the pages the base already holds, justified from both sides, and a skipped digest passes through untouched.
version: 5
step:
  name: relate
  instructions: |
    You wrote this digest yourself on your previous turn, and its result arrives in this message with
    the collection. If the digest's status is skipped, return it unchanged with edges_added set to 0
    and do nothing else.

    You already hold the digest's content in this conversation, so fetch only what you have not seen.
    Search the base for pages it builds on, is an alternative to, or applies to, then search again on
    the themes the best candidates raise. Read each candidate. For every relation you can justify
    with one sentence that holds from both pages, add it to the digest's frontmatter (builds_on, alternative_to,
    applies_to, mentions as lists of link strings, each exactly '[[page.id]]' with the brackets: a bare id, a 'page:'
    prefix, or markdown link syntax is refused as not a unit link) and rewrite the digest through write_note under
    the same id with the same sections, passing the whole payload (frontmatter, sections, claims). Add nothing you
    cannot justify from both pages. Keep write_note's citation rules in any section prose you touch:
    a page as [[its-file-stem|its.full.id]], a claim as [[its-file-stem#^cN|^cN]], never bare. Return the digest id, its status, and how many edges you added.
  inputs:
    digest: {like: digest_source}
    collection:
      json_schema: {type: string}
  output:
    json_schema:
      type: object
      title: RelateResult
      required: [digest_id, status, edges_added]
      properties:
        status:
          enum: [written, skipped]
          type: string
        digest_id: {type: string}
        edges_added: {type: integer}
  tools: [search, fetch, write_note]
---

Use as turn two of the source worker, after digest_source. Do not use to edit the older page a digest links to: it rewrites only the digest handed to it.
