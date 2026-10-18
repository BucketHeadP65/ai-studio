---
id: mu.emission-contract
aliases: [mu.emission-contract]
type: memory-unit
title: Aspect = emission contract + subscriber
entity_type: model
summary: Observing an aspect of an agent decomposes into an in-band emission contract (typed spans for that aspect) plus an out-of-band subscriber bound to it.
status: active
depends_on:
  - "[[mu.three-planes-model]]"
---

## Statement

"Observe an aspect of this agent" decomposes into two artifacts: an **emission contract** — the agent promises typed spans and attributes
for that aspect, in-band, cheap, deterministic — **plus a subscriber** bound to that contract, out-of-band, where the judgment lives. The
aspect lives in the contract, not in the agent and not in the observer: the agent-side thing stays small and dumb, and the observer-side
thing is free to be arbitrarily smart because it is off the run path. Three properties the tool-choice worked example established:

- **The contract is aspect-driven and bigger than default tracing.** Judging a *choice* requires recording the *alternatives* — the toolset
  offered at each model-request step (names + schema hash, snapshotted per step because dynamic/deferred toolsets change it), the chosen
  call(s) with untruncated arguments, and step/run linkage. Standard instrumentation logs the call made, not the option set it was chosen
  from, so an aspect cannot be bolted onto whatever OTel happens to emit.
- **Run-plane redaction fights record-plane fidelity.** Guardrails scrub secrets in-band; the record plane needs its own redaction policy,
  and a verdict computed over redacted evidence must disclose that fidelity loss in its provenance.
- **Everything downstream can stay out-of-band.** "Usefulness" — did the tool result change the next step? — is computable from the
  trajectory alone; nothing in the aspect needed to touch the run plane except emission.

## Provenance

Factored from [[bs.judges-operators-telemetry]] §4 and §8 (worked example: tool-choice quality) — brainstorming session 2026-08-08.
