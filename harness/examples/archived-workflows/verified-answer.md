---
id: wf.verified-answer
type: workflow
title: "Verified answer over a indexed knowledge base"
summary: "Gather evidence from a indexed knowledge knowledge base, draft a cited answer with an agent, hold it at a human gate, publish only what was approved."
status: draft
aliases:
  - verified answer
  - cited answer
  - research answer
workflow_version: 1
requires:
  - knowledge_base_search
  - approval
composes: []
tasks:
  - research
domains:
  - knowledge
stages:
  - draft
---

## TL;DR

Answer a question from a indexed knowledge base with a citation behind every claim, and publish
nothing a person has not approved.

## Graph

```yaml
name: verified-answer
version: 1
description: Evidence-gather, agent-draft, human-gate, publish -- the cited answer shape.
parameters:
  question:
    description: The question to answer with cited evidence.
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
```

`publish_answer` takes its input from the **gate**, not from the draft. Publishing an
unapproved answer is therefore not a policy but a structural impossibility, and the
validator proves it cold, before anything runs.

## Applicability

Use when someone asks a factual question that must be answered from a indexed knowledge base
with per-claim citations, and a person signs off before the answer goes out. The
retrieval leaf is the seam: any knowledge base with ranked search satisfies it.

Do not use for exploratory browsing with no answer to publish, for questions the knowledge base
does not cover, or anywhere an unreviewed answer could ship. There is no fallback to open
search on purpose -- an answer this shape claims its citations are real, and only
indexed evidence backs that claim.

## Provenance

Authored by hand, then run end to end on 2026-07-30 against a real knowledge base, twice: once
over an exported interchange bundle (lexical, offline) and once over the live indexed
knowledge base with ranked retrieval. The live path reached claim- and comparison-level units the
bundle scan cannot see, so prefer it when citation precision matters.

A rejection at the gate re-ran the drafting leaf only: `gather_evidence=1,
draft_answer=2, publish_answer=1`. Completed work is not repaid.

## Notes

The drafting leaf is an agent -- improvisation fenced inside a typed cell, evidence in
and a cited answer out. Swapping it for a deterministic function changes no part of this
artifact, which is the point of naming work rather than inlining it.
