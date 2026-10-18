---
id: engram-adr-0006-dual-dialect-bm25-seam
aliases: ["engram-adr-0006-dual-dialect-bm25-seam"]
type: adr
app: engram
status: accepted
tags: []
title: "ADR-0006: Dual-dialect BM25 seam (ParadeDB local, a managed Postgres target)"
---

# ADR-0006: Dual-dialect BM25 seam (ParadeDB local, a managed Postgres target)

**Status:** Accepted (2026-06-16). Implements Slice A of the managed Postgres plan. Slices B (DiskANN) and
C (Apache AGE) are deferred.

> **Amended by [ADR-0017](0017-local-postgres-primary-testbed.md) (2026-06-24):** the "ParadeDB
> stays the local/CI default" choice below is superseded. The local Postgres bed is now the
> primary local/CI test bed (`dialect=pg_textsearch`, `graph_backend=age`), with ParadeDB retained as an
> opt-in. The dual-dialect seam itself, the substance of this ADR, is unchanged.

**Context.** The wiki serves read-only hybrid retrieval, lexical BM25 (Best Matching 25, the standard
keyword-relevance score) fused with semantic vector search, out of one Postgres engine. Today that engine is
ParadeDB (`paradedb/paradedb:latest-pg17`) running in local Docker only. It has no managed home (no HA, backups,
point-in-time restore or hosted endpoint), which is the open "Revisit when" on the ParadeDB choice in
[root ADR 0001](../../../docs/adr/0001-wiki-app-placement-and-core.md). A managed Postgres that ships the
retrieval extensions as Postgres extensions (in-database vector, full-text and graph) fits this workload. But
such a host does **not** ship ParadeDB's `pg_search`. Its BM25 lives in a different extension, `pg_textsearch`,
with a different SQL surface:

| Concern | ParadeDB `pg_search` | `pg_textsearch` |
|---|---|---|
| Match + score | `@@@ paradedb.parse(q)` + `paradedb.score(id)` | `<@> to_bm25query(q,'idx')` (op returns score) |
| Index DDL | `WITH (key_field='id')` | `WITH (text_config='english')` |

Only the BM25 grammar differs. The vector legs (`halfvec(1024)` + HNSW, cosine `<=>`), Reciprocal Rank Fusion
(RRF, fuse ranked lists by `Σ 1/(60+rank)`), the `api.search` call signature, and all application code are
identical across both engines, so the risk of supporting both is contained to one SQL surface.

**Options considered.**

- **One-way cutover to the managed target.** Rejected: there is no local `pg_textsearch` image yet, and a
  cutover would make every test depend on a remote database and abandon the fast offline Docker loop.
- **Stay ParadeDB-only, unhosted.** Rejected: leaves the wiki with no managed database, the original gap.
- **Managed Postgres + an external search index.** Rejected: re-introduces the two-system reconcile that
  single-engine hybrid was chosen to avoid (root ADR 0001).
- **Dual-dialect seam (chosen).** One codebase emits the right BM25 SQL per backend, so ParadeDB stays the
  local/CI default and the managed target becomes a peer selected by config.

**Decision.**

- Introduce a `SearchDialect` Protocol covering only what varies (the BM25 index DDL, the `CREATE EXTENSION`
  line, and the `api.search` body) with two implementations: `ParadeDBDialect` (today's SQL lifted verbatim, the
  safety net) and `PgTextSearchDialect` (the `pg_textsearch` rewrite). They live in a `SearchDialects`
  frozen-dataclass registry keyed by a `DbDialect` StrEnum (`pg_search` / `pg_textsearch`), built by one
  `build_search_dialects()` factory. This is the project's Strategy Registry pattern (mirrors
  `CalendarAdapters`).
- A new `WIKI_DB_DIALECT` setting (default `pg_search`) selects the backend. `alembic/env.py` resolves it and
  routes the extension, the four BM25 index DDLs (`0001_wiki_schema.py`), and the `api.search` body
  (`0002_api_surface.py`) through the selected dialect.
- Keep `halfvec(1024)` + HNSW and the `<=>` vector legs unchanged. Keep `api.search` a `SECURITY DEFINER`
  function and swap only its body per dialect. The wiki DB is disposable (vault-derived), so "migrating" a
  backend is `alembic upgrade head` + `engram compile`, never a `pg_dump`.

**Consequences.**

- (+) One codebase serves both engines. ParadeDB keeps the offline dev/CI loop while the managed target gains a
  supported path. Nothing local changes by default, and the two backends can run side by side for
  retrieval-parity A/B testing.
- (+) Adding a third backend is a one-entry registry diff, not a rewrite.
- (-) Two BM25 grammars now exist behind the Protocol and must be kept in sync. Golden-SQL unit tests pin both
  grammars (and their mutual exclusion) to catch drift.
- (-) The `PgTextSearchDialect` body is validated statically and by golden tests until it runs against a
  `pg_textsearch` database. The gated integration profile (`WIKI_PG_TEXTSEARCH_URL`) and a top-k parity check
  are how it is confirmed.
- **Reversible?** Yes. ParadeDB is the untouched default, and the seam is additive (delete
  `PgTextSearchDialect` + the setting to revert).

**Related.**
[root ADR 0001](../../../docs/adr/0001-wiki-app-placement-and-core.md) (chose ParadeDB for single-engine
hybrid. This keeps it local and adds a managed peer).
[ADR 0002](0002-disposable-mirror-zotero-source-of-truth.md) (the disposable-DB principle that makes a backend
swap a recompile, not a data migration). Concepts primer: [retrieval-concepts.md](../retrieval-concepts.md).
