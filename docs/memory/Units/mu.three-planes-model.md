---
id: mu.three-planes-model
aliases: [mu.three-planes-model]
type: memory-unit
title: "Three planes: run, record, judgment"
entity_type: model
summary: Agent telemetry splits into a run plane (in-band, may intervene), a record plane (immutable spans), and a judgment plane (out-of-band consumers).
status: active
---

## Statement

Agent telemetry splits into three planes, separated by tense and by power:

- **Run plane** — the agent + its in-band capabilities (guardrails, advisor, reminders); present; may intervene: block, rewrite, retry.
- **Record plane** — the OTel/Logfire span stream, the trajectory as append-only data; past; no powers, it is the immutable fact base.
- **Judgment plane** — consumers of the record (operators, monitors, judges); future; may only influence: alert, gate, recalibrate.

A judge is therefore not a capability *of* the agent but a consumer *of its evidence*: a capability is in-band and scoped to the run, while
a judge is out-of-band, can never affect the run that produced its signal, and is scoped to a stream or a suite. The only genuinely
agent-side artifact is the **tap** — instrumentation plus a promise about what gets emitted. The record plane is the load-bearing boundary:
**fidelity of emission bounds quality of judgment** (no judge can score tool-choice quality from truncated tool arguments).

The judgment plane holds three consumer species forming an escalation ladder: **operators** (deterministic aggregation over span
populations, run on everything), **monitors** (event-triggered rules or sampled model verdicts on live traffic), **judges** (model verdicts
under a rubric over fresh runs of a golden set). Operators are tripwires that focus judgment; verdicts are re-emitted as spans, so judge
drift is itself observable.

## Provenance

Factored from [[bs.judges-operators-telemetry]] §§1–3 and §6 — brainstorming session 2026-08-08.
