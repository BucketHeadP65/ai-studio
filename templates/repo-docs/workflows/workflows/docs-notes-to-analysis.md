---
title: Fold notes into analyses
description: >-
  Turns every open owner thread on an initiative's files into a numbered finding in that initiative's analysis
  page, and answers each thread with where it went before resolving it.
uses: [st.initiative_threads.v1, st.fold_notes.v1, st.write_analyses.v1, st.fold_report.v1]
presentation:
  purpose: >-
    Get the owner's questions and observations out of the margins of an initiative's files and into that
    initiative's own analysis, where whoever picks the work up next will actually read them.
  produces: >-
    One rewritten analysis page per initiative that had open threads, its findings numbered on from the ones the
    page already carried, and every thread it folded resolved with a reply naming the finding it became.
  owner_participation: >-
    You leave the threads before the run and pick the knowledge base. The run itself is unattended and has no
    gate, so nothing waits for you. Afterwards the threads it folded are resolved, and anything you still want
    open you reopen yourself.
  not_for: >-
    Not for editing an analysis by hand, since it rewrites the page whole from the threads it folds. Not for
    threads on files outside an initiative folder, which it passes over. Not for resolving a thread without
    folding it into an analysis first.
  inputs:
    - {key: collection, label: Knowledge base, control: knowledge-base}
  result_fields:
    - {label: Initiatives written (recorded), path: values.report.initiatives}
    - {label: Threads resolved (recorded), path: values.report.threads_resolved}
workflow:
  name: docs-notes-to-analysis
  version: 2
  description: List the initiatives with open threads, draft each analysis from its threads, write the analyses and resolve the threads, report.
  parameters:
    collection:
      description: The knowledge base to work in, by collection name.
      json_schema: {type: string, minLength: 1}
  result:
    summary: One rewritten analysis per initiative that had open threads, with every thread it folded resolved.
    primary_work: fold_notes
    empty_primary: noop
    artifacts:
      - id: written_analyses
        label: The analyses written, each with how many threads it resolved
        source: {node: write_analyses}
        media_type: application/json
      - id: fold_report
        label: What the pass recorded
        source: {node: report}
        media_type: application/json
  nodes:
    - kind: run
      id: list_threads
      work: initiative_threads
      inputs: {collection: {parameter: collection}}
    - kind: map
      id: fold_notes
      turns: [fold_notes]
      over: {node: list_threads}
      each: initiative
      inputs: {collection: {parameter: collection}}
      max_parallel: 2
      on_error: skip
    - kind: run
      id: write_analyses
      work: write_analyses
      inputs:
        drafts: {node: fold_notes}
        initiatives: {node: list_threads}
        collection: {parameter: collection}
    - kind: run
      id: report
      work: fold_report
      inputs: {written: {node: write_analyses}}
  output: {node: report}
---

Use on demand, with `collection` set to the repo knowledge base, after the owner has left threads on initiative files.

The stages: `list_threads` groups every open thread by its initiative folder and reads the folder's current analysis
when one exists. `fold_notes` runs one agent step per initiative, two at a time, each drafting that initiative's whole
analysis from its threads and the current page. `write_analyses` writes each draft under its own initiative's
analysis.md and resolves every thread the draft folded, replying with where it went. `report` counts the pass. An
initiative whose draft fails is skipped and the rest carry on. Do not use to edit an analysis by hand: it rewrites
the page whole from the threads it folds.
