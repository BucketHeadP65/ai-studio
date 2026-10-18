---
id: wf.quarterly-report
type: workflow
title: "Quarterly revenue report"
summary: "Fetch sales and costs for a fiscal quarter in parallel, reconcile them, draft the report, human-approve, publish."
status: synthesized
aliases:
  - quarterly report
  - revenue report
  - finance report
workflow_version: 3
requires:
  - warehouse_sql
  - filesystem
  - approval
composes: []
tasks:
  - reporting
domains:
  - finance
stages:
  - attested
---

## TL;DR

The recurring finance report: two independent warehouse fetches run concurrently, a
deterministic reconciliation joins them, an agent drafts the prose, an owner approves
before anything is published.

## Graph

```yaml
name: quarterly-report
version: 3
description: Parallel fetches, reconcile, draft, human-approve, publish -- the recurring finance report shape.
parameters:
  quarter:
    description: The fiscal quarter to report on, e.g. Q2-2026.
    json_schema:
      type: string
      pattern: ^Q[1-4]-[0-9]{4}$
nodes:
  - kind: run
    id: fetch_sales
    work: fetch_sales
    inputs:
      quarter:
        parameter: quarter
  - kind: run
    id: fetch_costs
    work: fetch_costs
    inputs:
      quarter:
        parameter: quarter
  - kind: run
    id: reconcile
    work: reconcile
    inputs:
      sales:
        node: fetch_sales
      costs:
        node: fetch_costs
  - kind: run
    id: draft_report
    work: draft_report
    inputs:
      ledger:
        node: reconcile
  - kind: gate
    id: approve
    reviews:
      node: draft_report
    rejected_to: draft_report
  - kind: run
    id: publish
    work: publish
    inputs:
      report:
        node: approve
output:
  node: publish
```

The two fetches are wired independently, so the compiler emits a real fork and a join --
the parallelism is declared by the artifact, not hand-coded in an orchestration script.
`reconcile` is pure computation with no model in it; only `draft_report` improvises.

## Applicability

Use for a recurring finance report over one fiscal quarter, with revenue and costs coming
from the standard warehouses and an owner signing off before publication. The `quarter`
parameter is schema-constrained (`Q[1-4]-YYYY`), so a malformed period fails at the wire
before a single warehouse query runs.

Do not use for ad-hoc analyses, non-quarterly periods, or audits. An audit must not reuse
this report's own reconciliation, or it ends up checking its own arithmetic.

## Provenance

Version 3. Sales must be fetched net of returns; version 2 double-counted them, which is
why the drafting leaf reads a reconciled ledger rather than the raw pulls.

## Notes

The gate's rejection path rewinds to `draft_report`, never to the fetches -- rejecting
prose must not re-bill the warehouse. The validator enforces that `rejected_to` points
upstream of the gate, so a sideways rewind cannot be authored by accident.
