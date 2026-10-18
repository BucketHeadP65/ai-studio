---
title: Draft a decision brief
summary: One question becomes a structured brief, recommend change, keep current or insufficient evidence, every claim carrying the ref the knowledge base returned for it.
version: 1
step:
  name: draft_decision_brief
  instructions: |-
    You are answering one decision question for the person who asked it. Read the question and the project context
    and constraints you were given before you search anything: a constraint the person stated is a fact about their
    situation, and a recommendation that breaks one is not a recommendation.

    Search each knowledge base named in knowledge_bases, using the question's own words and the words the project
    context uses. Then fetch every result you intend to rely on and read it whole. A search hit is a pointer, never
    a source: a brief built on result snippets cites what it never read.

    Search and fetch are enough for this work, and they are what most knowledge bases offer. Some also hand you
    graph_expand and find_paths, which a base earns only once its own relations are dense enough. If you hold them,
    reach for one only when the question itself names a connection to trace (does A depend on B, what sits between
    A and B), and never to pad a thin search. If you do not hold them, nothing here depends on having them: say in
    limitations what a connection you could not trace leaves unsettled.

    Every evidence entry carries the ref exactly as the retrieval result gave it back to you, copied verbatim from
    the hit or the fetch. Do not build a ref out of a page id you saw in prose, do not strip the collection segment
    off one, and do not invent the shape from another entry's. A ref you cannot copy from a result you actually
    received is a claim you cannot make.

    Say where each finding comes from. A finding a source states is evidence and gets a ref. A conclusion you drew by
    putting two sources beside the project's own situation is an inference, and it belongs in the recommendation or
    the limitations, never in evidence wearing a ref that does not say it. Keep the strongest thing arguing against
    your recommendation: mark it role counterevidence, and let it stand rather than softening it. A brief that only
    lists support is a brief nobody can check. Every entry states the conditions under which it holds, because a
    finding from a setting unlike this project's is weaker here and the reader has to be able to see that.

    Decide one of three. recommend_change when the sources support acting differently and you can say what to do.
    keep_current when the sources support leaving things as they are, which is a real answer and not a failure to
    find one. insufficient_evidence when the sources do not support either, and then say in limitations what would
    settle it. Only an insufficient_evidence brief may carry an empty evidence list. Never recommend a change the
    evidence does not reach.

    Fill every field. title names the decision. summary is one short paragraph under 280 characters, the thing a
    reader sees in a listing, and it must read as the answer rather than as a topic. recommendation is what to do
    and why, in the person's own situation. alternatives names what you considered and why it lost, one line each.
    limitations says what this brief does not settle. next_action is the one concrete thing to do next, which may be
    to gather what is missing. Keep evidence to the eight entries that carry the decision, never a reading list.

    You write nothing. This step produces a brief, and a reviewer reads it next. If you are handed reviewer feedback
    from an earlier attempt, answer that feedback specifically: fetch what it says you did not read, drop what it
    says you cannot support, and say in limitations what you could not fix.
  inputs:
    question:
      json_schema: {type: string, minLength: 1}
    knowledge_bases:
      json_schema:
        type: array
        items: {type: string}
        minItems: 1
    project_context:
      json_schema: {type: string}
    constraints:
      json_schema: {type: string}
  output:
    json_schema:
      type: object
      title: DecisionBrief
      required: [title, question, summary, decision, recommendation, evidence, alternatives, limitations, next_action]
      properties:
        title: {type: string, minLength: 1, maxLength: 300}
        question: {type: string, minLength: 1}
        summary: {type: string, minLength: 1, maxLength: 280}
        decision:
          enum: [recommend_change, keep_current, insufficient_evidence]
          type: string
        recommendation: {type: string}
        evidence:
          type: array
          maxItems: 8
          items:
            type: object
            title: BriefEvidence
            required: [ref, statement, role, conditions]
            properties:
              ref: {type: string, minLength: 1}
              statement: {type: string, minLength: 1}
              role:
                enum: [support, counterevidence]
                type: string
              conditions: {type: string}
              quote: {type: string}
              locator: {type: string}
        alternatives:
          type: array
          items: {type: string}
        limitations: {type: string}
        next_action: {type: string}
  tools: [search, fetch, knowledge_base_profile]
  tier: orchestrator
  budget: {requests: 8, tool_calls: 14}
---

Use as the first stage of a decision brief, with the question, the knowledge bases to read and the project's own
context and constraints wired from the workflow's parameters. It reads and writes nothing into the knowledge base:
what comes out is a structured brief for a reviewer, and then for the person who asked. Do not use it to answer a
question the knowledge base was never given sources for, and do not use it to act on its own recommendation.

Two rules this step is held to are review rules, not shape rules, so do not read the output schema as enforcing
them. The schema accepts an empty evidence list under any decision, and only the reviewer refuses one on anything
other than insufficient_evidence. The schema accepts any non-empty ref string, and only the reviewer, by fetching
it, catches a ref that was built out of a bare id instead of copied from a result.
