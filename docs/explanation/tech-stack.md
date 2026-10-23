---
id: tech-stack
aliases: [tech-stack]
type: explanation
app: root
status: current
tags: [stack]
title: "Tech stack, what was chosen and why"
---

# Tech stack, what was chosen and why

One block per load-bearing choice: what, why (with the source where the
rationale is recorded), alternatives considered, trade-offs, and the condition
that should reopen the choice. New dependencies added by features get their
block here via the Phase-4 doc-impact procedure.

**Provenance of the "why".** Three markers keep recovered rationale honest:

- *(no marker)*, the rationale is recorded in an ADR or `CLAUDE.md`, cited
  inline.
- **inferred, confirm**, no record exists. The "why" is the documenter's
  best reconstruction from the code and the stack's shape. Plausible, not
  authoritative. Treat as a question to the architect, not a settled fact.

Where an inferred block carries a genuine reservation, it is stated in
Trade-offs / Revisit-when rather than smoothed over.

## uv path sources, not a uv workspace (Python)

- **What:** `core/` is consumed by every Python app as
  `core = { path = "../core", editable = true }`. Each app keeps its own
  lockfile and in-project `.venv`.
- **Why:** [ADR 0001](../adr/0001-wiki-app-placement-and-core.md): a uv
  workspace would relocate the shared `.venv` to the repo root, breaking each
  app's documented in-project virtualenv.
- **Alternatives:** uv workspace (rejected, above). Publishing `core` to a
  registry (ceremony for same-repo consumers).
- **Trade-offs:** `core/` changes require running each consumer's test suite
  manually, no enforced cross-app gate.
- **Revisit when:** a CI matrix exists that can gate all consumers.

## Shared frontend code inside the SPA (TypeScript)

- **What:** the app-agnostic frontend code (the typed fetch and zod boundary,
  the UI primitives, the utilities and the test support) lives in
  `studio-client/src/shared/`, which imports nothing from the rest of the app.
  `src/shared/boundary.test.ts` fails on an import that crosses that line.
- **Why:** [ADR 0021](../adr/0021-frontend-shared-code-lives-in-the-spa.md).
  The separate package had one consumer, and its copy install cost a refresh
  after every change, a Tailwind `@source` into `node_modules`, duplicated
  dependencies and a split test setup.
- **Trade-offs:** a second frontend would have to extract the folder into a
  package again.
- **Revisit when:** a second frontend needs the same code.

## `core.ai`, the shared AI stack (formerly `engramai`)

- **What:** the reusable AI stack (model/embedding factories, the CQRS kernel,
  the observability facade, a generic `Registry`, the `Resolver` port, embedding,
  and the whole `retrieval` package, strategy/policy/budget/contracts/toolset
  plus the `CorpusSchema`/`GraphSchema` descriptors and the SQL-builder ABCs)
  lives in `core` as `core.cqrs`, `core.observability`, `core.resolver`, and
  `core.ai` (including `core.ai.retrieval`). Consumed by
  engram as the one uv path source (`core = { path = "../core", editable = true }`).
  It previously lived in a sibling package `engramai` (import namespace
  `engram.ai`, poetry-core). That project has been retired and its modules
  folded into `core` by concern.
- **Why:** [root ADR 0006](../adr/0006-engramai-shared-ai-library.md) records
  the original extraction rationale (the retrieval + agent stack is meant to be
  reusable across projects with extend/reuse/override, so engram becomes one
  adapter and a second consumer can bring its own knowledge base), still true of
  `core` post-consolidation. Only the two-distribution shape ended.
- **Alternatives:** keep the stack in engram (a second consumer would drag in the
  whole knowledge base domain). A uv/poetry workspace (re-opens the per-app-independence cost
  ADR 0001/0004 rejected). Publish to a registry (versioning ceremony for same-repo
  consumers).
- **Trade-offs:** `core`'s dependency set now covers the absorbed AI stack
  (azure-identity, logfire, openai, pydantic-ai) for every consumer, even one
  that only needs `core.db`/`core.files`.
- **Revisit when:** a consumer needs `core` without the AI stack (motivates an
  extras split, e.g. `core[ai]`).

## ParadeDB (BM25 + vector + ltree)

- **What:** the engram DB runs `paradedb/paradedb:latest-pg17` (host port 5433):
  `pg_search` BM25, `halfvec(1024)` HNSW cosine, `ltree` section paths.
- **Why:** [ADR 0001](../adr/0001-wiki-app-placement-and-core.md): engram
  "needs ParadeDB extensions that a managed Postgres lacks", hybrid
  SQL+BM25+vector retrieval in one engine, per the knowledge base's retrieval policy.
- **Alternatives:** managed Postgres + external search index (two systems to
  reconcile). Dedicated vector DB (loses SQL+BM25 in one query surface).
- **Trade-offs:** local Docker dependency. Integration/e2e tests need
  testcontainers and auto-skip without Docker.
- **Revisit when:** engram needs to be hosted (no managed ParadeDB on the
  current platform) or the knowledge base outgrows one node, now partly acted on. See
  the managed Postgres block below.

## Managed Postgres target and the dual-dialect BM25 seam

- **What:** engram can index and serve on either ParadeDB (`pg_search`, the
  opt-in local bed) or `pg_textsearch` (the local default and a managed Postgres that ships it),
  chosen by `WIKI_DB_DIALECT`. A `SearchDialect` Protocol + `SearchDialects`
  registry emit the backend-specific BM25 SQL. The vector legs (`halfvec` cosine
  `<=>`, ranked by exact distance within a budget), RRF, and the `api.search` signature
  are identical on both.
- **Why:** [engram ADR 0006](../../engram/docs/adr/0006-dual-dialect-bm25-seam.md):
  a managed Postgres that ships the retrieval extensions answers the ParadeDB block's
  "needs to be hosted" revisit condition with in-database hybrid + graph search, while
  the seam keeps the offline ParadeDB dev/CI loop.
- **Alternatives:** one-way cutover to the managed target (rejected, no local
  image at the time, every test would need a remote database). Managed Postgres + external
  search index (rejected, re-splits hybrid into two systems).
- **Trade-offs:** two BM25 grammars to keep in sync (golden-SQL tests pin both).
  Beyond the local bed, the `pg_textsearch` body runs against another database
  only through the gated `WIKI_PG_TEXTSEARCH_URL` integration profile.
- **Revisit when:** the optional DiskANN (Slice B) and Apache AGE graph (Slice C) profiles
  for the managed target are taken up.

## One alembic chain

- **What:** the repo has one migration chain, engram's at `engram/alembic`. It
  migrates both databases by URL: `knowledge_bases` (schema `kb`) and the
  workflow library `engram_workflows`. Its migrations create the extensions, the
  SQL `api` surface and the `kb_reader` role.
- **Why:** [ADR 0009](../adr/0009-ai-factory-studio-consolidation.md) replaced
  the per-app chains of [ADR 0001](../adr/0001-wiki-app-placement-and-core.md)
  with this one chain.
- **Trade-offs:** engram's `api` surface is hand-written SQL with no
  type-checker coverage against the ORM.
- **Revisit when:** the apps ever share a database (they shouldn't).

## pydantic-ai (agents)

- **What:** all LLM access is pydantic-ai over Azure OpenAI through the
  `core.ai` model factories (root
  [ADR 0006](../adr/0006-engramai-shared-ai-library.md)), Entra ID bearer, no API keys.
- **Why:** partially recorded, [ADR 0001](../adr/0001-wiki-app-placement-and-core.md)
  describes the `core.ai` shape. Typed agent outputs match the repo's pydantic-everywhere
  discipline. No standalone framework-comparison ADR.
- **Trade-offs:** framework lock-in at the agent layer. Mitigated by
  protocol-port seams over the agent. Now pinned `<2` in
  `engram/pyproject.toml` because the custom `Model` API is version-sensitive
  across majors.
- **Revisit when:** an agent needs capabilities pydantic-ai resists (the seam
  is the port, not the framework).

## pydantic-evals (retrieval evals)

- **What:** Datasets/Evaluators for the evals slice
  (`engram/src/features/evals/`). Golden QA datasets scored by
  `RetrievalMetrics` (recall@k/precision@k/MRR + tool-usage). Also backs the
  structural eval generation and the propose-only retrieval tuning loop,
  the **Vibe RAG** program ([engram ADR
  0016](../../engram/docs/adr/0016-structural-eval-generation.md) /
  [0018](../../engram/docs/adr/0018-calibration-loop.md)).
- **Why:** chosen so retrieval quality is measured with the same pydantic-first
  discipline as the rest of the stack. Co-released with `pydantic-ai` (same
  version line, resolves 1.107.0) so the eval layer stays in lockstep with the
  agent layer. Recorded in the evals plan +
  [engram ADR 0011](../../engram/docs/adr/0011-single-source-retrieval-toolset.md)
  (single-source toolset gives "evaluate the agent == evaluate the MCP surface").
- **Alternatives:** hand-rolled metric harness (re-implements the
  dataset/case/evaluator plumbing). A non-pydantic eval framework (breaks the
  pydantic-everywhere discipline + the shared Logfire/span integration).
- **Trade-offs:** version-coupled to pydantic-ai (custom `Model`/`Evaluator`
  APIs differ across majors), both pinned `<2` in `engram/pyproject.toml`,
  exact version in `uv.lock`.
- **Revisit when:** pydantic-ai/pydantic-evals 2.x lands, re-verify the
  `Model`/`Evaluator` signatures before bumping.

## Pydantic Logfire (eval instrumentation + RCA)

- **What:** the deep observability store for evals, instruments the
  agent-under-test (tool calls, retrieved results, token counts, latency),
  stores pushed datasets, and drives root-cause analysis via the Query API +
  Logfire MCP. engram Postgres keeps only a thin `evaluation_runs` summary.
- **Why:** pydantic-native (pairs with pydantic-ai's `Instrumentation()`
  capability), gives traces/datasets/experiments/RCA with no self-hosted OTel
  stack. Recorded in the evals plan (decision "Logfire = deep store, thin
  Postgres summary"). Tokens live in the gitignored
  `config/application_settings.json` (`infrastructure.logfire.*`).
- **Alternatives:** self-hosted OTel + trace store (more infra for a
  single-user local tool). No instrumentation (no feedback loop, which is the point of
  the feature).
- **Trade-offs:** Logfire Cloud dependency (EU region). Runs degrade to local
  span-recording when no token is set, but then no trace is persisted.
- **Revisit when:** a self-hosted / pure-OTel mode is needed.

## zod at the boundary

- **What/Why/Trade-offs:** see
  [design-patterns.md section Zod at the boundary](design-patterns.md#zod-at-the-boundary).
  rationale recorded in root `CLAUDE.md` section API response validation.

## FastAPI, **rationale inferred, confirm**

- **What:** the Python HTTP surfaces (`studio serve-http`, the harness)
  are FastAPI. Class-based routers. wireup's FastAPI integration.
- **Why (inferred, confirm):** the whole stack is pydantic-first (settings,
  domain models, agent I/O), and FastAPI makes pydantic the request/response
  contract and serves it as OpenAPI at `/openapi.json`. The `studio-client`
  checks each response with its own zod schemas instead of generating types
  from it. Async-native, matching the async SQLAlchemy/httpx stack.
- **Alternatives (inferred):** Litestar (closest peer, also pydantic/async,
  smaller ecosystem). Flask/Django (sync-first, no native OpenAPI).
- **Trade-offs:** FastAPI's DI (`Depends`) is bypassed in favour of wireup, so
  there are two DI idioms in the request path to understand.
- **Revisit when:** rationale is confirmed/corrected by the architect.

## SQLAlchemy 2 async, **rationale inferred, confirm**

- **What:** `sqlalchemy[asyncio]` in the Python apps. Async engine/session
  factories live in `core.db`.
- **Why (inferred, confirm):** one ORM idiom shared across the apps via
  `core.db`. The 2.x typed API fits the no-`Any` discipline. Async keeps a
  single concurrency model end-to-end (async FastAPI + asyncpg) so
  request handlers never block the loop on I/O.
- **Alternatives (inferred):** sync SQLAlchemy + threadpool. An async-first
  micro-ORM (SQLModel/Tortoise), rejected implicitly by the shared-kernel
  `core.db` and the alembic investment.
- **Trade-offs / honest flag:** async adds async-context complexity to every
  test and fixture. It pays off on the asyncpg path to engram's ParadeDB.
  Consistency across the apps is the real driver, not per-request throughput,
  worth saying plainly.
- **Revisit when:** confirmed by the architect.

## wireup DI, **rationale inferred, confirm**

- **What:** container-based DI in both Python apps (composition roots,
  per-slice `deps.py` providers, FastAPI integration, CLI `ctx.obj` seam).
  The *usage discipline* is well recorded (root `CLAUDE.md`, container
  docstrings). The *why wireup over alternatives* is not.
- **Why (inferred, confirm):** the repo's testing stance (DI seams + fakes
  over monkeypatching) needs every dependency to arrive through a swappable
  seam. wireup gives a typed, decorator-driven container with first-class
  FastAPI integration and module discovery, so slices register providers
  locally in `deps.py` without a god-module. It is the mechanism the testing
  discipline assumes.
- **Alternatives (inferred):** hand-wired factory functions (no library, but
  the composition root grows unwieldy across many slices).
  `dependency-injector` (heavier, declarative-container style). FastAPI's own
  `Depends` (request-scoped only, doesn't serve the CLI).
- **Trade-offs / honest flag:** wireup is a comparatively **niche** library,
  smaller community, thinner docs, real bus-factor/maintenance risk versus the
  mainstream options. The mitigation is that DI is confined to the composition
  roots and `deps.py` providers behind protocol ports, so a future swap would
  be localised, but it is a dependency I'd want the architect to have chosen
  deliberately, not by inertia.
- **Revisit when:** wireup goes unmaintained, or another Python app makes the
  container patterns worth re-examining.

## Next.js 16 (App Router) + React 19, **rationale inferred, confirm**

- **What:** both SPAs are Next.js 16 App Router + React 19, TypeScript strict,
  Turbopack dev.
- **Why (inferred, confirm):** one frontend framework for both clients.
  App Router + RSC is the current-generation default. React 19 for the modern
  hooks/transitions surface the dashboards use (optimistic updates, etc.).
- **Trade-offs / honest flag:** this is the **bleeding edge**, Next 16 +
  React 19 are very new, and the repo has *already* paid for it: the
  Turbopack symlink OOM that forced the shared frontend package's copy install
  (root ADR 0004). Early-adopter tax is a recurring cost here, not a
  hypothetical. Justified if staying current matters to the owner. Worth a
  conscious "yes" rather than a default.
- **Revisit when:** another framework-version upgrade costs material debugging
  time, or the App Router/RSC model fights the SPA-style data flow.

## TanStack Query, **rationale inferred, confirm**

- **What:** server-state caching/invalidation in both SPAs. Query-key
  discipline + hydration-safe `QueryClient` boundary documented in `CLAUDE.md`.
- **Why (inferred, confirm):** the clients are read-heavy over a REST API
  (search, jobs, status). TanStack Query handles caching, background
  refetch, and optimistic mutation without a hand-rolled cache. No objection,
  standard, well-matched choice.
- **Revisit when:** confirmed by the architect.

## Tailwind v4, **rationale inferred, confirm**

- **What:** Tailwind CSS v4 via `@tailwindcss/postcss`. UI primitives in
  `studio-client/src/shared/ui/`.
- **Why (inferred, confirm):** utility-first styling keeps shared primitives
  framework-light and copyable into both clients without a component-library
  dependency. Mild newness flag (v4's engine is recent) but low blast radius.
- **Revisit when:** confirmed by the architect.

## Biome, **rationale inferred, confirm**

- **What:** Biome for lint + format across the TS apps (`check`/`lint`
  scripts).
- **Why (inferred, confirm):** one fast Rust tool replacing the
  ESLint + Prettier pair, fewer configs, faster CI, no plugin-version drift.
- **Trade-offs:** smaller rule ecosystem than ESLint. Some lint rules have no
  Biome equivalent. Low cost for a solo repo where speed and simplicity win.
- **Revisit when:** a needed lint rule has no Biome equivalent.

## Vitest + MSW + Playwright, **rationale partially recorded**

- **What:** unit/component tests in Vitest with MSW network interception
  (`onUnhandledFrame: "error"`). e2e in Playwright. Shared test lifecycle in
  `studio-client/src/shared/test-support/`.
- **Why:** the MSW discipline *is* recorded, root `CLAUDE.md` section "No shared MSW
  handlers (the empty default handler set is load-bearing: every test states
  the network behaviour it depends on). The tool choices themselves are not.
- **Revisit when:** a test tier needs real-browser network behaviour MSW can't
  model.
