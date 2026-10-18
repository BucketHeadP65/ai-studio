---
title: Digest benchmark documents
description: >-
  Turns the documents of one benchmark dataset into digest pages, each claim quoted from the document itself, with
  no links between digests and no references queued.
uses: [st.find_undigested.v4, st.digest_source.v11, st.count_digests.v2]
presentation:
  purpose: >-
    Give a benchmark dataset's documents digest pages, so its search can be scored over digests as well as over the
    raw chunks of each document. A document lands as a mirror of the original, and until it is digested the base
    holds no page that states what the document claims.
  produces: >-
    One digest page per document, each claim quoted from the mirror and checked against it at index time, and a
    count of the documents written and skipped. Nothing links one digest to another, and nothing is queued.
  owner_participation: >-
    You pick the knowledge base and the exact documents to digest. You may leave the documents empty only when the
    base declares a digest-sources policy entry. Nothing stops for you once the run starts, because every quote is
    verified when the page is indexed rather than by a reviewer. Reading the digests afterwards is yours.
  not_for: >-
    Not for a research knowledge base, whose digests link to what the base already holds and queue the references
    they build on. Not for re-digesting a document that already has a digest page, which writes a second page under a
    new id instead of updating the first. Not for editing a digest by hand, and not for deciding what a digest should
    say.
  inputs:
    - {key: collection, label: Knowledge base, control: knowledge-base}
    - {key: picked_sources, label: Only these documents, control: text}
  result_fields:
    - {label: Digests written (recorded), path: values.report.written}
    - {label: Documents the digest skipped (recorded), path: values.report.skipped}
workflow:
  name: benchmark-digest
  version: 3
  description: Find undigested documents (exactly the ones picked, or all admitted), digest each one, count what was written and skipped.
  parameters:
    collection:
      description: The knowledge base to work in, by collection name.
      json_schema: {type: string, minLength: 1}
    picked_sources:
      description: Source ids to digest, comma-separated. Empty digests whatever the base's digest-sources policy entry admits.
      json_schema: {type: string}
      required: false
  result:
    summary: One digest page per document that was digested.
    primary_work: digest_sources
    empty_primary: noop
    artifacts:
      - id: pass_report
        label: What the pass recorded
        source: {node: report}
        media_type: application/json
  nodes:
  - id: find_sources
    kind: run
    work: find_undigested
    inputs:
      collection: {parameter: collection}
      picked_sources: {parameter: picked_sources}
  - id: digest_sources
    each: source
    kind: map
    over: {node: find_sources}
    turns: [digest_source]
    inputs:
      collection: {parameter: collection}
    on_error: skip
    max_parallel: 4
    abort_after_repeats: 3
  - id: report
    kind: run
    work: count_digests
    inputs:
      digests: {node: digest_sources}
  output: {node: report}
---

Use by hand, one fire per benchmark dataset, with `collection` set to that base and `picked_sources` set to the
documents to digest. Leave `picked_sources` empty only when the base declares a `digest-sources` entry in its
compounding policy. Without one, `find_sources` fails and the run stops.

The stages: `find_sources` lists exactly the undigested mirrors picked, or the ones the base's digest-sources policy
entry admits. `digest_sources` runs one worker per document, one digest_source turn each, up to four documents in
flight at once. `report` counts the digests written and the documents the digest skipped. A document whose turn
fails is left out of both counts, and the rest carry on. It stays undigested, and the next run picks it up. Three
consecutive identical failures abort the run as failed. Every quote a digest carries is verified when indexed, so no
gate sits in this workflow.

It does not relate a digest to other pages and does not queue the references a document cites. Use the research
template's digest workflow for a base that wants both. The reason for each skipped document is in the run's values
under digest_sources.
