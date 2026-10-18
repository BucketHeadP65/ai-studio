# Example: a brief that recommends a change

A worked example of "Settle one decision from the reading" (`research-decision-brief`).

**How this was made.** No model wrote any of it. The brief below is authored, in the exact shape
`draft_decision_brief` declares, and no drafting step and no reviewer ever ran over it. What is real is the
Markdown and the note: both are the output of `render_decision_brief`, the workflow's own deterministic program,
run over that authored brief in the sandbox. Feed the same brief to the same program and you get these bytes back.
Nothing here is evidence that the workflow has been run, and nothing here was approved by a reviewer.

It is here so you can see the shape of what the workflow leaves you holding before you run it on a decision of your
own, and so a change to the renderer can be read against output somebody has already looked at.

The shape to look at is the third evidence entry. It argues against the recommendation, it is marked
`counterevidence`, and it is stated at full strength with the conditions that make it weaker here said
out loud rather than shaved off. A brief that lists only support is a brief nobody can check, and the
reviewer rejects one.

## What the example asks

| | |
| --- | --- |
| The decision to settle | Should the assistant rerank its five retrieved results with a cross-encoder before answering? |
| Knowledge bases to read | research |
| What you are deciding about | A question-answering assistant over short passages, reading five retrieved results per question. |
| What the decision has to live with | The answer has to stay inside the current per-question cost and latency. |

In a real run this brief would reach the reviewer, which fetches each ref back and answers approve,
reject or cannot judge. Nothing reviewed this one.

## The brief, rendered

````markdown
# Whether to rerank retrieved results before the assistant answers

**Question.** Should the assistant rerank its five retrieved results with a cross-encoder before answering?

**Decision.** Recommend a change

Rerank. Two of the three studies read here gained more from reordering the same results than from retrieving more of them, and the one that did not was working at a scale we are nowhere near.

## Recommendation

Add a cross-encoder reranker over the five results already retrieved, and leave the number retrieved where it is. The gain the sources report comes from what reaches the top two positions, which is exactly what our current ordering leaves to the embedding score alone.

## Evidence

**Supporting**

- Reordering a fixed set of five passages with a cross-encoder raised answer accuracy by more than doubling the number retrieved did. (page:research/paper.rerank-over-recall, table 3)
  > Reranking five beat retrieving ten by a wide margin on every question type we measured.
  Holds when: Measured on short factual questions over passages of about 400 tokens, which is the shape of ours.
- Answer quality tracks what lands in the first two positions far more closely than what lands anywhere else in the prompt. (section:research/talk.what-the-model-actually-reads#findings)
  Holds when: Prompts under about half the context window, which ours are.

**Arguing the other way**

- A cross-encoder pass added 180 milliseconds per question at the size this team was running. (page:research/paper.rerank-latency-at-scale)
  Holds when: Their reranker ran over 50 candidates on shared hardware. Over five candidates on our own hardware the cost is a fraction of that, but it is not nothing and it lands on every question.

## Alternatives considered

- Retrieve ten results instead of five, which the same sources show gaining less while costing more per question.
- Rerank only when the top two embedding scores are close, which is cheaper but leaves the hardest questions unreranked.

## Limitations

Nothing read here measures a reranker over as few as five candidates, so the gain reported is an upper bound rather than what we should expect. No source covers questions that need two documents at once.

## Next action

Run the reranker over the existing five results on a held-out set of our own questions and compare answer accuracy and per-question latency against today's ordering.
````

## The note it proposes

A run proposes this note and writes nothing. Keeping it is a separate act you take afterwards, against a knowledge
base you pick, and you can edit the draft first. The page type, the sections and the status all come from the
destination knowledge base's own schema, which is why a base that declares no question page type cannot take this
note.

````json
{
  "page_type": "question",
  "title": "Whether to rerank retrieved results before the assistant answers",
  "summary": "Rerank. Two of the three studies read here gained more from reordering the same results than from retrieving more of them, and the one that did not was working at a scale we are nowhere near.",
  "frontmatter": {
    "status": "synthesized",
    "question_status": "answered"
  },
  "sections": [
    {
      "heading": "TL;DR",
      "body": "Recommend a change. Rerank. Two of the three studies read here gained more from reordering the same results than from retrieving more of them, and the one that did not was working at a scale we are nowhere near.\n\nNext: Run the reranker over the existing five results on a held-out set of our own questions and compare answer accuracy and per-question latency against today's ordering."
    },
    {
      "heading": "Why It Matters",
      "body": "Should the assistant rerank its five retrieved results with a cross-encoder before answering?\n\nNothing read here measures a reranker over as few as five candidates, so the gain reported is an upper bound rather than what we should expect. No source covers questions that need two documents at once."
    },
    {
      "heading": "Current Evidence",
      "body": "**Supporting**\n\n- Reordering a fixed set of five passages with a cross-encoder raised answer accuracy by more than doubling the number retrieved did. (page:research/paper.rerank-over-recall, table 3)\n  > Reranking five beat retrieving ten by a wide margin on every question type we measured.\n  Holds when: Measured on short factual questions over passages of about 400 tokens, which is the shape of ours.\n- Answer quality tracks what lands in the first two positions far more closely than what lands anywhere else in the prompt. (section:research/talk.what-the-model-actually-reads#findings)\n  Holds when: Prompts under about half the context window, which ours are.\n\n**Arguing the other way**\n\n- A cross-encoder pass added 180 milliseconds per question at the size this team was running. (page:research/paper.rerank-latency-at-scale)\n  Holds when: Their reranker ran over 50 candidates on shared hardware. Over five candidates on our own hardware the cost is a fraction of that, but it is not nothing and it lands on every question."
    },
    {
      "heading": "Candidate Answers",
      "body": "Add a cross-encoder reranker over the five results already retrieved, and leave the number retrieved where it is. The gain the sources report comes from what reaches the top two positions, which is exactly what our current ordering leaves to the embedding score alone.\n\n**Alternatives considered**\n\n- Retrieve ten results instead of five, which the same sources show gaining less while costing more per question.\n- Rerank only when the top two embedding scores are close, which is cheaper but leaves the hardest questions unreranked."
    }
  ]
}
````

What a run does not do: it does not add the reranker, it does not change any retrieval setting, and it
does not record that you agree with it. Reading the brief and deciding is yours.
