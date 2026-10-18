# Example: a brief that says the sources do not settle it

A worked example of "Settle one decision from the reading" (`research-decision-brief`).

**How this was made.** No model wrote any of it. The brief below is authored, in the exact shape
`draft_decision_brief` declares, and no drafting step and no reviewer ever ran over it. What is real is the
Markdown and the note: both are the output of `render_decision_brief`, the workflow's own deterministic program,
run over that authored brief in the sandbox. Feed the same brief to the same program and you get these bytes back.
Nothing here is evidence that the workflow has been run, and nothing here was approved by a reviewer.

It is here so you can see the shape of what the workflow leaves you holding before you run it on a decision of your
own, and so a change to the renderer can be read against output somebody has already looked at.

What this example shows is the workflow's `insufficient_evidence` result, which is an answer and not a
failure. It is the only decision allowed an empty evidence list, and the brief spends its length on what is
missing and on what would settle it. The note it proposes leaves the question open rather than claiming it
was answered.

## What the example asks

| | |
| --- | --- |
| The decision to settle | Should long documents be split by their own section headings rather than by fixed token windows before indexing? |
| Knowledge bases to read | research |
| What you are deciding about | An index over long internal documents, split today into fixed 800-token windows with a 100-token overlap. |
| What the decision has to live with | Changing the split means reindexing everything, so it happens once or not at all. |

In a real run this brief would reach the reviewer like any other. An empty evidence list is the one
thing it may only pass on with, and nothing reviewed this one.

## The brief, rendered

````markdown
# Whether to split long documents by section or by fixed token windows

**Question.** Should long documents be split by their own section headings rather than by fixed token windows before indexing?

**Decision.** Not enough evidence yet

No source read here puts the two splits side by side on documents like ours, so the question stays open until we measure it on our own.

## Recommendation

Do not change the split on this reading. What is missing is a comparison on our own documents, not another paper.

## Evidence

Nothing in the knowledge bases read settles this yet.

## Alternatives considered

- Split by section now and watch answer quality, which changes the index before anything says the change is right.
- Wait for a source that compares both, which nothing suggests is coming.

## Limitations

The knowledge base holds three sources on splitting. Two measure fixed windows alone and the third measures section splits on transcripts with no headings at all, so none of them puts the two side by side. Nothing read here measures retrieval quality against documents of our length.

## Next action

Index a hundred of our own documents both ways and compare answer accuracy on the same set of questions.
````

## The note it proposes

A run proposes this note and writes nothing. Keeping it is a separate act you take afterwards, against a knowledge
base you pick, and you can edit the draft first. The page type, the sections and the status all come from the
destination knowledge base's own schema, which is why a base that declares no question page type cannot take this
note.

````json
{
  "page_type": "question",
  "title": "Whether to split long documents by section or by fixed token windows",
  "summary": "No source read here puts the two splits side by side on documents like ours, so the question stays open until we measure it on our own.",
  "frontmatter": {
    "status": "synthesized",
    "question_status": "open"
  },
  "sections": [
    {
      "heading": "TL;DR",
      "body": "Not enough evidence yet. No source read here puts the two splits side by side on documents like ours, so the question stays open until we measure it on our own.\n\nNext: Index a hundred of our own documents both ways and compare answer accuracy on the same set of questions."
    },
    {
      "heading": "Why It Matters",
      "body": "Should long documents be split by their own section headings rather than by fixed token windows before indexing?\n\nThe knowledge base holds three sources on splitting. Two measure fixed windows alone and the third measures section splits on transcripts with no headings at all, so none of them puts the two side by side. Nothing read here measures retrieval quality against documents of our length."
    },
    {
      "heading": "Current Evidence",
      "body": "Nothing in the knowledge bases read settles this yet."
    },
    {
      "heading": "Candidate Answers",
      "body": "Do not change the split on this reading. What is missing is a comparison on our own documents, not another paper.\n\n**Alternatives considered**\n\n- Split by section now and watch answer quality, which changes the index before anything says the change is right.\n- Wait for a source that compares both, which nothing suggests is coming."
    }
  ]
}
````

What to do with it: the next action is the point. A brief like this is worth keeping as an open question
note, so the same ground is not covered again the next time somebody wonders.
