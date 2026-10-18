---
id: adr-0001-wiki-app-placement
aliases: ["adr-0001-wiki-app-placement"]
type: adr
app: root
status: accepted
tags: []
title: "0001 — Wiki app placement and the `core` shared package"
---

# 0001 — Wiki app placement and the `core` shared package

- **Status:** accepted
- **Date:** 2026-06-11
- **Deciders:** repository maintainers (wiki-rag feature)
- **Supersedes / Amends:** none
- **Related:** `docs/adr/0002` (the wiki app's internal CQRS + `wikinote` kernel architecture)

## Context

This feature adds the **production serving layer of an LLM research wiki** (the app now called `engram/`): a vault→Postgres compiler plus MCP/CLI retrieval. We have to decide *where that code lives* before any of it is written.

Two questions are entangled and have a single structural answer:

1. **Where does the wiki serving layer live**: is it a slice inside a sibling app, a part of the Layer-1 Zotero-to-vault sync tool, or its own thing?
2. **What is the shared substrate** that more than one app in the monorepo needs — talking to Azure OpenAI, building a database engine, and reading/writing Markdown are all common. How is that commonality factored out without coupling the apps' release/runtime concerns?

Constraints in play:

- Each app's runtime is documented a specific way: an **in-project virtualenv** (e.g. `engram/.venv`, via `poetry.toml` `virtualenvs.in-project = true`). Anything that relocates that venv or changes how `uv sync` resolves dependencies breaks that documented setup.
- The wiki's corpus is a **single global corpus** (the Knowledge vault's `research` collection) — a self-contained data-ownership model that does not belong inside another app's slices.
- The wiki needs **ParadeDB Postgres extensions** (`pg_search` for BM25, `vector`/`halfvec`, `ltree`) that a managed Postgres does not provide; it runs against its own disposable database (docker compose, host port 5433).
- The wiki is **batch-compile + read-only-serving**, not a request/response web monolith — a distinct lifecycle.
- The wiki's database is **disposable and fully rebuilt** from the vault (vault ADR D-15); it must not share another app's alembic migration chain, or a wiki rebuild would entangle with that schema history.

Task 2.1 of this feature has already landed the core extraction: the `core/` package was extracted (`feat(core)`, commit `cf3cedb`). This ADR records the *why* behind that move; the wiki app itself does not exist yet (it is built by later slices B–E), so this document is written in target-state terms.

## Decision

We make **one structural decision with two facets.**

### (a) The wiki serving layer is an independent app `engram/`

The wiki is its **own top-level project in this monorepo**, not a slice inside a sibling app and not part of the sync tool. Rationale:

- The **corpus is global** and self-contained — a data-ownership model that does not belong inside another app's slices.
- It needs **ParadeDB extensions that a managed Postgres lacks** (`pg_search`/BM25, `halfvec`, `ltree`); it runs against its own database.
- It is a **batch CLI plus read-only serving surface**, which is not the same shape as a request/response monolith.
- Its **separate, disposable database must not share another app's alembic chain** — a full wiki rebuild cannot be allowed to entangle with another schema's migration history.
- The sync tool's own design **already treated the compiler as "the next project"**, so the serving layer was always intended to be a distinct artifact rather than an extension of the sync tool. The sync tool remains the Layer-1 Zotero-to-vault sync. The new app deliberately mirrors its patterns so a later absorption stays cheap, but it is not built inside it now.

### (b) `core/` is a concrete first-class package `core`, consumed via uv path sources — not a uv workspace

`core/` is a **concrete, first-class Python package** — project name `core`, import name `core`, with a hatchling build and its own tests and ruff/pyright config. **The apps depend on it** through **uv path sources**, each declaring in its own `pyproject.toml`:

```toml
[tool.uv.sources]
core = { path = "../core", editable = true }
```

It is deliberately **not a uv workspace.** Rationale:

- A uv **workspace would relocate the shared `.venv` to the repository root**, breaking each app's documented in-project virtualenv (e.g. `engram/.venv`) and any tooling that expects to resolve dependencies from the app's own rootDir.
- **Path sources keep each app's lockfile and venv independent.** Each app `uv sync`s its own environment and resolves `core` from the sibling directory; there is no shared root environment to coordinate or break.

**`core` v1 contents inventory** — all the general-purpose, app-agnostic code that exists today:

- `db.py` — async engine / session factories plus a `create_tables(engine, metadata)` parametrized over the caller's metadata (it no longer hard-codes any one app's `Base`).
- `ai.py` — **Azure OpenAI pydantic-ai model factories** (chat + embedding + a test shortcut): a plain `AsyncOpenAI` against the Azure `/openai/v1/`-compatible endpoint, an Entra-ID async bearer-token provider (no keys), and a `TestModel` shortcut. No `os.environ` reads in the library — endpoints/deployments are explicit parameters; each app's settings own the env names.
- `files.py` — generic file utilities (atomic write, safe slug, path-traversal guard).
- `markdown.py` — frontmatter ⇄ dict split/parse/render helpers.

**The ownership rule:** *general-purpose, app-agnostic code lives in `core`; an app's domain stays in the app.* In particular, **each app's `DeclarativeBase`/metadata stays app-owned** — the engine/session *machinery* is shared, but the schema definitions are not. The apps have **separate databases and separate alembic chains, deliberately NOT shared.** `core` gives them a common way to build an engine and create tables against *their own* metadata; it does not give them a common schema.

This was a **direct user instruction**: "make the core a concrete package where the apps depend on it and move there general-purpose reusable code".

## Consequences

**Positive**

- The repo gains a **clean top-level split** — `engram/` (vault→Postgres compiler + MCP/CLI retrieval, being built by later slices) and the shared `core/` library (`core`) — with clean, explicit boundaries.
- Each app keeps its **own lockfile, venv, and deploy story**. The extraction was surgical — only imports re-point onto `core`; the engine/session, AI factory, and file/markdown utilities move with no behaviour change.
- Shared infrastructure has **one home and one set of tests**; the Azure OpenAI factory pattern and file/markdown utilities are no longer duplicated or trapped inside one app.

**Negative / costs**

- **`core/` is a shared dependency: a change there can affect every consumer.** When touching `core/`, the core and each consumer's test suites all need to run.
- Path sources mean each app **resolves `core` from the filesystem**; the dependency is editable and local, not published — fine for a monorepo, but it is a build-time coupling to the directory layout (`../core`). Moving or renaming `core/` requires updating every `[tool.uv.sources]` entry.
- Separate projects mean **separate `uv sync` environments** to maintain rather than one — accepted as the price of independent lockfiles and deploys.
- Deliberately **not sharing `DeclarativeBase`/metadata** means each app re-declares its own `Base`; this is intentional (separate DBs, separate alembic chains) and must not be "DRY-refactored" into a shared schema later.

**Follow-ups**

- The wiki app's *internal* architecture (CQRS message bus + Unit of Work, and the `wikinote` shared-kernel note grammar) is recorded separately in `docs/adr/0002`.
