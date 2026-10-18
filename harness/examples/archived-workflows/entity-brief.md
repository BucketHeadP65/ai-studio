---
id: wf.entity-brief
type: workflow
title: "Entity brief from the typed graph"
summary: "Given one entity, walk its typed neighbourhood and write a short orientation brief -- what it is, what it needs, what it competes with."
status: draft
aliases:
  - entity brief
  - orientation brief
  - what is this thing
workflow_version: 1
requires:
  - corpus_graph
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

Orientation, not argument: walk the graph two hops from one entity and turn its typed
relations into a brief a newcomer can read.

## Graph

```yaml
name: entity-brief
version: 1
description: Graph walk from one entity, brief written from its typed relations, human gate, publish.
parameters:
  question:
    description: The entity to orient around, by name.
    json_schema:
      type: string
      minLength: 3
nodes:
  - kind: run
    id: graph_neighbourhood
    work: graph_neighbourhood
    inputs:
      question:
        parameter: question
  - kind: run
    id: write_brief
    work: write_brief
    inputs:
      question:
        parameter: question
      graph:
        node: graph_neighbourhood
  - kind: gate
    id: approve_brief
    reviews:
      node: write_brief
    rejected_to: write_brief
  - kind: run
    id: publish_audit
    work: publish_audit
    inputs:
      findings:
        node: approve_brief
output:
  node: publish_audit
```

## Applicability

Use when someone needs to get oriented on one named thing and the useful content is its
*relations* -- what it is a kind of, what it requires, what it is an alternative to.
Cheapest of the research shapes: one graph walk, one drafting pass.

Do not use to settle a contested comparison; a graph walk shows structure, not evidence,
and it will happily present two rivals as neighbours without telling you which wins. That
is what the evidence audit is for.

## Provenance

Authored 2026-07-30. Zero attested runs at authoring time.

## Notes

Deliberately has no text-search leaf. If the brief needs quotes it is the wrong workflow,
and the wrongness should be visible in the shape rather than patched in at run time.
