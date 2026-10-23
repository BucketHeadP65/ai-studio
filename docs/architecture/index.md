<!-- GENERATED from architecture.md by scripts/docs/split_doc.py --dir docs/architecture — do not edit here; edit the master and re-run the script. -->

# Architecture doc — section index

**Master:** [architecture.md](architecture.md) — the single source of
truth. Edit it, then run `python3 scripts/docs/split_doc.py --dir docs/architecture` to regenerate `sections/` and this index.

**Purpose of this index:** when a question touches specific points of
the design, load ONLY the matching `sections/` chunk(s) below instead
of the whole document. Match against the *Load when* keywords; token
counts say what each load costs.

## Orientation (from the master)

This is the arc42-lite architecture master for the monorepo: system context and
containers (C4 L1/L2), the cross-cutting concepts every app obeys, and one
building-block section per app. It is **split-doc'd**: edit this master only,
then run `python3 scripts/docs/split_doc.py --dir docs/architecture`. Readers
and agents should start from [index.md](index.md) and load only the sections
whose *load-when* keywords match their question. Contracts per feature slice
live in the apps' `docs/reference/` docs. Decision rationale lives in the ADRs
and [explanation docs](../explanation/tech-stack.md). The entry point to
everything is [CONTEXT-MAP.md](../../CONTEXT-MAP.md).

Sections: 7 · whole document ≈ 6,607 tokens.

### 01 — System context
[sections/01-system-context.md](sections/01-system-context.md) · ~444 tokens
- **TL;DR:** One person's local toolchain, a domain-general markdown knowledge base indexer (an Obsidian-style knowledge base folder, to Postgres, to hybrid retrieval for agents), its screen, and workflows that run against that knowledge base behind gates, assembled as one product, AI Factory Studio, over in-repo libraries.
- **Load when:** system context, what is this repo, external systems, integrations, personas, C4 L1

### 02 — Containers & data flow
[sections/02-containers-data-flow.md](sections/02-containers-data-flow.md) · ~1,266 tokens
- **TL;DR:** Two Python apps (studio on :8100, harness on :8770) + one TS app (studio-client) over three libraries (engram, core, engram-connectors), with `templates/` as data beside them. Two Postgres databases of the same shape on one server, the knowledge base DB (the disposable local Postgres on :5434 by default, ParadeDB on :5433 opt-in) and the workflow library `engram_workflows`, migrated by one alembic chain.
- **Load when:** containers, deployment, databases, local Postgres, ParadeDB, ports, data flow, C4 L2, connectors, templates

### 03 — Cross-cutting concepts
[sections/03-cross-cutting-concepts.md](sections/03-cross-cutting-concepts.md) · ~3,883 tokens
- **TL;DR:** Vertical slices in the engram library. CQRS bus from core.cqrs, used by the knowledge base half only. Five records about one workflow run (graph, presentation, intent, checked outcome, graded usefulness and owner acceptance), each answering one question. A governed outcome benchmark refuses before it measures, and names its own model paths by reading them. zod-parse every TS boundary. Both backends local-only with no auth. Path-source (not workspace) dependency strategy. Two composition styles inside studio (wireup DI in the knowledge base half, plain composition in the agent half). One alembic chain, in engram, for both databases.
- **Load when:** conventions, patterns, adding a new app, adding a slice, dependency strategy, DI, migrations, testing discipline, presentation, intent, checked outcome, usefulness, owner acceptance, artifact action, run arguments, outcome benchmark, case custody, metered model path, blocked report

### 04 — engram building blocks
[sections/04-engram-building-blocks.md](sections/04-engram-building-blocks.md) · ~309 tokens
- **TL;DR:** A knowledge base to Postgres indexer and read-only retrieval library, organized into five bounded contexts (Ingestion, Knowledge, Retrieval, Evaluation, Lifecycle & interchange). Its structure has graduated into engram's own arc42/C4 master, linked below.
- **Load when:** engram, knowledge base, memory, compile, indexing, serve, sync, retrieval, knowledge base pipeline, note grammar, mcp, Studio backend, bounded contexts

### 05 — Frontends
[sections/05-frontends.md](sections/05-frontends.md) · ~222 tokens
- **TL;DR:** One Next.js 16/React 19 SPA, studio-client (:3031), local and no-auth, branded "AI Factory Studio", with its app-agnostic code in `src/shared/`: a typed zod fetch factory, UI primitives, and a Vitest/MSW test harness.
- **Load when:** frontend, studio-client, src/shared, AI Factory Studio, the Studio, dashboard, react, next.js, ui, SPA

### 06 — Quality, risks, tech debt
[sections/06-quality-risks-tech-debt.md](sections/06-quality-risks-tech-debt.md) · ~400 tokens
- **TL;DR:** Clean codebase (zero inline debt markers, debt is ledgered in prose). The live risks are engram's hand-written SQL api surface, the sync lazy-route invariant, Docker-gated coverage, the workflow runner's shell-out posture, and ADR-0009's recorded manifest/contract follow-ups.
- **Load when:** risks, tech debt, known issues, caveats, coverage, drift

### 07 — Glossary pointers
[sections/07-glossary-pointers.md](sections/07-glossary-pointers.md) · ~83 tokens
- **TL;DR:** Domain terms live in per-app CONTEXT.md files, indexed from CONTEXT-MAP.md, not here.
- **Load when:** glossary, terminology, domain language, what does X mean
