---
id: engram-adr-0043-age-graph-projection-collection-tag
aliases: ["engram-adr-0043-age-graph-projection-collection-tag"]
type: adr
app: engram
status: accepted
tags: []
title: "0043 — The Apache AGE graph projection carries the collection tag"
---

# 0043 — The Apache AGE graph projection carries the collection tag

**Status:** Accepted (2026-08-23)

**Context.** [ADR-0042](0042-corpus-qualified-identity.md) made every corpus-owned table's key `(collection, id)` and taught the graph
traversal builder a `partition_col` so a walk scopes to one corpus. The recursive-CTE backend partitions at every hop, its join filters
the collection column at each step of the walk. Apache AGE was made fail-closed instead:
`core.ai.retrieval.graph_builder.AgeGraphTraversalBuilder` raised `ValueError` when `partition_col` was set, because its projected property
graph carried no per-collection tag at all — a partitioned cypher walk would route through another corpus's nodes and edges, leaking their
topology. A database configured for the Apache AGE backend, which is what the managed Postgres target runs, could not apply migration `0025` at all, exactly
the follow-up ADR-0042's Consequences named as required, not optional.

A second, independent defect sat underneath the first: the projection has deduplicated vertices by bare `id` and rebuilt from the whole
`memory.edges` table on every compile since migration `0004`, regardless of partitioning. Two corpora's same-`id` pages have always
collapsed into one AGE vertex — the graph has silently merged every collection's topology together since the projection first shipped.

A spike against a live AGE 1.6.0 instance settled how isolation can even be expressed there. A naive endpoint-only filter
(`WHERE a.collection = X AND b.collection = X` over `(a)-[*1..N]->(b)`) leaks: it returns a path routing through a foreign interior node,
because it never inspects the nodes between the two endpoints. The natural openCypher idiom for that,
`ALL(n IN nodes(p) WHERE n.collection = X)`, is a hard parse error on AGE 1.6.0 — `ALL`/`ANY`/`NONE`/`SINGLE` predicate forms are
unsupported, and list-comprehension property access is broken too. Unwinding `nodes(p)` and aggregating a count of foreign-collection
nodes, filtered to zero, is the AGE-safe equivalent, and the spike confirmed it is result-equivalent to the CTE's per-hop pruning on the
trap topology `a(A) → x(B) → b(A)`: it excludes the path leaking through `x`'s other-collection copy while keeping the clean in-collection
path.

**Options considered.**

- **(b) A graph per collection.** Rejected: the strongest isolation, but the heaviest — N graphs, a dynamic graph name threaded through
  every cypher call, runtime `create_graph`/teardown as collections come and go. Nothing in the codebase manages graph lifecycle as data
  arrives; this would add a new schema-bootstrap responsibility with no precedent.
- **(c) A label per collection** (`Node_<collection>`/`REL_<collection>`). Rejected: real per-hop isolation via the label boundary itself,
  but it requires per-collection DDL (`create_vlabel`/`create_elabel`) created as collections appear — the same new schema-bootstrap
  responsibility as (b), for no correctness gain over (a).
- **(a) A `collection` property on every vertex and edge, composite `(id, collection)` vertex identity, and the `UNWIND`-aggregate per-hop
  filter.** Chosen: the same isolation guarantee as the CTE backend, achieved entirely with data (properties), no new AGE catalog DDL.

**Decision.**

1. **Composite vertex identity.** The projection keys every vertex by `(id, collection)`, not bare `id`: the vertex-dedup `UNION` and the
   edge-endpoint join both key on the pair. Every vertex's property map gains `collection`; every edge's property map gains its own
   `collection` (its source side's), fixing the pre-existing cross-collection merge regardless of whether the schema is partitioned.
2. **Per-hop isolation via `UNWIND nodes(p)`, never an endpoint-only or `ALL(...)` check.** When `GraphSchema.partition_col` is set,
   `expand_body`/`paths_body` insert `UNWIND nodes(p) AS pn WITH p, count(CASE WHEN pn.collection <> $p_collection THEN 1 END) AS
   foreign_ct WHERE foreign_ct = 0` between the cypher `WHERE` and the `RETURN`, ahead of any hop or relation-type filtering. The
   fail-closed guard (`_AGE_PARTITION_UNSUPPORTED`, both `raise ValueError` call sites) is removed; the un-partitioned bodies stay
   byte-identical, golden-pinned.
3. **Migration `0026` lands the fork on disk.** It regenerates `api.graph_expand`/`api.graph_paths` from the same builder and schema
   `0025` already used (a content no-op for a database that reaches it, since the builder itself is what changed), and — for the first
   time since `0004` — regenerates `memory.refresh_graph()` from a partitioned schema, then runs it once inside the migration
   (`SELECT memory.refresh_graph()`) to rebuild every existing AGE vertex and edge with the tag and the composite identity. A failure
   there aborts the whole migration rather than leaving a half-tagged graph; the recursive-CTE backend's `refresh_graph()` stays an
   unconditional no-op, so the same call is safe under either backend without a branch.

**Consequences.**

- (+) **Corpus-qualified identity reaches the Apache AGE backend.** A database configured for `graph_backend = age` can reach
  `alembic upgrade head` again. The AGE-backed managed target ADR-0042 named as blocked is unblocked.
- (+) **The pre-existing all-collections vertex merge is fixed**, independent of partitioning — two collections sharing a bare id no
  longer collapse into one AGE vertex.
- (−) **The AGE traversal is match-then-filter, not prune-during-expansion.** It expands a path, then discards it if any node along the
  way is foreign, rather than the CTE's join excluding a foreign edge before it is ever walked. Correct and result-equivalent (per the
  spike), but potentially heavier on a large graph; the walk stays capped by `hops`/`k`. A follow-up lever, if this bites, is a
  collection-scoped refresh (today's is a wholesale rebuild on every compile) or a tighter cypher form.
- (−) **`memory.refresh_graph()`'s rebuild stays wholesale**, not collection-scoped: every compile re-projects every collection's
  vertices and edges, even though only one collection's edges changed.
- **Reversible?** Yes. The fork is internal to `core.ai.retrieval.graph_builder` plus one forward migration; the un-partitioned bodies and
  the fixed `api.graph_expand`/`api.graph_paths` signatures are unchanged, and `0026`'s downgrade restores `memory.refresh_graph()`'s
  pre-fork, untagged body.

**Related.** [ADR-0042](0042-corpus-qualified-identity.md) (the corpus-qualified identity this fork completes),
[ADR-0007](0007-age-multihop-dual-graph-backend.md) (the dual-graph-backend seam this lands on),
[ADR-0017](0017-local-postgres-primary-testbed.md) (the local Postgres bed this now reaches head on). Code:
`core/src/core/ai/retrieval/graph_builder.py` (`AgeGraphTraversalBuilder`), `engram/alembic/versions/0026_age_graph_projection_fork.py`,
`engram/tests/integration/test_graph_collection_isolation.py` (the cross-corpus trap replay).
