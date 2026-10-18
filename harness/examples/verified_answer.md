---
title: Verified answer over a indexed knowledge base
description: Gather evidence from a indexed knowledge base, draft a cited answer with an agent, human-approve, publish.
workflow:
  name: verified-answer
  version: 1
  description: Evidence-gather, agent-draft, human-gate, publish -- the research answer shape.
  parameters:
    question:
      description: The research question to answer with cited evidence.
      json_schema:
        type: string
        minLength: 8
  nodes:
  - kind: run
    id: gather_evidence
    work: gather_evidence
    inputs:
      question:
        parameter: question
  - kind: run
    id: draft_answer
    work: draft_answer
    inputs:
      question:
        parameter: question
      evidence:
        node: gather_evidence
  - kind: gate
    id: approve
    reviews:
      node: draft_answer
    rejected_to: draft_answer
  - kind: run
    id: publish_answer
    work: publish_answer
    inputs:
      answer:
        node: approve
  output:
    node: publish_answer
---

## What this is

A workflow artifact as a file: frontmatter carries whatever metadata the surrounding
system wants, the spec sits under one key, and the package validates and indexes it.
Nothing here is a format -- plain YAML frontmatter satisfies several interchange formats
without the loader knowing any of them exist.

`publish_answer` wires its input from the **gate**, not from the draft: publishing an
unapproved answer is structurally impossible, and the validator proves it cold.

```mermaid
flowchart TB
    Q[question] --> G[gather_evidence]
    Q --> D
    G --> D[draft_answer]
    D --> A{approve}
    A -->|approved| P[publish_answer]
    A -->|rejected| D
```

## Notes

Evidence comes from a indexed knowledge base, reached either as an exported bundle (offline) or
live over ranked search on a read-only role. The drafting leaf is a pydantic-ai agent;
the workflow neither knows nor cares.
