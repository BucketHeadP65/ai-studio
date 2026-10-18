---
id: mu.enforcement-tiers
aliases: [mu.enforcement-tiers]
type: memory-unit
title: "Disclosure enforcement: three honesty tiers"
entity_type: model
summary: A disclosure protocol is only as real as the runtime carrying it — guaranteed (own agents), offered (MCP, server-enforced), conventional (filesystem).
status: active
---

## Statement

A disclosure *convention* is worthless if the agent can ignore it, so the protocol's honesty is a property of the runtime carrying it:

- **Guaranteed** — engram's own pydantic-ai agents: memory is reachable *only* through the three-verb toolset (map / open / search); schemas
  are `Disclosable` models, so each level is a typed view with summary fields at level 0 and the tool **return types are the ladder**; a
  capability holds the descent cursor, enforces per-rung token budgets, and emits the descent path as telemetry — a judgeable retrieval
  trajectory.
- **Offered** — MCP (Claude Code, external agents): the same three verbs, with the **server** enforcing shape (summaries by default, capped
  payloads, node-by-node descent). Client behavior stays uncontrollable.
- **Conventional** — direct filesystem access: unenforceable; schema conventions plus CLAUDE.md discipline are all there is, admitted
  openly.

"Automagically works for agents, always" lands only in the guaranteed tier: embed the disclosure contract once in the schema, make the
toolset generic over it, and every new doc kind gets map/open/search behavior with zero new code. That is the engram-owns-the-runtime
argument for building the agent surface on pydantic-ai rather than relying on an external agent's goodwill.

## Provenance

Factored from [[bs.memory-engineering-lifecycle]] §9(h), with the three-verb surface from §9(e) — brainstorming session 2026-08-08.
