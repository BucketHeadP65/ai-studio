# Example: an initiative's open threads folded into its analysis

A worked example of "Fold notes into analyses" (`docs-notes-to-analysis`).

**How this was made.** No model wrote any of it, and no knowledge base was touched. The three threads and the analysis draft
below are authored, in the exact shape `initiative_threads` and `fold_notes` return. What is real is everything
after them: the `write_note` payload, the three `resolve_note` replies and the recorded counts are the output of
`write_analyses` and `fold_report`, the workflow's own deterministic programs, run over that authored draft in the
sandbox against a note surface that recorded the calls instead of making them. Feed the same draft to the same
programs and you get these bytes back. Nothing here is evidence that the workflow has been run.

This is the second reference automation, and it is here for the contrast. The decision brief answers one question
and stops. This one changes the knowledge base: it rewrites a page and resolves the threads it folded, with no gate
in front of either. Read it to see what an unattended workflow does when it is doing its job, and what it puts back
where the owner will find it.

## What the example asks

| | |
| --- | --- |
| Knowledge base | repo-docs |

Nothing else. A run finds the initiatives that have open owner threads on their own files, so the only thing you
pick is which knowledge base to work in.

## What it started from

Three open threads, as an owner would leave them in the margins of one initiative's files.

| File | Thread | What the owner wrote |
| --- | --- | --- |
| `initiatives/2026-04-08-search-ranking/design.md` | n-14 | Why does this rank by recency before relevance? |
| `initiatives/2026-04-08-search-ranking/design.md` | n-15 | What does a query that matches nothing return? |
| `initiatives/2026-04-08-search-ranking/plan.md` | n-16 | Task 2 needs task 1's schema change, so these cannot be parallel. |

## The page it writes

One `write_note` call, exactly as the program built it. The knowledge base's own renderer turns this payload into
the markdown on disk, so the frontmatter, the id and the path are the program's, and the page's headings are the
schema's canonical sections.

````json
{
  "knowledge_base": "repo-docs",
  "payload": {
    "frontmatter": {
      "id": "bs.search-ranking",
      "aliases": [
        "search-ranking",
        "bs.search-ranking"
      ],
      "type": "brainstorming-doc",
      "title": "Search ranking",
      "summary": "What the owner asked about the ranking change, and where each question was answered or left open.",
      "status": "active"
    },
    "sections": [
      {
        "heading": "Findings",
        "body": "F1. The owner asked why the design ranks by recency before relevance. The design's own ordering section answers it: a stale but exact match was the complaint that started the work, and recency is applied as a tie-break rather than a first sort. The design says so in one clause, which is easy to miss, so it is restated here.\n\nF2. The owner asked what happens to a query that matches nothing. The design does not say. Left open, and it needs an answer before the plan is written, because an empty result and an error look the same to the caller today.\n\nF3. The owner observed that the plan's second task depends on the first one's schema change and is listed as parallel. That is a plan error rather than a design question. The plan has been corrected to run them in order."
      },
      {
        "heading": "Risks and Open Questions",
        "body": "From F2: what a query matching nothing returns, and whether the caller can tell it apart from a failure."
      }
    ]
  },
  "path": "initiatives/2026-04-08-search-ranking/analysis.md"
}
````

Each finding is numbered, each says whether the initiative's own documents answer it, and F2 is left open rather
than answered. That is the point of the pass: it moves the owner's questions out of the margins without pretending
to settle them.

## What it leaves on each thread

Every thread it folds is resolved with a reply naming the finding it became, so the owner can follow one back.

````json
[
  {
    "knowledge_base": "repo-docs",
    "path": "initiatives/2026-04-08-search-ranking/design.md",
    "note_id": "n-14",
    "reply": "Folded into analysis.md as F1."
  },
  {
    "knowledge_base": "repo-docs",
    "path": "initiatives/2026-04-08-search-ranking/design.md",
    "note_id": "n-15",
    "reply": "Folded into analysis.md as F2."
  },
  {
    "knowledge_base": "repo-docs",
    "path": "initiatives/2026-04-08-search-ranking/plan.md",
    "note_id": "n-16",
    "reply": "Folded into analysis.md as F3."
  }
]
````

## What the pass recorded

````json
{
  "initiatives": 1,
  "threads_resolved": 3
}
````

Those two counts are what the workflow's result declares, and they are counts of work done, not a judgment that the
analysis is any good. Reading the page and reopening anything you still want open is yours.
