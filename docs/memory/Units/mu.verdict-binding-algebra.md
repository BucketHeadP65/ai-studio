---
id: mu.verdict-binding-algebra
aliases: [mu.verdict-binding-algebra]
type: memory-unit
title: One judge function, many bindings
entity_type: model
summary: A judge is the pure function Trajectory × Rubric → Verdict; guardrail, monitor, and eval judge are that one function under different trigger, input, scope, and authority.
status: active
depends_on:
  - "[[mu.three-planes-model]]"
---

## Statement

Stripped of operational clothing a judge is a pure function, `judge : Trajectory × Rubric → Verdict`. Everything else is a **binding**:
```
binding = (trigger, input source, scope, authority)
  trigger      ∈ {in-band hook, event, window, schedule, release}
  input source ∈ {live run (in flight), live trace (emitted), replayed trace, fresh run over golden case}
  scope        ∈ {one run, window, suite}
  authority    ∈ {block this run, alert, annotate, gate deploy, report only}
```
The same scoring function bound three ways yields three familiar things: in-band / one run / authority=block is a **guardrail** (the
degenerate judge whose verdict has immediate power — the only binding that is legitimately a capability); on-event / live traces /
authority=alert is a **monitor**; on-schedule / golden suite / authority=gate-or-report is an **eval judge**.

So the useful question is not "what is a judge as a capability?" but **"what binding does this verdict need, and what authority does it
carry?"** — the capability question arises only for the in-band binding. Authority is where Goodhart bites: a number safe as an operator's
descriptive metric (tool-call count) turns dangerous once a verdict makes it normative; prefer usefulness measures over count proxies.

## Provenance

Factored from [[bs.judges-operators-telemetry]] §4, with the authority/Goodhart rule from §5 and §8 — brainstorming session 2026-08-08.
