---
title: Relate one digest
summary: Links from one digest to the pages the base already holds, through the link fields its own page type declares, each justified from both pages.
version: 2
step:
  name: relate_digest
  instructions: |
    The digest id and the collection arrive in this message. Pass the collection as `knowledge_base` to
    every tool you call: search, fetch, read_page, knowledge_base_profile and decide_page. Fetch the digest
    by its id: the answer is its card, its page type first, then its title and summary, and its sections as
    children, each with the ref to fetch it by. If the summary starts with Skipped:, the digest holds no
    findings: return edges_added set to 0 and write nothing.

    Call knowledge_base_profile. Under page_types, the digest's page type lists its `edge_fields`: each field
    a link to another page goes in, whether it `holds` one link or a list, and its relation's `domain`,
    `range` (what the field may point at, often page types joined by ' | ') and `description` (what a link
    of that relation means). Leave alone a field that holds one link, and every field whose range is the
    profile's `entity_page_type`: entity links are written with the digest. Every other field is yours to
    fill.

    Read the digest's sections by the refs its card lists. For each field you fill, search the base for
    pages its range and description fit, starting from what the digest says: what it is about, the names it
    gives, its dates. Search again on what the best candidates raise. Fetch each candidate and read enough
    of it, its card and then the sections it lists that matter, to judge. Add a link only when one sentence
    that holds from both pages justifies it under the relation's description. Never link the digest to
    itself or to a page you have not read.

    When a relation's description says its links point from a later page to an earlier one, compare
    publication dates before you link. The date is the page's publication date field: under the profile's
    vocabulary, the facet whose values are dates, the one each page's card prints. Read it from the page
    itself: call read_page with the collection as `knowledge_base` and the page id as `page_id`, once for
    the digest and once for each candidate you would link. Its `payload` holds the page's `frontmatter`, so
    print only that field's value from it, never the whole payload. Link a candidate only when its date is
    earlier than the digest's. A candidate dated later than the digest gets no link from you, because that
    link points the other way and belongs on the candidate. When a page has no such date, judge the order
    from what the two pages say.

    Write the links you found with one decide_page call: the digest id as `page_id`, no status, so the page
    keeps its own, `add` set to true, and `links` mapping each field you fill to the page ids you found for
    it. With `add` true, each call adds those links to the field and never removes one the field already
    holds, so give only the links you found. A field you leave out keeps what it holds. The answer names
    everything each field now holds. An answer that says indexing is deferred to the end of the run is a
    success. Write nothing when you found no link you can justify.

    Return the digest id and how many links you passed to decide_page.
  inputs:
    digest_id:
      json_schema: {type: string}
    collection:
      json_schema: {type: string}
  output:
    json_schema:
      type: object
      title: RelateDigestResult
      required: [digest_id, edges_added]
      properties:
        digest_id: {type: string}
        edges_added: {type: integer}
  tools: [knowledge_base_profile, search, fetch, read_page, decide_page]
---

Use once per digest, as a turn of its own, after the pages it may link to are indexed. Search does not see a page
written earlier in a run when the base indexes once at the run's end, so run it in a pass after the digests exist.
It reads every field and relation name from the base's profile. Do not use to write entity links: those come with
the digest. Do not use to edit the pages it links to: it writes only the digest handed to it.
