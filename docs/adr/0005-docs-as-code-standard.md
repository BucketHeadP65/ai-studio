---
id: adr-0005-docs-as-code-standard
aliases: ["adr-0005-docs-as-code-standard"]
type: adr
app: root
status: accepted
tags: [docs]
title: "0005 — Docs-as-code standard: Diátaxis + arc42-lite, federated topology, split-doc masters"
---

# 0005 — Docs-as-code standard: Diátaxis + arc42-lite, federated topology, split-doc masters

- **Status:** accepted
- **Date:** 2026-06-12
- **Deciders:** repository maintainers (docs-standardization effort)
- **Supersedes / Amends:** none
- **Related:** `docs/adr/0001`–`0004` (the decisions this standard now indexes), the `documenting-codebase` skill (owns the templates and the doc-impact procedure)

## Context

The monorepo accumulates code faster than its architect can review it, and there was no high-level documentation layer: no architecture overview, no tech-stack rationale, no module docs; READMEs uneven (`engram-client/` absent, root three lines). What did exist and work: 9 ADRs (4 root + 5 engram-local), the `engram/CONTEXT.md` glossary, and strong Google-style Python docstrings. Two consumers shape the requirements: a human architect reviewing agent-built code, and agents that must retrieve documentation piecemeal inside a bounded context window. A future wiki compiler (the Knowledge-vault pipeline) should be able to ingest the corpus without restructuring it.

## Decision

Adopt a **docs-as-code** standard with these parts:

1. **Diátaxis + arc42-lite + ADR.** Diátaxis quadrants organize folders (`explanation/`, `reference/`, `how-to/`, `adr/`); one arc42-lite architecture master per scope with C4 L1/L2 Mermaid diagrams; ADRs continue in the existing MADR-ish house format.
2. **Federated two-level topology.** Root `docs/` holds cross-app concerns only; each app owns its `docs/` and `CONTEXT.md`; root `CONTEXT-MAP.md` is the single entry point indexing everything, with known gaps as visible TODO rows. `engram/docs/adr/` and `engram/CONTEXT.md` stay where they are.
3. **Split-doc masters + graduation rule.** Architecture masters are split by `scripts/docs/split_doc.py --dir <scope>` into `sections/` + a retrieval-map `index.md`; every H2 carries `TL;DR` + `load-when` (lint-enforced, exit 1). An app section in the root master past ~150 lines graduates to `<app>/docs/architecture/`.
4. **Contract-focused slice reference docs.** One ~100–200-line doc per feature slice under `<app>/docs/reference/` (Purpose / Entry points / Public contract / Runtime flow / Examples / Patterns used / Testing seams) — not per-function API reference, which docstrings own.
5. **Frontmatter schema.** Every governed doc carries `id` (stable kebab, immutable — the future wiki compile key), `type`, `app`, `status`, `tags`; linted by `scripts/docs/validate_docs.py` (id uniqueness, CONTEXT-MAP resolution). `docs/implementation/` and `docs/superpowers/` are workflow state, excluded from the corpus.
6. **Maintenance via tooling, not discipline alone.** The `documenting-codebase` skill owns the standard's templates and the diff→doc-impact procedure; the building-feature workflow's Phase 4 routes detection through it, so docs accrue per feature instead of by backfill campaigns.

## Options considered

- **Single root `docs/` tree (centralized)** — rejected: app-local docs (engram's ADRs, slice references) would live far from the code they bind; engram already proved the app-local pattern works.
- **Full arc42 (12 sections) / full C4 (4 levels, generated)** — rejected: heavyweight for a solo-architect repo; bottom-up generated C4 corpora (the `documenting-architecture` route) duplicate what docstrings already say and rot fast. arc42-lite + curated L1/L2 keeps the master reviewable.
- **Per-function API reference docs (Sphinx/TypeDoc style)** — rejected: redundant with enforced docstrings; high churn, low architect value.
- **Wiki-style claims grammar + registries now** (mirroring the Knowledge vault `_meta/schema.md`) — rejected as YAGNI: stable `id`s, the CONTEXT-MAP walk-root, and load-when keywords are a sufficient compiler contract until a wiki actually ingests this repo.

## Consequences

**Good:** one entry point (`CONTEXT-MAP.md`) for human and agent readers; retrieval-priced architecture sections (load-when + token counts) instead of monolith reads; doc gaps are visible TODO rows; the standard is enforced by two stdlib-only scripts plus Phase 4 routing, not by memory.

**Bad:** frontmatter and TL;DR/load-when discipline is overhead on every governed doc; generated `sections/` add diff noise on master edits; the ~150-line graduation rule needs occasional judgment calls.

**Mitigation:** templates ship in the `documenting-codebase` skill so conformance is copy-paste; the splitter regenerates everything (never hand-maintained); lint failures name the exact missing line.
