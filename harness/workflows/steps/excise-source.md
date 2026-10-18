---
title: Excise one page from a source's removal
summary: One page loses everything it took from a removed source and is rewritten clean, healed of a stray link, or set aside as `deferred` when there is nothing left to keep.
version: 2
step:
  name: excise_source
  instructions: |
    You are handed one item from a source's removal plan and the collection its page lives in. Read the whole
    page first, with read_notes, `knowledge_base` set to `collection` and `path` set to the item's path. Whatever you answer,
    carry the item's own source_id into the result unchanged. It names the source this whole pass is
    removing, not something you work out yourself.

    When the item's kind is heal: the page only linked to the removed source, nothing on it quotes or cites it.
    Drop the link the item's drop_link_to names, or re-point it if the page still makes sense pointing somewhere
    else. Run check_note on the corrected page (the same payload write_note would take), fix whatever it flags,
    check again, and only then write_note it with `path` set to the item's path. Answer status healed, and say in
    detail which link you dropped or re-pointed.

    When the item's kind is rewrite: first check whether the page already carries no trace of the source (no
    quoted text, no claim citing it, no link into its digest pages), which means a prior attempt already landed
    this same excision. If so, do not edit it again: answer status rewritten, detail "already clean".

    Otherwise remove exactly what the item's excise names (the source's own words, the claims that cite it,
    the sentences that lean on it) for the reason the item's why gives, and nothing more. The excise note is
    the whole warrant for what leaves the page: everything it does not name, and everything it explicitly says
    to keep, must still be there once you are done. When you are not sure whether a sentence actually depends
    on the removed source, keep it, and say in detail that you left it in on that doubt. Rephrase whatever needs
    rephrasing so the page still reads as one whole piece, and keep every finding another source still supports.
    When the item also carries a drop_link_to, this page also links to one of the source's own digest pages,
    which is being removed along with the source itself. Drop that link too, or re-point it if the page still
    makes sense pointing somewhere else, the same way a heal item's link is handled.

    Before you write anything, judge whether the excision (not the link drop) would gut the page, leaving it
    with nothing of its own, because the page was substantially about the removed source. If so, do not write a
    rewritten page. Instead call knowledge_base_profile with `knowledge_base` set to `collection`, and look at its decisions. When
    that list names at least one page type, take its first entry: write_note a new page of that entry's own page type, at
    that entry's own open status, following the same page type's guidance and canonical sections (also in
    knowledge_base_profile's page_types), naming the page you could not cleanly excise, why it can't be (that it is
    substantially the removed source), and the inbound links the plan named for it. Name the removed source by
    the item's source_title and digest_page_id, never by writing its bare source_id-shaped identifier into the
    new page. A page that names the id itself would read, on a later trace, as if it still cited the source
    directly. When decisions names no page type at all, write no page: instead put that whole account (the page you
    could not cleanly excise, why, and the inbound links the plan named for it) into detail itself, so the
    run's own report carries it in place of a page. Either way, answer status `deferred`, and say so in detail: name
    the page and its page type when you filed one, or the full account itself when you did not.

    Otherwise, once the rewrite reads clean and whole, run check_note on the draft (the same payload write_note
    would take), fix every finding it raises, check again, and only then write_note it with `path` set to the
    item's path. Answer status rewritten, and summarize in detail exactly what you removed.

    Never invent a claim, a quote, or a link that was not already on the page. Never touch a page other than the
    one this item names.
  inputs:
    item:
      like: plan_items
      element: true
    collection: {json_schema: {type: string}}
  output:
    json_schema:
      type: object
      title: ExciseResult
      properties:
        page_id: {type: string}
        source_id: {type: string}
        status: {type: string, enum: [rewritten, deferred, healed]}
        detail: {type: string}
      required: [page_id, source_id, status, detail]
  tools: [read_notes, check_note, write_note, knowledge_base_profile]
  tier: orchestrator
---

Use once per item inside a map over a source's removal plan, one page per worker, in isolation. Do not use to
rewrite a page for any other reason: it only knows how to remove what one item names, never a general edit.
