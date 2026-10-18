---
id: adr-0006-engramai-library
aliases: ["adr-0006-engramai-library"]
type: adr
app: root
status: superseded
superseded_by: "[[adr-0007-consolidate-shared-python-into-core]]"
tags: []
title: "0006 — `engramai` shared AI library (`engram.ai`) via a uv path source"
---

# 0006 — `engramai` shared AI library (`engram.ai`) via a uv path source

**Superseded by [ADR-0007](0007-consolidate-shared-python-into-core.md) (2026-08-08).**

- **Status:** superseded by [ADR-0007](0007-consolidate-shared-python-into-core.md)
- **Date:** 2026-07-09
- **Deciders:** repository maintainers (refactoring-ai-core initiative)
- **Supersedes / Amends:** amends [`docs/adr/0001`](0001-wiki-app-placement-and-core.md) (the `core` split — the AI factories move out of `core`) and [`docs/adr/0002`](0002-wiki-cqrs-bus-and-wikinote-kernel.md) (the CQRS bus's home — the bus kernel moves out of engram)
- **Related:** [`docs/adr/0004`](0004-client-core-shared-frontend-package.md) (the frontend analogue of this decision), [engram ADR-0035](../../engram/docs/adr/0035-retrieval-sql-from-descriptors.md) (the descriptor seam engram consumes), [engram ADR-0036](../../engram/docs/adr/0036-behavioral-parity-generated-sql.md) (the parity gate the extraction is validated against)

## Context

engram grew a mature, strategy-pluggable retrieval + agent stack: dual-dialect BM25, a dual graph backend, a named retrieval-strategy axis, a calibratable `RetrievalPolicy`, budget-controlled toolsets, and a streaming agent orchestration. The goal is to reuse this stack across other projects with ergonomic *extend / reuse / override*, not fork it.

Two apps already share app-agnostic code: general Python via `core/` (ADR-0001) and frontend seams via `client-core/` (ADR-0004). But the AI-specific building blocks did not have a shared home. They were split across engram's `src/` (the CQRS kernel, the observability facade, the `Resolver` port, the embedding port, and the whole retrieval machinery) and `core`'s AI corner (`core.ai` model/embedding factories, `core.bridge`). A second consumer wanting only the retrieval stack would have to depend on all of engram — dragging in the wiki domain it does not want.

The generic/domain line, from the extraction review, runs between generic Python control-flow/ports (corpus-agnostic) and SQL-text-in-strings + calibrated prompts + the presentation engine (engram-specific). Almost all the Python is already corpus-agnostic; wireup never leaks into the reusable code (it lives in ~11 composition-glue files, and a sibling app already consumes the same code with plain constructor injection).

## Decision

Create a new sibling package **`engramai`** in the monorepo, import namespace **`engram.ai`** (PEP 420 namespace package `engram`, subpackage `ai`), so consumers write `from engram.ai import ...`. It ships the reusable AI stack as plain, constructor-injectable classes and imposes no DI framework on consumers.

**Packaging & consumption.** `engramai` builds with **poetry-core** (`build-backend = "poetry.core.masonry.api"`, `packages = [{ include = "engram" }]`, `namespace-packages = ["engram", "engram/ai"]`), Python `>=3.13`. engram (and any future consumer) depends on it as a uv **path source** (`engramai = { path = "../engramai", editable = true }`) — uv builds the poetry-core backend fine, so consumers stay on uv and only `engramai` itself uses poetry. This mirrors the `file:`-path discipline ADR-0004 established for `client-core` and the uv path source ADR-0001 established for `core`.

**What moved into `engram.ai`:** `core.ai` → `engram.ai.models`; `core.bridge` → `engram.ai.bridge`; the CQRS kernel → `engram.ai.cqrs`; the observability facade → `engram.ai.observability`; a generic `Registry[K, V]` collapsing the three duplicated engram registries → `engram.ai.registry`; the `Resolver` port → `engram.ai.resolver`; `EmbeddingPort` + the pydantic-ai embedder → `engram.ai.embedding`; and the retrieval package `engram.ai.retrieval` (the strategy axis, `RetrievalPolicy`, the budget toolset, tool descriptions, the generic message/result contracts, the tool ports + toolset builder, and — per [engram ADR-0035](../../engram/docs/adr/0035-retrieval-sql-from-descriptors.md) — the `CorpusSchema`/`GraphSchema` descriptors and the SQL-builder ABCs). `core/` keeps only `db` / `files` / `markdown`.

**No wireup in the library surface.** The library ships the `Resolver` Protocol and plain classes; the wireup container stays the app's composition-root choice (a sibling app proves the no-wireup path). engram keeps its wireup `ContainerResolver` adapter local.

**What did NOT move, and why.** The `AskService` streaming orchestration stays in engram. It is the glass-box presentation *engine*, not a generic kernel: it is wired to engram's model catalog, the glass-box event wire contract, a per-tool gloss mapping engram tool names to representative `api.*` SQL, engram's `Strategy`, and engram's `DEFAULT_INSTRUCTIONS`. Moving it would leak engram's whole domain into the domain-general library, the exact anti-goal of the extraction, and there is no second consumer (YAGNI). The calibrated prompt text (`DEFAULT_INSTRUCTIONS`) likewise stays in engram: it is the `evals apply-policy` rewrite target, resolved by file path. This is a deliberate deviation from the plan's letter ("move `AskService` to the library"), recorded here as the boundary rule: **the reusable AI stack is generic control-flow, ports, and generated SQL; the presentation layer and the calibrated prose are the consumer's.**

**Ownership rule** mirrors `core/` and `client-core/`: a change in `engramai/` must keep the `core`, `engram` **and** sibling app suites green, plus `engramai`'s own suite. The library prose stays domain-general — no wiki/page/claim/paper vocabulary crosses the boundary.

## Options considered

- **A uv / poetry workspace.** Rejected — a root-level build graph in a polyglot monorepo re-opens exactly the per-app-independence cost ADR-0001 rejected for the uv workspace and ADR-0004 for npm workspaces.
- **Keep the stack in engram; let a second consumer depend on engram.** Rejected — drags the whole wiki domain (and its DB, its vault grammar) into an unrelated consumer.
- **Keep `core.ai` / `core.bridge` in `core`.** Rejected (owner decision): `engramai` is the single shared AI surface, so the AI factories belong with it; `core` stays the general, AI-free library.
- **Publish `engramai` to a private registry.** Rejected — versioning + publish ceremony for same-repo consumers, the same call ADR-0004 made.

## Consequences

**Good:** one home for the reusable AI stack; the extraction playbook and ownership rule are the proven `core`/`client-core` ones; engram is reduced to **one adapter** that supplies its corpus as descriptors ([engram ADR-0035](../../engram/docs/adr/0035-retrieval-sql-from-descriptors.md)) plus its calibrated prompts and demo engine; a second consumer can bring its own corpus without touching engram.

**Bad:** a third Python package to keep in sync (the uv path source must be re-installed when `engramai`'s own deps change, the same install-time gotcha ADR-0004 flags for `file:` deps); the domain-general prose bar must be policed at review time in the library; `core`'s now-unused `azure-identity` / `openai` / `pydantic-ai` deps are left in place this initiative (trimming them risks removing `pydantic`, which other `core` modules use transitively) and are a logged follow-up.

**Reversible?** Yes, additively. The path source could be inlined back into a consumer and nothing is published, so the split is a source-layout choice, not a distribution commitment.
