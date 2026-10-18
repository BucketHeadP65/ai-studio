"""The graph walks keep the set of nodes they reached, and `api.graph_reach` says how far a walk can go.

Revision ID: 0062
Revises: 0061
Create Date: 2026-10-11 00:00:00.000000

`kb.graph_expand_walk` and `kb.graph_paths_walk` listed every simple path out of their start, so their work grew with
the number of paths. On thirty pages that each link to every other, an expansion of six hops and a route search of
four did not finish in three seconds, and under Apache AGE an expansion of two hops did not either, because its match
always ran to six hops before the hop filter. Both walks now keep the set of nodes they reached and take one hop at a
time from the newest of them. The route search first walks back from its target to learn each node's hops to it, then
deepens a length bound one hop at a time and extends a route only to a node from which the target is still within the
bound. The rows they return do not change.

`api.graph_reach` is new. From a seed set, with the same relation filter and currency argument, it walks with no hop
bound and returns one row: how many nodes the seeds reach, the hops to the farthest of them and to an optional target,
and whether the walk took in every node it could reach. It stops at 10,000 nodes. A caller tells "no route exists"
from "no route within max_hops" with it, and an expansion from the nodes past its last hop. It is `SECURITY DEFINER`
with a pinned `search_path`, guarded by `api.visible_collection(p_collection)` like the walks, and granted to
`kb_reader`.

The comments on `api.graph_expand` and `api.graph_paths` now state their hop limits and name `api.graph_reach`.

The graph descriptor does not change, and every revision renders the walks from the library's current builder, so
downgrade drops `api.graph_reach` and its walk and puts the two comments back, leaving the walk bodies as the library
renders them. What this revision freezes is the descriptor, never the rendered SQL.
"""

from __future__ import annotations

from alembic import context, op
from core.ai.retrieval.contracts import AgeGraphTraversalBuilder, CteGraphTraversalBuilder, GraphSchema

revision: str = "0062"
down_revision: str | None = "0061"
branch_labels: str | tuple[str, ...] | None = None
depends_on: str | tuple[str, ...] | None = None

_READER_ROLE = "kb_reader"
_AGE_GRAPH_NAME = "kb_graph"

_EXPAND_WALK = "kb.graph_expand_walk"
_PATHS_WALK = "kb.graph_paths_walk"
_REACH_WALK = "kb.graph_reach_walk"

_EXPAND_WALK_SIGNATURE = "kb.graph_expand_walk(text, text[], integer, text[], integer, boolean)"
_PATHS_WALK_SIGNATURE = "kb.graph_paths_walk(text, text, text, integer, text[], integer, boolean)"
_REACH_WALK_SIGNATURE = "kb.graph_reach_walk(text, text[], text[], text, boolean)"
_REACH_SIGNATURE = "api.graph_reach(text, text[], text[], text, boolean)"

# The page currency the Apache AGE projection reads, byte-identical to 0048's.
_PAGE_CURRENCY_SOURCE = "(SELECT p.id, p.collection AS src_collection, p.superseded_by IS NOT NULL AS superseded FROM kb.pages p)"

# The cypher walk keeps every hop inside `p_collection`. The recursive-CTE backend's walk crosses collections at its
# destination endpoint, so it reads the both-endpoints-visible view instead of the edge table.
_WALK_EDGE_TABLE = {"age": "kb.edges", "cte": "kb.visible_edges"}

# The wrappers' volatility follows the walk they call.
_WALK_VOLATILITY = {"age": "VOLATILE", "cte": "STABLE"}

_REACH_WRAPPER = """
CREATE FUNCTION api.graph_reach(
  p_collection text,
  seed_ids text[],
  rel_types text[] DEFAULT NULL,
  target_id text DEFAULT NULL,
  include_superseded boolean DEFAULT false
)
  RETURNS TABLE(reached_nodes integer, deepest_hop integer, target_hop integer, complete boolean)
  LANGUAGE sql {volatility} SECURITY DEFINER SET search_path = pg_catalog, kb, public, pg_temp AS $$
    SELECT w.*
    FROM kb.graph_reach_walk(p_collection, seed_ids, rel_types, target_id, include_superseded) w
    WHERE api.visible_collection(p_collection)
$$
"""

_REACH_COMMENT = (
    f"COMMENT ON FUNCTION {_REACH_SIGNATURE} IS "
    "'How far a set of seed ids reaches in the link graph of p_collection, with no hop limit: one row of reached_nodes "
    "(the seeds included), deepest_hop (hops to the farthest node reached), target_hop (hops to target_id, NULL when "
    "it was not reached or none was given) and complete (true when the walk took in every node it could reach). "
    "rel_types and include_superseded filter the walk as they filter api.graph_expand. The walk stops at target_id "
    "once it reaches it and at 10,000 nodes, and complete is false then. Use it to tell that no route exists "
    "(target_hop NULL and complete true) from a route longer than graph_paths was asked for. Example: SELECT * FROM "
    "api.graph_reach(''<collection>'', ARRAY[''<page-id>''], target_id => ''<other-page-id>'');'"
)

_EXPAND_COMMENT = (
    "COMMENT ON FUNCTION api.graph_expand(text, text[], integer, text[], integer, boolean) IS "
    "'Typed-relation neighbourhood expansion, scoped to one collection: from one or more seed ids in p_collection, walk "
    "the link graph out to N hops (default 2, at most 6), optionally filtered to rel_types, returning one row per "
    "reached node with its id, type, title, status and superseded, plus each edge that first reached it (src_id, dst_id, "
    "rel, provenance_claim, NULL on a seed). Every node is reached once, at its fewest hops. k caps how many nodes come "
    "back (max 50). include_superseded defaults to false, which skips a superseded node at the hop that would reach "
    "it, so a node reachable only through one is absent too. Pass true to walk them. A seed is never judged, so "
    "expanding a superseded page still answers. api.graph_reach says whether nodes lie past the last hop. Example: "
    "SELECT * FROM api.graph_expand(''<collection>'', ARRAY[''<page-id>''], hops => 2);'"
)

_PATHS_COMMENT = (
    "COMMENT ON FUNCTION api.graph_paths(text, text, text, integer, text[], integer, boolean) IS "
    "'Shortest typed-relation routes between two nodes, scoped to one collection: the routes from from_id to to_id "
    "within p_collection of at most max_hops hops (default 4, at most 8), shortest first, optionally filtered to "
    "rel_types, returning one row per node along each route with its id, type, title, status and superseded, plus the "
    "edge into it (src_id, dst_id, rel, provenance_claim, NULL on the start node), the path_rank group and the path "
    "score. A route never visits a node twice. k caps the number of routes. include_superseded defaults to false, "
    "which drops any route running through a superseded node past the start. Pass true to keep them. No rows means no "
    "route within max_hops. api.graph_reach with target_id says whether a longer one exists. Example: SELECT * FROM "
    "api.graph_paths(''<collection>'', ''<page-id>'', ''<other-page-id>'');'"
)

# 0053's comments, byte-identical: what `downgrade` restores.
_PREVIOUS_EXPAND_COMMENT = (
    "COMMENT ON FUNCTION api.graph_expand(text, text[], integer, text[], integer, boolean) IS "
    "'Typed-relation neighbourhood expansion, scoped to one collection: from one or more seed ids in p_collection, walk "
    "the link graph out to N hops (default 2), optionally filtered to rel_types, returning one row per reached node "
    "— its id/type/title/status/superseded plus the edge that reached it (src_id, dst_id, rel, provenance_claim; "
    "NULL on a seed). k caps how many rows come back (max 50). include_superseded defaults to false, which skips a "
    "superseded node at the hop that would reach it, so a node reachable only through one is absent too; pass true to "
    "walk them. A seed is never judged, so expanding a superseded page still answers. Example: SELECT * FROM "
    "api.graph_expand(''<collection>'', ARRAY[''<page-id>''], hops => 2);'"
)

_PREVIOUS_PATHS_COMMENT = (
    "COMMENT ON FUNCTION api.graph_paths(text, text, text, integer, text[], integer, boolean) IS "
    "'Shortest typed-relation paths between two nodes, scoped to one collection: enumerate the paths from from_id to "
    "to_id within p_collection within max_hops (default 4), optionally filtered to rel_types, returning one row per "
    "node along each path — its id/type/title/status/superseded plus the edge into it (src_id, dst_id, rel, "
    "provenance_claim; NULL on the start node), the path_rank group and the path score. k caps the number of paths. "
    "include_superseded defaults to false, which drops any path running through a superseded node past the start; pass "
    "true to keep them. Example: SELECT * FROM api.graph_paths(''<collection>'', ''<page-id>'', ''<other-page-id>'');'"
)


def _graph_schema(edge_table: str) -> GraphSchema:
    """0048's currency-aware descriptor over `edge_table`, the one the walks are rendered from."""
    return GraphSchema(
        edge_table=edge_table,
        src_col="src_id",
        dst_col="dst_id",
        rel_col="rel",
        edge_payload_cols=("provenance_claim",),
        node_view="api.cards",
        node_cols=("id", "type", "title", "status", "superseded"),
        node_col_types=("text", "text", "text", "text", "boolean"),
        partition_col="src_collection",
        superseded_col="superseded",
        currency_source=_PAGE_CURRENCY_SOURCE,
    )


def _graph_backend_name() -> str:
    """The configured graph backend's name, `age` or `cte`.

    Returns:
        The backend name.

    Raises:
        RuntimeError: When `alembic/env.py` stashed no recognised `GraphBackendSql`.
    """
    backend_name = getattr(context.config.attributes.get("graph_backend"), "name", None)
    if backend_name in ("age", "cte"):
        return backend_name
    msg = f"graph_backend not configured (or unrecognised name {backend_name!r}): alembic/env.py must stash a GraphBackendSql"
    raise RuntimeError(msg)


def upgrade() -> None:
    """Republish the two walks over the set of reached nodes, and publish `api.graph_reach`."""
    backend_name = _graph_backend_name()
    builder = AgeGraphTraversalBuilder(_AGE_GRAPH_NAME) if backend_name == "age" else CteGraphTraversalBuilder()
    schema = _graph_schema(_WALK_EDGE_TABLE[backend_name])

    op.execute(f"DROP FUNCTION {_EXPAND_WALK_SIGNATURE}")
    op.execute(f"DROP FUNCTION {_PATHS_WALK_SIGNATURE}")
    op.execute(builder.expand_body(schema, function_name=_EXPAND_WALK))
    op.execute(builder.paths_body(schema, function_name=_PATHS_WALK))
    op.execute(builder.reach_body(schema, function_name=_REACH_WALK))
    op.execute(_REACH_WRAPPER.format(volatility=_WALK_VOLATILITY[backend_name]))
    op.execute(f"GRANT EXECUTE ON FUNCTION {_REACH_SIGNATURE} TO {_READER_ROLE}")
    op.execute(_REACH_COMMENT)
    op.execute(_EXPAND_COMMENT)
    op.execute(_PATHS_COMMENT)


def downgrade() -> None:
    """Drop `api.graph_reach` and its walk, and put 0053's comments back."""
    op.execute(f"DROP FUNCTION {_REACH_SIGNATURE}")
    op.execute(f"DROP FUNCTION {_REACH_WALK_SIGNATURE}")
    op.execute(_PREVIOUS_EXPAND_COMMENT)
    op.execute(_PREVIOUS_PATHS_COMMENT)
