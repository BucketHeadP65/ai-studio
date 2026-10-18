---
title: Trace a source's removal
summary: Every page a removed source's mirror or digests touch, sorted into what must be rewritten and what only needs a dropped link, or a refusal when the base has nothing of it.
version: 1
step:
  name: trace_removal
  instructions: |
    Call trace_source with `knowledge_base` set to `collection` and `source` set to `source`. It answers the pages this base's
    copy of that source touches, and the file paths that source's own workflows (its mirror, its digest, its
    section pages) would leave behind once removed.

    When trace_source refuses (the source is not in this base, or nothing in the base points at it), do not
    guess. Answer the RemovalPlan with `source_id` set to the source you were given, `source_title` and
    digest_page_id left empty, expected_paths, rewrites and heals all empty, nothing_found set to true, and the
    refusal's own message copied verbatim into only_x_summary.

    Otherwise, read trace_source's own answer for what it resolved: source_id is its source.source_id, never the
    raw title or URL you were given, source_title is its source.title, and digest_page_id is the id of its first
    only_x.digest_pages entry (empty when it named none). Copy expected_paths straight through from what
    trace_source answered, unchanged. For every candidate page it named, read the whole page with read_notes,
    `knowledge_base` set to `collection` and `path` set to the candidate's path. Use fetch only if a link needs checking
    against something outside this base before you can tell what it points to. Then decide which of two things
    is true:

    - The page quotes the source's own words, restates one of its claims, or leans a sentence on what the source
      said. Add it to rewrites: its page_id and path, why naming the sentence or claim that depends on the
      source, and excise naming exactly what has to go: the quoted lines, the claims that cite it, the
      sentences that lean on it. Name only what must be removed, never the whole page. When the same page also
      links to one of the source's own digest pages (one of only_x.digest_pages, which is being removed along
      with the source), also set drop_link_to on this same item, naming that link or the linked id. A rewrite
      item can carry both an excise and a drop_link_to when the page does both.
    - The page only links to the source or to something the source produced, with nothing quoted and no claim
      resting on it. Add it to heals instead: its page_id and path, and drop_link_to naming the link, or the
      linked id, that has to be dropped or re-pointed.

    Decide which list a page goes in by whether removing the source would leave a sentence unsupported
    (rewrites) or only break a link (heals). A page never appears in both lists, though a single rewrite item
    may itself carry a drop_link_to as described above.

    Return the finished RemovalPlan: source_id, source_title and digest_page_id set as resolved above,
    expected_paths copied through verbatim, rewrites and heals filled as above, nothing_found left false, and
    only_x_summary left empty.
  inputs:
    collection: {json_schema: {type: string}}
    source: {json_schema: {type: string}}
  output:
    json_schema:
      type: object
      title: RemovalPlan
      properties:
        source_id: {type: string}
        source_title: {type: string}
        digest_page_id: {type: string}
        only_x_summary: {type: string}
        expected_paths: {type: array, items: {type: string}}
        rewrites:
          type: array
          items:
            type: object
            title: RewriteItem
            properties:
              page_id: {type: string}
              path: {type: string}
              why: {type: string}
              excise: {type: string}
              drop_link_to: {type: string}
            required: [page_id, path, why, excise]
        heals:
          type: array
          items:
            type: object
            title: HealItem
            properties:
              page_id: {type: string}
              path: {type: string}
              drop_link_to: {type: string}
            required: [page_id, path, drop_link_to]
        nothing_found: {type: boolean}
      required: [source_id, expected_paths, rewrites, heals]
  tools: [trace_source, read_notes, fetch]
  tier: orchestrator
---

Use at the head of a source-removal pass, once per source, with collection and source both given. Reads only.
It never writes or deletes anything, so running it twice is free. Do not use to remove a source this base does
not track: trace_source refuses it and this step reports that refusal rather than inventing a plan.
