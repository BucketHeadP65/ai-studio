---
id: mu.lifecycle-as-consolidation
aliases: [mu.lifecycle-as-consolidation]
type: memory-unit
title: The doc lifecycle is memory consolidation
entity_type: model
summary: brainstorming → specs → plans → implementation → governed docs is episodic-to-semantic consolidation; the lifecycle encodes the stages but not the write policy.
status: active
---

## Statement

Project knowledge has four layers with different lifetimes: **L0 procedural** (how we work — CLAUDE.md, skills), **L1 semantic** (what the
system is and why — governed docs, ADRs, CONTEXT-MAP), **L2 working knowledge** (ideas in flight, findings, decisions-so-far — the lifecycle
dirs), **L3 session state** (context window, `docs/implementation/`). L0/L1 are solved-ish and L3 is the harness's job; the pain
concentrates in L2, whose findings are trapped inside prose docs with no format, no shared layer, and no retrieval path.

The lifecycle brainstorming → specs → plans → implementation → promotion into governed docs is **episodic-to-semantic memory
consolidation**, the hippocampus-to-cortex move: a brainstorm is episodic (what we found, in order, with dead ends), an approved spec is
semantic (what is true and decided, dead ends dropped), an ADR is maximally consolidated (one decision, its context, its consequences). The
lifecycle already encodes the stages; what it does not yet encode is the **write policy** — what is extracted at each promotion, what is
duplicated versus referenced, what decays or gets superseded.

Two consequences. Cold-start is a **retrieval problem, not a documentation problem**: the target is a retrieval-to-context pipeline
("working on X" → retrieve X's memory pack → admit it under a token budget), which needs memory addressable *by aspect* — which prose is
not. And the factoring move follows: **documents keep narrative and decisions; shared knowledge moves to memory units; documents link to
them.** A shared memory is just a unit with more than one inbound link — one owner, and contradictions become detectable.

## Provenance

Factored from [[bs.memory-engineering-lifecycle]] §§1–3 — brainstorming session 2026-08-08.
