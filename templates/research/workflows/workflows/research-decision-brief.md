---
title: Settle one decision from the reading
description: >-
  Turns one decision question into a reviewed brief: a recommendation, the evidence for and against it with the refs
  it was read from, what it does not settle, and a proposed question note you may keep.
uses: [st.draft_decision_brief.v1, st.review_decision_brief.v1, st.render_decision_brief.v1]
presentation:
  purpose: >-
    Answer a decision you are actually facing out of what the knowledge base already holds, rather than reading
    around it again. The point is not a summary of the reading. It is a recommendation with the counterevidence
    kept beside it, so you can see what would have to be true for it to be wrong.
  produces: >-
    One brief you can read in two minutes: the decision (change course, keep the current one, or not enough
    evidence yet), what to do and why, at most eight pieces of evidence each carrying the ref it came from and the
    conditions it holds under, what the brief does not settle, and one next action. Beside it, the same brief as a
    proposed question note for the knowledge base, which nothing writes until you say so.
  owner_participation: >-
    You write the question, pick the knowledge bases to read and say what constrains the decision, and then the run
    goes on its own. A reviewer that did not write the brief reads its sources back and rejects one whose
    recommendation runs past its evidence, which sends the drafting round again. Reading the brief and deciding
    anything is yours, and so is keeping the note.
  not_for: >-
    Not for a question the knowledge base holds no sources on, since it will honestly answer that it cannot settle
    it. Not for gathering reading on a topic, which is what a digest pass is for. Not for carrying the decision out:
    it recommends and stops.
  inputs:
    - {key: question, label: The decision to settle, control: textarea}
    - {key: knowledge_bases, label: Knowledge bases to read, control: knowledge-base}
    - {key: project_context, label: What you are deciding about, control: textarea}
    - {key: constraints, label: What the decision has to live with, control: textarea}
  result_fields:
    - {label: Decision (recorded), path: values.review.decision}
    - {label: Next action (recorded), path: values.review.next_action}
  actions:
    - kind: save_note
      id: keep_as_question
      label: Keep this as a question note
      artifact_id: retention_draft
      destination: user_selects
workflow:
  name: research-decision-brief
  version: 1
  description: Draft a decision brief from the knowledge bases, have it reviewed against its own sources, then render it.
  parameters:
    question:
      description: The decision to settle, as a question.
      json_schema: {type: string, minLength: 1}
    knowledge_bases:
      description: The knowledge bases to read, by collection name.
      json_schema:
        type: array
        items: {type: string}
        minItems: 1
    project_context:
      description: What you are deciding about, in your own words.
      json_schema: {type: string}
    constraints:
      description: What the decision has to live with, one per line.
      json_schema: {type: string}
  result:
    summary: A research decision brief is ready for your review.
    primary_work: draft
    required_gates:
      - {gate_id: review, accepted_sources: [judge]}
    artifacts:
      - {id: brief, label: Decision brief, source: {node: present}, field: document, media_type: text/markdown}
      - {id: retention_draft, label: Proposed knowledge note, source: {node: present}, field: note_draft, media_type: application/json}
      - {id: evidence, label: Structured evidence, source: {node: review}, media_type: application/json}
  nodes:
    - id: draft
      kind: run
      work: draft_decision_brief
      inputs:
        question: {parameter: question}
        knowledge_bases: {parameter: knowledge_bases}
        project_context: {parameter: project_context}
        constraints: {parameter: constraints}
    - id: review
      kind: judge
      work: review_decision_brief
      reviews: {node: draft}
      inputs:
        question: {parameter: question}
        constraints: {parameter: constraints}
      approved_when: verdict
      rejected_to: draft
    - id: present
      kind: run
      work: render_decision_brief
      inputs: {brief: {node: review}}
  output: {node: present}
---

Use on demand, with one decision question and the knowledge bases that hold the reading behind it.

The stages: `draft` reads the question, the project context and the constraints, searches each knowledge base named,
fetches every source it relies on and writes the structured brief. `review` is the judge, a separate conversation
that did not write the brief: it fetches the brief's own refs, checks that each source says what the entry claims,
and answers approve, reject or cannot judge. A rejection sends `draft` round again carrying the reviewer's feedback,
and a cannot judge leaves the brief with the person rather than passing it on. `present` is plain code with no model
call: it joins the approved brief into Markdown and into a proposed question note, and writes nothing anywhere.

Keeping the note is a separate act you take afterwards, over the proposed note the run declares as an artifact. A
brief that concludes the current course holds, or that the sources do not settle the question, is a finished run and
not a failed one. Do not use it against a knowledge base whose schema declares no question page type: the proposed
note names that page type and its canonical sections.
