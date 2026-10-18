---
id: engram-adr-0049-graph-walks-keep-the-set-of-reached-nodes
aliases: ["engram-adr-0049-graph-walks-keep-the-set-of-reached-nodes"]
type: adr
app: engram
status: accepted
tags: []
title: "0049: Graph walks keep the set of reached nodes, and a reach walk says how far they can go"
---

# 0049: Graph walks keep the set of reached nodes, and a reach walk says how far they can go

**Status:** Accepted (2026-10-11). Amends [ADR-0007](0007-age-multihop-dual-graph-backend.md): both backends stay, and
neither walks paths any more.

**Context.** `api.graph_expand` and `api.graph_paths` listed every simple path out of their start. The recursive-SQL
backend recursed with `UNION ALL` and a `path` array, and the Apache AGE backend matched `[*1..6]` and `[*1..8]` and applied
the hop limit afterwards. Their work grew with the number of paths, not nodes. On thirty pages that each link to every
other, a six-hop expansion and a four-hop route search did not answer within three seconds, and under AGE a two-hop
expansion did not either. The hop limits of 6 and 8 applied silently, so "no route" read the same as "stopped at 8 hops".

**Decision.**

1. Every walk keeps the set of nodes it has reached and takes one hop at a time from the newest of them, so a node is
   reached once, at its fewest hops. `core.ai.retrieval.graph_builder.BaseGraphTraversalBuilder` generates one plpgsql
   body for each walk. A backend supplies only the one-hop read: the edge relation for the recursive-SQL backend, a
   single-hop `MATCH (a:Node)-[r:REL]->(b:Node)` for AGE. Edge validity (relation, source collection, a current
   destination) belongs to the edge, so one predicate serves every walk, and the seed is never judged.
2. An expansion stops at its hop bound, when nothing new is reached, or once it has reached more nodes than `k` selects,
   since a later hop could never be selected.
3. A route search walks back from its end over the same kind of set to learn each node's hops to the end. It then deepens
   a length bound one hop at a time from the hops between the two nodes up to `max_hops`, extends a route only to a node it
   has not visited from which the end is still within the bound, and stops once it has found more routes than `k` selects,
   since a longer route ranks after every shorter one.
4. `api.graph_reach(p_collection, seed_ids, rel_types, target_id, include_superseded)` walks with no hop bound, stops at its
   target or at `GRAPH_REACH_NODE_LIMIT` (10,000) nodes, and returns `reached_nodes`, `deepest_hop`, `target_hop` and
   `complete`. It is `SECURITY DEFINER`, guarded by `api.visible_collection` and granted to `kb_reader` (migration 0062).
5. Graph results carry it as `reach`. An expansion that was not cut reads it and says whether nodes lie past its last hop.
   A route search that found nothing reads it and says that no route exists, how long the shortest route is, or that the
   start reaches more than 10,000 nodes without the end among them. The facts ride on the result because every serving
   path rebuilds the next-move block.
6. Migration 0062 republishes both walks under their signatures. Their rows are byte-identical to the path-listing walks on
   both backends, compared across 2,304 calls.

**Consequences.**

- (+) A walk's work grows with the nodes and edges it reaches. A six-hop expansion on the local knowledge base fell from
  84 ms to 13 ms, and the AGE parity run from 209 s to 5 s.
- (+) The agent can tell "no route exists" from "no route within `max_hops`" and knows when a neighbourhood goes further.
- (+) The two backends differ in one statement, so a reviewer can diff their functions line by line.
- (−) A route search still lists every route at the length where it stops. The distance check is necessary, not
  sufficient, so routes cannot be pruned to the `k` smallest per level. A graph with very many routes of that length is
  bounded only by the statement timeout.
- (−) AGE now runs one cypher match per hop instead of one per call.
- **Reversible?** Partly. 0062's downgrade drops `api.graph_reach`, but every revision renders the walks from the
  library's current builder, so the walk bodies stay in their new form.

**Related.** [ADR-0007](0007-age-multihop-dual-graph-backend.md) (the dual backend this amends),
[ADR-0043](0043-age-graph-projection-collection-tag.md) (the collection tag every hop checks under AGE),
[ADR-0048](0048-search-legs-are-bounded-tuned-and-read-from-one-snapshot.md) (the snapshot both reads of a call share). Code:
`core/src/core/ai/retrieval/graph_builder.py`, `engram/alembic/versions/0062_graph_reached_set_walks.py`,
`engram/src/engram/retrieval/queries.py` (`GraphExpandHandler`, `GraphPathsHandler`, `graph_next_steps`,
`paths_next_steps`), `core/src/core/ai/retrieval/domain.py` (`GraphReach`).
