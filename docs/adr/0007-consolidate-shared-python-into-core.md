---
id: adr-0007-consolidate-shared-python-into-core
aliases: ["adr-0007-consolidate-shared-python-into-core"]
type: adr
app: root
status: accepted
absorbs: ["[[spec-2026-08-08-core-consolidation-and-claude-md-split-design]]"]
tags: []
title: "ADR-0007 — Consolidate shared Python into core"
---

# ADR-0007 — Consolidate shared Python into core

**Status:** Accepted (2026-08-08). Supersedes [ADR-0006](0006-engramai-shared-ai-library.md).

**Context.** ADR-0006 extracted the shared AI stack into a second library, `engramai`, owning the
PEP 420 `engram.ai` namespace. In practice the split created three frictions. The namespace was
dishonest: `engram.ai` reads as app-internal while being a separate library — 73 import sites in
the app looked like app code importing itself. The two shared libraries doubled the ripple rule
(every shared change had to run two library suites plus all consumers). And the boundary question
("does this go in core or engramai?") had no principled answer for infrastructure like the CQRS
kernel, the logging facade, and the DI `Resolver` port, none of which are AI concerns. The two
libraries' consumer sets were identical (`engram`, `workflows`), so the split bought no dependency
isolation in exchange.

**Decision.** One shared Python library: `core`. The engramai modules land by concern, not
wholesale: `core.cqrs` (bus, messages, uow), `core.observability`, and `core.resolver` are plain
infrastructure; `core.ai` carries the genuinely-AI surface (model/embedding factories, registry,
capabilities, `retrieval`, `bridge`), re-exporting the factory functions at the package root.
`core.resolver` (the port) deliberately coexists with engram's app-local `src/resolver.py` (the
`ContainerResolver` adapter): port in the library, adapter in the app.

**Consequences.**
- (+) The boundary is honest: `core.*` = shared, everything else = app-local.
- (+) One ripple rule: a change in `core` runs the core, engram, and workflows gates — one list.
- (+) The "which shared lib" question is gone; infrastructure vs AI is expressed inside one
  library's module tree instead of across two distributions.
- (−) `core`'s dependency set grows (azure-identity, logfire, openai, pydantic-ai). Accepted:
  every current consumer already installed them via engramai; a future lightweight consumer can
  motivate an extras split (`core[ai]`) — not needed today (YAGNI).
- (−) History: ADR-0006's extraction rationale (keeping the wiki app honest about what is
  app-specific) is preserved — the *library* boundary stays; only the two-distribution shape ends.
