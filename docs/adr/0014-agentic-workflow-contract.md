---
id: adr-0014-agentic-workflow-contract
aliases: ["adr-0014-agentic-workflow-contract"]
type: adr
app: root
status: proposed
absorbs: ["[[spec-2026-08-29-agentic-workflow-contract]]"]
tags: []
title: An agentic workflow is a deterministic spine that calls agentic steps
---

# An agentic workflow is a deterministic spine that calls agentic steps

**Status:** Proposed (2026-08-29). Absorbs
[the agentic workflow contract design](../initiatives/2026-08-29-agentic-workflow-contract/design.md).

## Context

The gate that reviewed three drafted ADRs on the 2026-08-29 promote run was the session's own chat agent, carrying forty
tools and handed a 4,000 character preview of one draft out of the three. It spent thirteen minutes at the gate and then
approved, with a caveat that nobody downstream reads. It had two answers available, approve or reject, so a reviewer that
cannot judge what it was handed has nowhere to say so.

## Decision

An agentic workflow is a deterministic spine that calls agentic steps. The spine owns order, parallelism, retries,
persistence, budgets, and state, and it never asks a model what runs next. A step is a function whose body is a model
turn, with typed input, typed output, the smallest tool set that does the job, a budget known from the first turn, and
examples that show it works. This replaces "a workflow is a DAG spec" as the sentence the vocabulary serves.

The definition holds the vocabulary to six practices.

1. A step reads its whole input or an explicit page of it, and it never truncates silently.
2. A reviewer holds read tools only, and it answers approve, reject with reasons, or cannot judge.
3. The budget lives in the step's own contract.
4. Control flow changes only through a typed verdict that the spine interprets.
5. Every step is evaluated on its own before a workflow trusts it.
6. Every step's full input and output stays retrievable afterward.

## Consequences

The vocabulary now carries a judge answered by a brought step, with three verdicts where there were two. It carries a
budget declared on the step's own contract, a read set for a judge declared on the registry rather than assumed,
examples that ride beside a step's own block, and a validator guard for the one wiring shape the engine currently runs
wrong.

The orchestrator gate stays, and so do the three gate modes, wait, orchestrate, and auto. The research workflows keep
their orchestrator gate as reviewer until the orchestrator's own redesign.
