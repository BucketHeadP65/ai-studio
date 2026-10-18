---
title: Digest new sources
description: >-
  Turns the sources that have arrived in a knowledge base into typed digest pages, each claim quoted from the
  source itself, linked to what the base already holds, with the references they build on queued behind them.
uses: [st.find_undigested.v4, st.digest_source.v11, st.relate.v5, st.extract_references.v5, st.report.v3]
presentation:
  purpose: >-
    Keep a research knowledge base current with what has arrived in it. A source lands as a mirror of the
    original, and until it is digested the base cannot answer from it, link to it, or act on what it says.
  produces: >-
    One digest page per source (a lead page plus a page per major section when the source is long), each claim
    quoted from the mirror and checked against it at index time, typed links from every new digest to the pages
    the base already holds, and a queued request for each reference that passes the base's own source policy.
  owner_participation: >-
    You pick the knowledge base, and the exact sources when you want only some of them. Nothing stops for you
    once the run starts, because every quote is verified when the page is indexed rather than by a reviewer.
    Reading the digests afterwards is yours.
  not_for: >-
    Not for a knowledge base with no source layer, which has nothing to digest and reports zeros. Not for
    re-digesting a source that already has a digest page, which writes a second page under a new id instead of
    updating the first. Not for editing a digest by hand, and not for deciding what a digest should say.
  inputs:
    - {key: collection, label: Knowledge base, control: knowledge-base}
    - {key: picked_sources, label: Only these sources, control: text}
  result_fields:
    - {label: Digests written (recorded), path: values.report.written}
    - {label: Sources skipped (recorded), path: values.report.skipped}
    - {label: Links added (recorded), path: values.report.edges_added}
    - {label: References requested (recorded), path: values.report.requested}
workflow:
  name: research-digest
  version: 16
  description: Find undigested mirrors (all admitted, or exactly the ones picked), digest each one (splitting long sources into a lead plus section pages), relate each digest to the base, queue what each one references, report.
  parameters:
    collection:
      description: The knowledge base to work in, by collection name.
      json_schema: {type: string, minLength: 1}
    picked_sources:
      description: Source ids to digest, comma-separated. Empty digests whatever the base's digest-sources policy entry admits.
      json_schema: {type: string}
      required: false
  result:
    summary: One digest page per source that was digested, with its links into the base and its queued references.
    primary_work: digest_sources
    empty_primary: noop
    artifacts:
      - id: pass_report
        label: What the pass recorded
        source: {node: report}
        media_type: application/json
      - id: reference_decisions
        label: Every reference a source builds on, with its decision and the reason for it
        source: {node: digest_sources}
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
    turns: [digest_source, relate, extract_references]
    inputs:
      collection: {parameter: collection}
    on_error: skip
    max_parallel: 4
    abort_after_repeats: 3
  - id: report
    kind: run
    work: report
    inputs:
      digests: {node: digest_sources, turn: relate}
      references: {node: digest_sources}
  output: {node: report}
---

Use on a schedule, one fire per base, with `collection` set to that base. A hand fire may also set `picked_sources`
to digest exactly those mirrors and nothing else. A schedule leaves it empty and gets the policy batch.

The stages: `find_sources` lists the undigested mirrors the base's digest-sources policy entry admits, or exactly the
ones picked. `digest_sources` runs one worker per source, three turns on one conversation (digest_source, relate,
extract_references), up to four sources in flight at once. `report` counts what the pass did. A source whose turns fail
is skipped and the rest carry on. Three consecutive identical failures abort the run as failed. Every quote a digest
carries is verified when indexed, so no gate sits in this workflow. Do not use for a base that declares no source
layer: there is nothing to digest and the pass reports zeros. What each digest requested, saw and dropped, with
reasons, is in the run's values under digest_sources, the chat reads it with run_values.
