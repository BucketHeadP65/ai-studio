---
title: Digest news articles
description: >-
  Turns the news articles of a filter-first base into digest pages, each claim quoted from the article with its date
  and figure, ready for the link workflow to link their entities.
uses: [st.find_undigested.v4, st.digest_source.v11]
presentation:
  purpose: >-
    Give every news article a digest that SQL can filter: its outlet, author, publication time, publication date,
    category and url as fields, and its claims with their dates and figures. An article lands as a mirror of the
    original, and until it is digested the base holds none of these.
  produces: >-
    One digest page per article, each claim quoted from the mirror and checked against it at index time, and a count
    of the digests written and the articles skipped. The digests carry no entity links until the link workflow runs.
  owner_participation: >-
    You pick the knowledge base and, when you want only some articles, the exact ones by source id. Leave them empty
    and the run takes the batch the base's digest-sources policy entry admits. Nothing stops for you once the run
    starts, because every quote is verified when the page is indexed rather than by a reviewer. Reading the digests
    afterwards is yours.
  not_for: >-
    Not for linking a digest to its entities, which the link workflow, news-link, does once this run has ended. Not for
    linking a digest to earlier digests on the same story, which the relate workflow does once every article is digested
    and linked. Not for re-digesting an article that already has a digest page, which writes a second page under a new
    id instead of updating the first. Not for a research knowledge base, whose digests relate and queue references in
    the same worker. Not for editing a digest by hand.
  inputs:
    - {key: collection, label: Knowledge base, control: knowledge-base}
    - {key: picked_sources, label: Only these articles, control: text}
  result_fields:
    - {label: Digests written (recorded), path: values.report.written}
    - {label: Articles the digest skipped (recorded), path: values.report.skipped}
workflow:
  name: news-digest
  version: 2
  description: Find undigested articles (exactly the ones picked, or the policy batch), digest each one, count what was written and skipped.
  parameters:
    collection:
      description: The knowledge base to work in, by collection name.
      json_schema: {type: string, minLength: 1}
    picked_sources:
      description: Source ids to digest, comma-separated. Empty digests whatever the base's digest-sources policy entry admits.
      json_schema: {type: string}
      required: false
  inline:
    - name: count_written_digests
      source: |
        written = [d for d in digests if d["status"] == "written"]
        skipped = [d for d in digests if d["status"] == "skipped"]
        {"written": len(written), "skipped": len(skipped)}
      inputs:
        digests: {like: digest_source, many: true}
      output:
        json_schema:
          type: object
          title: WrittenDigestCount
          properties:
            written: {type: integer}
            skipped: {type: integer}
          required: [written, skipped]
  result:
    summary: One digest page per article that was digested, with its quoted claims.
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
    max_parallel: 12
    abort_after_repeats: 3
  - id: report
    kind: run
    work: count_written_digests
    inputs:
      digests: {node: digest_sources}
  output: {node: report}
---

Use by hand on a news base, with `collection` set to that base and `picked_sources` set to the articles to digest.
Leave `picked_sources` empty only when the base declares a `digest-sources` entry in its compounding policy. Without
one, `find_sources` fails and the run stops.

The stages: `find_sources` lists exactly the undigested mirrors picked, or the ones the base's digest-sources policy
entry admits. `digest_sources` runs one worker per article, up to twelve articles in flight at once, and each writes the
digest with its quoted claims and their text fields. `report` counts the digests written and the articles skipped. An
article whose turn fails is left out of every count, and the rest carry on. When it fails before the digest is written,
it stays undigested and the next run picks it up. When it fails after, the digest stays written, and the link workflow
links it like any other. Three consecutive identical failures abort the run as failed. Every quote a digest carries is
verified when indexed, so no gate sits in this workflow.

A base that indexes once per run indexes every page this run wrote when the run ends, so start the link workflow and
then the relate workflow after this run has finished. The reason for each skipped article is in the run's values under
digest_sources.
