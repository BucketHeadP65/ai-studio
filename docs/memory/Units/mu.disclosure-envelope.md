---
id: mu.disclosure-envelope
aliases: [mu.disclosure-envelope]
type: memory-unit
title: "Progressive disclosure: provider, spec, consumer"
entity_type: decision
scope: slice-1
summary: Decided 2026-08-08 — the spec defines only a thin conformant result envelope plus a descent convention; no first-party toolset exists anywhere in the design.
status: active
depends_on:
  - "[[mu.enforcement-tiers]]"
---

## Statement

**Decided 2026-08-08.** Progressive disclosure is split across three roles:

- **Provider** — anyone with a database and a retrieval tool (engram first). Owns the data, the compiled summaries, and any maintenance
  hooks, and keeps its existing tools; conformance means its *results* follow the spec.
- **The spec** — defines only the format of a conformant tool result: a **thin envelope** (the disclosed node, its children as summaries,
  opaque refs, and the presentation mode) plus one behavioral requirement, the **descent convention** — the provider's tool accepts a ref
  and returns that node's envelope. No tools are defined by the spec.
- **Consumer runtime** — a generic `ProgressiveDisclosure` pydantic-ai capability: a thin protocol handler that recognizes conformant
  envelopes, governs context injection, offers descent, tracks the cursor, and enforces budgets. It ships no content — no summaries, no
  hooks, no storage.

Thin envelopes buy **server-enforced leveling**: even uncontrolled MCP clients get progressive disclosure with no capability installed, and
the capability adds owned-runtime discipline (cursor, budgets, traces) on top. The decision supersedes the earlier "two consumption points"
version of the same section, in which the consumer built a first-party toolset and parked payloads — the wrong split. Two alternatives are
parked for post-slice-1 revisit: type-level disclosure binding via pydantic's native `ConfigDict`, and who owns the threshold logic.

## Provenance

Factored from [[bs.memory-engineering-lifecycle]] §9(j), with the parked alternatives from §9(k) — brainstorming session 2026-08-08.
