---
title: Fold notes into an analysis
summary: One initiative's whole analysis page, drafted fresh from its annotated files and every open owner thread on them.
version: 1
step:
  name: fold_notes
  instructions: |
    Call knowledge_base_profile for the base and read the brainstorming-doc page type's guidance and sections. You are handed one
    initiative folder, its annotated files with their text and every open thread, and the current analysis text
    when one exists. Write the whole analysis page for that initiative. Every open thread becomes one finding under
    Findings, F<n> numbered on from the last finding the current analysis carries (F1 when it carries none), stating
    what the owner asked or observed, quoting the anchored text, and what the answer or consequence is, in the
    file's own terms. Keep every existing section's content unless a thread changes it. Keep the canonical sections
    in order, drop one that has nothing to say. Copy folder verbatim into the output.
  inputs:
    initiative: {like: initiative_threads, element: true}
    collection: {json_schema: {type: string}}
  output:
    json_schema:
      type: object
      title: FoldedAnalysis
      properties:
        folder: {type: string}
        title: {type: string}
        summary: {type: string}
        sections:
          type: array
          items:
            type: object
            title: Section
            properties:
              heading: {type: string}
              body: {type: string}
            required: [heading, body]
        folded:
          type: array
          items:
            type: object
            title: FoldedThread
            properties:
              path: {type: string}
              note_id: {type: string}
              finding: {type: string}
            required: [path, note_id, finding]
      required: [folder, title, summary, sections, folded]
  tools: [knowledge_base_profile, fetch, search]
---

Use once per initiative, inside a map over initiative_threads. It is handed one initiative's files, their text and
every open thread, and the current analysis text when one exists. Do not use to patch an analysis by hand. It
rewrites the whole page from the threads and the files it is handed.
