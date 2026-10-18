---
id: engram-adr-0007-age-multihop-dual-graph-backend
aliases: ["engram-adr-0007-age-multihop-dual-graph-backend"]
type: adr
app: engram
status: accepted
tags: []
title: "ADR-0007 — AGE multihop seam: dual graph backend (recursive-CTE + Apache AGE)"
---

# ADR-0007 — AGE multihop seam: dual graph backend (recursive-CTE + Apache AGE)

**Status:** Accepted (2026-06-17). Covers compile, the AGE projection and multi-hop traversal as the
read-only `wiki_reader`, through MCP. Implements the multihop-reasoning capability of
the full-mode test plan. Amends vault decision
[D-13](../../../knowledge/_meta/design/decisions.md) ("the graph is a derived
table; recursive CTEs serve multi-hop; a graph database was rejected") — see
the matching vault entry **D-23**.

> **Amended by [ADR-0049](0049-graph-walks-keep-the-set-of-reached-nodes.md) (2026-10-11):** neither backend
> lists paths any more. Both walk over the set of reached nodes, one hop at a time, from one generated plpgsql body in
> which only the one-hop read differs. The dual backend itself, the substance of this ADR, is unchanged.

**Context.** The wiki's knowledge graph is a *derived* artifact: `wiki.edges`
(`src_id, rel, dst_id, qualifiers jsonb, provenance_claim`) is materialized at
compile time from the vault's registry-validated typed links, with claim-level
provenance. Today retrieval exposes only a **flat, single-hop** view of it —
`api.edges` and the `api.graph` corpus-graph (`GraphHandler`, `api.cards ⋈
api.edges`). The corpus itself (a research wiki *about* graph/multihop RAG)
shows that **path-shaped, multi-hop reasoning** is the high-value capability on
a *dense, curated* graph like this one: associative k-hop expansion is the
cleanest isolated graph win (HippoRAG: +5 recall@5 over dense retrieval,
encoder-agnostic; up to +38 on entity-centric multi-hop), and ranked relational
paths buy robustness (S-Path-RAG: 78% F1 retained without the gold seed, 92%
under entity-linking noise; PathRAG: fewer tokens, higher win-rate). Recursive
CTEs over `wiki.edges` *can* express multi-hop, but variable-length path
traversal and path ranking are painful and slow to write/maintain in SQL.

D-13 rejected a graph database on two grounds: "unproven payoff" and
"rebuildable-from-Markdown stays absolute." Apache AGE changes the calculus on
both: it is an **in-Postgres** extension (openCypher over the same engine, no
external graph store), and an AGE graph built *from* `wiki.edges` is fully
rebuildable — `wiki.edges` remains the single source of truth. The open
question D-13 named ("unproven payoff") is precisely what we now want to
*measure*, not assume.

**Options considered.**

- **Recursive CTEs only (status quo, D-13).** Rejected as the sole option:
  expressing ranked variable-length paths in recursive SQL is awkward and the
  "unproven payoff" of a graph engine stays untested forever.
- **Apache AGE only, AGE graph as primary.** Rejected: making the AGE graph the
  source of truth breaks D-13's rebuildable-from-Markdown invariant and discards
  the working CTE path with no comparison.
- **External graph DB (Neo4j etc.).** Rejected for the original D-13 reasons —
  a second engine breaks single-engine rebuildability and the disposable-DB
  principle (ADR-0002).
- **Dual backend behind one port (chosen).** Both recursive-CTE and AGE
  implementations satisfy a common `GraphBackend` port, selected by config — so
  the *same* multihop API runs both ways and a golden-dataset eval can decide
  which wins, on which query shapes, with what cost.

**Decision.**

- Introduce a `GraphBackend` Protocol covering what varies between traversal
  engines — the `api.graph_expand` / `api.graph_paths` function bodies and the
  (AGE-only) graph-projection DDL — with two implementations: `CteGraphBackend`
  (recursive CTEs over `wiki.edges`, the D-13 baseline) and `AgeGraphBackend`
  (`CREATE EXTENSION age`, a `wiki_kg` graph projected from `wiki.edges`,
  openCypher bodies). They live in a `GraphBackends` frozen-dataclass registry
  keyed by a `GraphBackend` StrEnum (`cte` / `age`), built by one
  `build_graph_backends()` factory — the project's Strategy Registry pattern
  (mirrors `SearchDialects`, ADR-0006).
- A `retrieval.graph_backend` setting in `application_settings.json` (env
  override `APP_RETRIEVAL__GRAPH_BACKEND`; default `cte`, so the D-13 path stays
  the default) selects the backend — the single config system of ADR-0008, *not*
  a `WIKI_*` env var. A new migration `0004_graph.py` routes the AGE extension +
  projection and the two `api.*` graph functions through the selected backend
  (mirroring how `0001`/`0002` route the BM25 dialect).
- Two capabilities, both returning the subgraph + claim provenance so an agent
  can L4-verify: **Expand** (`api.graph_expand(seed_ids, hops, rel_types, k)` —
  k-hop traversal along an optional relation-type filter, which subsumes
  lineage/dispute/supersession chains) and **Paths**
  (`api.graph_paths(from_id, to_id, max_hops, rel_types, k)` — ranked relational
  paths between two entities). Both are `SECURITY DEFINER`, `EXECUTE`-granted to
  `wiki_reader`.
- The AGE graph is a **projection rebuilt at compile time** from `wiki.edges`
  (nodes = the distinct edge endpoints; edges = the typed relations carrying
  `rel` + `provenance_claim`). Projection is a **writer-only**
  `wiki.refresh_graph()` that the compile *load step* runs (as the writing role)
  right after it replaces `wiki.edges`; the `api.graph_expand` /
  `api.graph_paths` functions are **pure-read** and only *traverse* whatever the
  last projection produced. This write/read split is forced by the serving role:
  `wiki_reader` is `default_transaction_read_only=on` (ADR-0003) and
  `SECURITY DEFINER` does **not** lift transaction read-only, so a query-time
  re-projection (an `INSERT`) fails on the read path. The CTE backend reads
  `wiki.edges` live and needs no projection, so it emits a no-op
  `wiki.refresh_graph()` and compile calls it backend-agnostically. `wiki.edges`
  stays the source of truth; the graph is a disposable derivative (ADR-0002).
- Surface: a dedicated `api.*` function + MCP tools (`graph_expand`,
  `find_paths`) and a new retrieval-policy archetype — **opt-in, routed**, not
  fused into `api.search`. The evidence says graph traversal *hurts* on
  low-integration/factoid queries, so it must be a deliberate choice, and
  `api.search`'s hybrid-RRF body (ADR-0006) stays untouched.

**Consequences.**

- (+) The same multihop API runs on CTE and AGE, so the "graph DB payoff"
  question D-13 left open becomes an empirical comparison on golden datasets,
  not a guess. ParadeDB, the managed Postgres target and the local Postgres bed
  all gain multihop without engine-specific call sites.
- (+) D-13's invariants survive: Markdown-canonical, `wiki.edges`-derived,
  rebuildable. AGE is additive and default-off.
- (−) Two traversal grammars now exist behind the port and must be kept in sync;
  golden-SQL unit tests pin both (and their result-shape equivalence) to catch
  drift, exactly as the dialect seam does.
- (−) The AGE backend's openCypher bodies are validated statically + by golden
  tests. A managed Postgres host imposes four constraints the golden tests cannot catch,
  and the local bed's superuser/read-write posture masks three of them: (1) AGE projection
  DDL cannot share the migration's single transaction (run it in an
  `op.get_context().autocommit_block()`); (2) `drop_graph` must be
  `ag_catalog`-qualified + existence-guarded; (3) `LOAD 'age'` is superuser-only
  on a managed host and redundant given
  `shared_preload_libraries=age`, so it is removed; (4) query-time projection
  violates the read-only reader — hence the write/read split above. Enabling AGE
  on a managed host also requires `age` in **both** the host's extension allow-list and
  `shared_preload_libraries` (a server parameter change and a restart).
- **Reversible?** Yes. `cte` is the untouched default; the seam is additive
  (delete `AgeGraphBackend` + the setting + the AGE projection to revert).

**Related.**
[ADR-0006](0006-dual-dialect-bm25-seam.md) (the BM25 dialect seam this mirrors);
[ADR-0002](0002-disposable-mirror-zotero-source-of-truth.md) (the disposable-DB
principle that makes the AGE projection a recompile, not a migration); vault
decisions **D-13** (amended) and **D-16** (retrieval routing is doctrine — the
new multihop archetype is a policy edit, not a hardcoded router).
