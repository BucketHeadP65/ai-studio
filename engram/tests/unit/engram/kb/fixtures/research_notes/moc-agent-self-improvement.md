---
id: moc.agent-self-improvement
type: moc
title: "Map: Agent self-improvement — harness, context, and optimization"
aliases: [agent self-improvement map, harness optimization map, self-improving agents]
summary: "Navigation hub for improving a fixed-weights LLM system by optimizing its scaffolding, not its weights: harness evolution (Self-Harness/AHE/Meta-Harness), context/prompt optimization (ACE), the feedback/observability mechanisms behind them, and the benchmarks + open questions."
status: draft
created: 2026-06-14
updated: 2026-06-14
tasks: [agentic-task-execution, text-classification]
domains: [code, mixed]
stages: [harness-optimization, prompt-optimization, context-compilation]
---

## TL;DR

This territory is about making a **fixed-weights** model better by changing what's *around* it — its harness, context, tools, and the loop that edits them — not by training the model. Start with the organizing concept [[self-improving-agent]] and the object being optimized, [[agent-harness]]. Two method families: **harness evolution** (compared in [[self-improving-harness]]) and **context/prompt optimization** (whose central tension is [[when-does-optimization-beat-hand-design]]). The mechanisms that make either work — rich feedback, observability — are the most transferable ideas. Separate from model-side RL (see [[rag-rl]] and the [[hierarchical-rl-credit-assignment]] comparison).

## Map

### Concepts & problems

- [[self-improving-agent]] — the organizing concept: an agent that improves its own deployment from execution evidence.
- [[agent-harness]] — the non-parametric scaffolding being optimized (prompts/tools/memory/runtime).
- [[model-specific-harness-mismatch]] — why a single hand-designed harness scales poorly across models.
- [[brevity-bias]] / [[context-collapse]] — the failure modes of naive context optimization that ACE names.

### Harness evolution (methods + comparison)

- [[self-improving-harness]] — **the comparison**: who optimizes, what's edited, where the gain lives.
- [[self-harness]] — the agent edits its own harness across three base models, regression-gated.
- [[ahe]] — observability-driven joint evolution of the full component set; lifts Terminal-Bench 2 69.7→77.0.
- [[meta-harness]] — external outer-loop search over harness code for a frozen model.

### Context & prompt optimization

- [[ace]] — evolving playbook contexts (offline prompts + online memory); the anchor of this sub-thread.
- [[prompt-optimization]] — the family page; instances [[gepa]], [[miprov2]], [[dynamic-cheatsheet]], [[mce]].
- [[when-does-optimization-beat-hand-design]] — the open question reconciling ACE/Meta-Harness optimism with prompt-opt skepticism.
- [[skillrae]] — skill-based context compilation for retrieval-augmented execution (adjacent skills line).

### Feedback & observability mechanisms (the transferable ideas)

- [[filesystem-feedback-channel]] — give the optimizer raw prior-candidate history; beats scalar scores and summaries.
- [[component-observability]] / [[experience-observability]] / [[decision-observability]] — AHE's three pillars.
- [[nexau]] — the file-level harness substrate that makes components editable and revertible.

### Benchmarks

- [[terminal-bench-2]] — the shared hard-CLI benchmark of the harness cluster (89 tasks; protocol caveats inside).
- [[swe-bench-verified]] — AHE's frozen-harness transfer target.
- [[appworld]] — ACE's agent benchmark.

### Lineage (queued, not yet ingested)

- ADAS · Darwin-Gödel Machine · STOP — prior self-improvement systems; GEPA · Dynamic-Cheatsheet · MCE source papers; AlphaEvolve.
