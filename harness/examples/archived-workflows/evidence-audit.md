---
id: wf.evidence-audit
type: workflow
title: "Evidence audit of a contested claim"
summary: "Interrogate a contested claim three ways at once -- ranked text, typed graph neighbourhood, claim-level records -- then synthesise, flag disagreements, and publish only what a reviewer approved."
status: draft
aliases:
  - evidence audit
  - contested claim
  - disagreement check
  - who contradicts whom
workflow_version: 1
requires:
  - knowledge_base_search
  - knowledge_base_graph
  - knowledge_base_claims
  - approval
composes: []
tasks:
  - research
  - review
domains:
  - knowledge
stages:
  - draft
---

## TL;DR

When a claim is contested, one retrieval strategy is not enough. This runs three in
parallel over the same knowledge base -- ranked text, the typed graph around the entities, and
the claim table with verbatim quotes -- joins them, and asks a model to synthesise
*including where the sources disagree*. A reviewer sees the disagreements before anything
is published.

## Graph

```yaml
name: evidence-audit
version: 1
description: Three-way parallel retrieval over one knowledge base, synthesis that must report disagreement, human gate, publish.
parameters:
  question:
    description: The contested claim or comparison to audit.
    json_schema:
      type: string
      minLength: 8
nodes:
  - kind: run
    id: ranked_text
    work: ranked_text
    inputs:
      question:
        parameter: question
  - kind: run
    id: graph_neighbourhood
    work: graph_neighbourhood
    inputs:
      question:
        parameter: question
  - kind: run
    id: claim_records
    work: claim_records
    inputs:
      question:
        parameter: question
  - kind: run
    id: synthesise
    work: synthesise
    inputs:
      question:
        parameter: question
      text:
        node: ranked_text
      graph:
        node: graph_neighbourhood
      claims:
        node: claim_records
  - kind: gate
    id: approve_findings
    reviews:
      node: synthesise
    rejected_to: synthesise
  - kind: run
    id: publish_audit
    work: publish_audit
    inputs:
      findings:
        node: approve_findings
output:
  node: publish_audit
```

Three independent retrievals means the indexer emits a real fork from the start and a
join before synthesis: they run concurrently and the model waits for all three. The
rejection path rewinds to `synthesise` only -- a reviewer rejecting the *interpretation*
must not re-bill three retrievals.

## Applicability

Use when the answer is genuinely contested and a single search would flatter one side:
benchmark disputes, "X beats Y" claims, anything where the interesting content is the
disagreement between sources. The synthesis leaf is instructed to report disagreement
rather than resolve it, and the gate exists so a human sees that before publication.

Do not use for simple factual lookups -- three retrievals and a review cycle is a lot of
machinery to answer something one search settles. Do not use where the knowledge base holds only
one source per topic: with nothing to disagree, the audit has nothing to audit.

## Provenance

Authored 2026-07-30. Zero attested runs at authoring time.

## Notes

The three retrieval leaves are deliberately different *kinds* of access, not three
flavours of the same one: ranked full-text finds phrasing, the typed graph finds
neighbours the text never mentions, and the claim table carries verbatim quotes with
locators so a citation can be checked rather than trusted.
