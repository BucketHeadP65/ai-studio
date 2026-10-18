---
id: hub.judges-thread
aliases: [hub.judges-thread]
type: hub
title: "Thread: judges and operators over agent telemetry"
summary: Routing node for the agent-observability thread — the plane taxonomy, the binding algebra, the emission contract, and verdict-as-evidence doctrine.
status: active
members:
  - "[[mu.three-planes-model]]"
  - "[[mu.verdict-binding-algebra]]"
  - "[[mu.emission-contract]]"
  - "[[mu.evidence-kinds-doctrine]]"
---

## Overview

The thread opens on a category mismatch — "what would a judge be, in terms of a capability?" — and dissolves it: capabilities are in-band,
judges are not. Read the units in dependency order. [[mu.three-planes-model]] is the taxonomy that does the dissolving (run / record /
judgment) and names the three consumer species. [[mu.verdict-binding-algebra]] unifies those species as one scoring function under different
bindings. [[mu.emission-contract]] is the only agent-side obligation the design imposes. [[mu.evidence-kinds-doctrine]] governs what a
verdict is allowed to be.

[[mu.evidence-kinds-doctrine]] is also a member of [[hub.memory-thread]]: it belongs to the project's standing trust rules rather than to
either brainstorm, and whether the two threads share literally one doctrine is itself an open fork.

Two doctrines from [[bs.judges-operators-telemetry]] §5 ground the units without being compressed into any of them. **Evaluation is a
controlled experiment; monitoring is field observation.** The suite fixes inputs (cases), controls execution (fresh runs, not production
traces), and compares against a baseline; those lab conditions buy comparability across time, so "did my change make it worse?" is only
answerable when the inputs are held still. Monitors answer the complement under field conditions — "is reality drifting away from what my
lab covers?" Neither substitutes for the other, which is why the monitor and eval-judge bindings both have to exist.

**The unit of judgment is the trajectory, not the answer.** An agent can emit the right answer through broken reasoning (and fail on the
next similar input), or the right answer at 47 tool calls (a cost problem), or the right answer having retrieved garbage and gotten lucky (a
reliability problem) — so output eval, process eval, and memory/learning eval catch different failure modes. The Logfire trace *is* the
trajectory, which is why the record plane's fidelity is the real substrate of the whole system, and why the signature in
[[mu.verdict-binding-algebra]] takes a Trajectory rather than an answer.

Decisions the thread took, recorded in [[bs.judges-operators-telemetry]] §9 rather than in a unit: **venue** — stay conceptual for now and
refine the model before picking a substrate (2026-08-08; the options were an engram feature, a pydantic-ai-harness contribution, or a
standalone experiment); **replayable tool backends** — tiered (2026-08-08): frozen fakes and recorded fixtures for the regression
suite, plus a small live-tool canary whose distinct purpose is to measure the fidelity gap between frozen and live environments rather than
agent regression, so a stale fixture set becomes detectable instead of silently misleading.

Still open in the doc: auditing what engram/workflows spans carry today, verdict storage (trace annotations vs an app table), judge
calibration (who judges the judges), authority policy per verdict kind, and the record-plane redaction policy. Further material the doc
carries that no unit compresses includes the assembled control loop (operators → monitors → golden-set minting → judges → verdicts
re-emitted as spans), the golden set as a frozen distribution repaired by the drift-as-trigger loop, and the CQRS mapping (operator =
projection, monitor = event-triggered policy, judge = scheduled process producing evidence documents).
