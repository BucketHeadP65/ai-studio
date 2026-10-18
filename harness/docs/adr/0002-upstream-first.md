---
id: harness-adr-0002-upstream-first
aliases: ["harness-adr-0002-upstream-first"]
type: adr
app: harness
status: accepted
tags: []
title: "0002 — Upstream first: mount pydantic-ai's primitives, never parallel-build"
---

# 0002 — Upstream first: mount pydantic-ai's primitives, never parallel-build

**Status:** Accepted (2026-08-22). Records the owner's rule and the audit that motivated it.
The binding wording lives in `harness/CLAUDE.md` § Upstream first.

**Superseded in part:** on 2026-08-28, on the model knob decision below. The mounted `SelectModel` this ADR settled on
is itself replaced by three owner-set tiers over a tier store, `harness.adapters.model_tiers`. See
[model-tiers](../reference/model-tiers.md).

## Context

The declared floor (`pydantic-ai>=2.20`) undersold the installed library (2.33 — thirteen minor releases of surface), and a
full inventory found one genuine reinvention (a hand-rolled chat SSE protocol built while `pydantic_ai.ui` shipped) plus two
parallel-evolved primitives (a door wrapper beside `DynamicCapability`, a threaded model knob beside `SelectModel`). The owner's
standing worry: never rebuild what pydantic-ai core already gives.

## Decision

Before designing any agent-side mechanism, inventory the **installed** `pydantic_ai` surface (capabilities, toolsets, `ui`,
`durable_exec`, native tools) and the vendored `pydantic_ai_harness`; then mount, wrap, or extend upstream. A parallel build is
legitimate only when its design doc names the upstream gap it fills. Executed on the audit's findings, same day:

- The chat stream is pydantic-ai's own `VercelAIAdapter` (Vercel AI v5 data stream); the one domain addition is the settle
  payload riding a `data-settle` chunk from `on_complete`, built by the same helper as `/api/chat`.
- `Switched` (the door switch) subclasses `DynamicCapability` with `id: door:<name>` — a closed door mounts nothing at all,
  and door reads are per run, the turn-level promise every surface already made.
- The model knob is a mounted `SelectModel` reading the selection per request step; nothing is threaded at call sites, and the
  ACP door honours the knob for the first time.
- A client disconnect journals the turn abandoned immediately on both doors; run teardown is the library's.

## Consequences

- The one surviving shim is `core.ai.voice.RuntimeVoiced` (upstream has no instruction-position control yet) — re-checked per
  upstream release, retired the day it does.
- The unused upstream shelf (compaction beyond `WarnNearLimits`, guardrails, the prompt-injection defender, system reminders,
  planning, subagents) is the standing answer to those needs when they arise: adopt, never build.
- The vendored `loop_detection` copy is deleted the day its upstream PR merges.
