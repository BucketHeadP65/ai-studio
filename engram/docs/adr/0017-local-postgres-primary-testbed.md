---
id: engram-adr-0017-local-postgres-primary-testbed
aliases: ["engram-adr-0017-local-postgres-primary-testbed"]
type: adr
app: engram
status: accepted
tags: []
title: "ADR-0017: The local Postgres as the primary local/CI test bed (amends ADR-0006)"
---

# ADR-0017: The local Postgres as the primary local/CI test bed (amends ADR-0006)

**Status:** Accepted (2026-06-24). Amends [ADR-0006](0006-dual-dialect-bm25-seam.md), which made
ParadeDB the local/CI default while the managed Postgres target became a peer selected by config. This flips
**which dialect is the local default**: the local Postgres bed becomes the primary local/CI bed
(`dialect=pg_textsearch`, `graph_backend=age`), and ParadeDB stays a first-class opt-in. The dual-dialect seam
itself and both dialect implementations are unchanged.

**Context.** ADR-0006 chose ParadeDB as the local/CI default for a concrete reason: there was **no local
`pg_textsearch` bed**, so defaulting to that dialect would have made every test depend on a remote database. Two
things have changed since:

- A local **Postgres bed** now exists (`docker/postgres/`): `postgres:17` built with `pg_textsearch`, Apache AGE,
  pgvector and ltree, with `shared_preload_libraries='age,pg_textsearch'` baked into the image. It runs the
  `pg_textsearch` dialect and the AGE graph backend with the same **grammar** as the managed Postgres target,
  fully offline.
- The managed target ships `pg_textsearch` and AGE, and not ParadeDB's `pg_search` (ADR-0006's own framing). So a
  local bed defaulting to ParadeDB means local/CI exercises the `pg_search` BM25 grammar and the recursive-CTE
  graph walk, which is **not what the managed target runs**. Before this decision the `pg_textsearch` + `age`
  path ran only in a gated profile against an external database URL. A tier of in-process AGE migration checks
  (`test_migrations.py::TestGraphSurface[age]`) skipped for lack of a local AGE bed.

The default should exercise the grammar the managed target speaks. With the local bed available, that is now
possible offline.

**Decision.** Make the local Postgres the **primary local/CI test bed**, with ParadeDB retained as an opt-in:

- **Flip the defaults.** `RetrievalSettings.dialect` defaults to `DbDialect.PG_TEXTSEARCH` and `graph_backend`
  to `GraphBackend.AGE`. `alembic/env.py`'s fallbacks flip the same way (`pg_textsearch` / `age`). A bare run with
  no config now targets the `pg_textsearch` grammar, which the local bed and the managed target both speak.
- **conftest defaults to the local bed.** The integration tier runs against the prebuilt
  `engram-postgres:latest` image. The migrations create the extensions themselves (`0001` does `vector` /
  `ltree` and the BM25 extension through the `SearchDialect`, `0004` does `CREATE EXTENSION age` under the AGE
  backend), and the image's baked `shared_preload_libraries` makes those succeed, so no initdb mount is needed.
  This **unlocks the in-process AGE tests** (`test_migrations.py::TestGraphSurface[age]`) that previously
  skipped for lack of a local AGE bed, and they pass, so AGE migrates and traverses end to end on the local bed.
  The gated profiles that read an external database URL (`WIKI_PG_TEXTSEARCH_URL`, `WIKI_LOCAL_POSTGRES_URL`)
  stay for running the same checks against another database.
- **ParadeDB stays a first-class opt-in.** `KNOWLEDGE_BASE_TEST_DIALECT=pg_search` (test harness) and
  `APP_RETRIEVAL__DIALECT=pg_search` + `graph_backend=cte` (runtime) select the ParadeDB path. The `pg_search`
  golden-SQL unit tests stay, so the dialect keeps coverage. `docker compose up` brings up the local Postgres by
  default, and ParadeDB moves behind a `paradedb` profile.

**Consequences.**

- (+) **Local/CI exercises the managed target's grammar by default.** The `pg_textsearch` BM25 surface and the
  AGE multi-hop graph run on every local/CI run, and the in-process AGE migration tests that used to skip now
  execute, so grammar regressions surface offline. This pivot already caught one: an unqualified
  `CREATE EXTENSION vector` in migration `0001` that lands `vector` in the `wiki` schema (not `public`) under a
  non-superuser role, which ParadeDB's pre-seeded image masked and the local bed exposed.
- (+) **No dialect loses all coverage.** ParadeDB stays opt-in (the dual-dialect seam's safety net), and its
  golden-SQL unit tests are unchanged, so the `pg_search` grammar is still pinned.
- (-) **The local bed is not a managed service.** It is `postgres:17` plus the same extensions, so it matches
  the grammar and AGE multi-hop but **not** a managed host's role model or AGE session semantics (for example a
  per-session `LOAD 'age'` that the baked `shared_preload_libraries` papers over locally). The local bed can pass
  where a managed host would differ, so the gated profile against an external database (ADR-0006's
  `WIKI_PG_TEXTSEARCH_URL`) complements it rather than being replaced by it.
- (-) **First build is slow.** The image compiles three extensions from source. It is cached after the first
  build (prebuilt locally as `engram-postgres:latest`), and CI must cache the image layer or accept a one-time
  build cost.
- **Reader-role fidelity is preserved.** `wiki_reader` is created by migration `0003` (not the image) and
  conftest's `reader_url` fixture connects as it on either bed, so `test_guardrails.py` still exercises read-only
  enforcement on the local bed.
- **Reversible?** Yes. Flip the two defaults back and restore conftest's ParadeDB bed. The dialect seam, both
  dialect implementations and the migrations are untouched by this ADR.

**Related.** Amends [ADR-0006](0006-dual-dialect-bm25-seam.md) (the dual-dialect seam. Its "ParadeDB stays the
local/CI default" decision is the one this supersedes). [ADR-0007](0007-age-multihop-dual-graph-backend.md) (the
AGE multi-hop backend now exercised locally by default),
[ADR-0008](0008-configuration-via-application-settings-json.md) (the `APP_RETRIEVAL__DIALECT` /
`APP_RETRIEVAL__GRAPH_BACKEND` config this flips the default of). Concepts primer:
[retrieval-concepts.md](../retrieval-concepts.md).
