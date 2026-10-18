"""AGE graph projection fork: the collection tag and composite identity reach the api.graph_* functions.

Revision ID: 0026
Revises: 0025
Create Date: 2026-08-23 00:00:00.000000

`core.ai.retrieval.graph_builder.AgeGraphTraversalBuilder` now projects every vertex keyed by
`(id, collection)` instead of bare `id`, carries a `collection` property onto every vertex and
edge, and — when `GraphSchema.partition_col` is set — filters every hop of a cypher walk to the
caller's own collection via an `UNWIND nodes(p)` aggregate rather than an endpoint-only check (the
only form Apache AGE's openCypher dialect accepts; `ALL`/`ANY` predicates and list-comprehension
property access both hard-error there). 0025 already regenerated `api.graph_expand`/
`api.graph_paths` from this builder with a partitioned `GraphSchema`, so this revision's DROP+CREATE
of both functions (same builder, same schema, the same DROP+CREATE idiom 0025 uses) reproduces the
one it already installed — this revision's actual change is `memory.refresh_graph()`, which 0025
never touched: its body still projects bare-`id` vertices with no `collection` property, so the
newly-partitioned `api.graph_*` functions would silently return nothing against whatever it last
built. Recreating `refresh_graph()` from the same partitioned schema, then running it once here,
rebuilds every existing AGE vertex and edge with the tag and the composite identity in one pass;
the recursive-CTE backend's `refresh_graph()` is an unconditional no-op regardless of partitioning,
so running it needs no backend guard — it is a safe call under either backend by construction.
"""

from __future__ import annotations

from alembic import context, op
from core.ai.retrieval.contracts import AgeGraphTraversalBuilder, CteGraphTraversalBuilder, GraphSchema

revision: str = "0026"
down_revision: str | None = "0025"
branch_labels: str | tuple[str, ...] | None = None
depends_on: str | tuple[str, ...] | None = None

_READER_ROLE = "corpus_reader"

# Byte-identical to 0025's post-upgrade signatures — this revision recreates the same functions,
# not new ones.
_EXPAND_SIGNATURE = "api.graph_expand(text, text[], integer, text[], integer)"
_PATHS_SIGNATURE = "api.graph_paths(text, text, text, integer, text[], integer)"
_REFRESH_SIGNATURE = "memory.refresh_graph()"

# Byte-identical to 0025's `_NEW_EXPAND_COMMENT`/`_NEW_PATHS_COMMENT` — the caller-facing contract
# is unchanged, only the AGE backend's underlying body differs.
_EXPAND_COMMENT = (
    "COMMENT ON FUNCTION api.graph_expand(text, text[], int, text[], int) IS "
    "'Typed-relation neighbourhood expansion, scoped to one collection: from one or more seed ids in "
    "p_collection, walk the link graph out to N hops (default 2), optionally filtered to rel_types, "
    "returning one row per reached node — its id/type/title/status/superseded plus the edge that "
    "reached it (src_id, dst_id, rel, provenance_claim; NULL on a seed). k caps how many rows come "
    "back (max 50). Example: SELECT * FROM api.graph_expand(''research'', ARRAY[''ent.graphrag''], hops => 2);'"
)
_PATHS_COMMENT = (
    "COMMENT ON FUNCTION api.graph_paths(text, text, text, int, text[], int) IS "
    "'Shortest typed-relation paths between two nodes, scoped to one collection: enumerate the paths "
    "from from_id to to_id within p_collection within max_hops (default 4), optionally filtered to "
    "rel_types, returning one row per node along each path — its id/type/title/status/superseded plus "
    "the edge into it (src_id, dst_id, rel, provenance_claim; NULL on the start node), the path_rank "
    "group and the path score. k caps the number of paths. Example: "
    "SELECT * FROM api.graph_paths(''research'', ''ent.graphrag'', ''ent.lightrag'');'"
)

_REFRESH_COMMENT = (
    "COMMENT ON FUNCTION memory.refresh_graph() IS "
    "'Rebuild the projected link graph from memory.edges, keyed by (id, collection) and carrying "
    "each vertex and edge''s own collection so two collections sharing a bare id never merge into "
    "one vertex (a no-op under the recursive-CTE backend, which walks the edge relation live and "
    "needs no projected graph). Writer-only — run by the compile load step after the edge set is "
    "replaced; the read-only api.graph_expand/api.graph_paths functions only traverse the "
    "projection it produces.'"
)

# The pre-0026 refresh COMMENT (byte-identical to 0010's `_refresh_comment(schema_name=\"memory\")`),
# restored on downgrade alongside the bare-id, untagged projection body.
_OLD_REFRESH_COMMENT = (
    "COMMENT ON FUNCTION memory.refresh_graph() IS "
    "'Rebuild the projected link graph from memory.edges (a no-op under the recursive-CTE "
    "backend, which walks the edge relation live). Writer-only — run by the compile load step "
    "after the edge set is replaced; the read-only api.graph_expand/api.graph_paths functions "
    "only traverse the projection it produces.'"
)


def _graph_schema(*, partitioned: bool) -> GraphSchema:
    """The link-graph shape `api.graph_expand`/`api.graph_paths`/`memory.refresh_graph()` build from.

    Byte-identical to 0025's `_graph_schema` (migration-local, since a hand-authored migration body
    cannot import another revision file).
    """
    return GraphSchema(
        edge_table="memory.edges",
        src_col="src_id",
        dst_col="dst_id",
        rel_col="rel",
        edge_payload_cols=("provenance_claim",),
        node_view="api.cards",
        node_cols=("id", "type", "title", "status", "superseded"),
        node_col_types=("text", "text", "text", "text", "boolean"),
        partition_col="src_collection" if partitioned else None,
    )


def _builder() -> AgeGraphTraversalBuilder | CteGraphTraversalBuilder:
    """Return the traversal builder for the target database's configured backend (mirrors 0025's own lookup)."""
    backend = context.config.attributes.get("graph_backend")
    backend_name = getattr(backend, "name", None)
    if backend_name == "age":
        return AgeGraphTraversalBuilder("wiki_kg")
    if backend_name == "cte":
        return CteGraphTraversalBuilder()
    msg = f"graph_backend not configured (or unrecognised name {backend_name!r}): alembic/env.py must stash a GraphBackendSql"
    raise RuntimeError(msg)


def upgrade() -> None:
    """Recreate `api.graph_*` + `memory.refresh_graph()` from the partitioned schema, then re-project once.

    The re-projection (`SELECT memory.refresh_graph()`) is not wrapped in a guard: it runs inside
    this migration's transaction, so any failure aborts the whole upgrade rather than leaving a
    half-tagged graph behind.
    """
    builder = _builder()
    schema = _graph_schema(partitioned=True)

    op.execute(f"DROP FUNCTION {_EXPAND_SIGNATURE}")
    op.execute(f"DROP FUNCTION {_PATHS_SIGNATURE}")
    op.execute(f"DROP FUNCTION {_REFRESH_SIGNATURE}")

    op.execute(builder.expand_body(schema))
    op.execute(builder.paths_body(schema))
    op.execute(builder.refresh_body(schema))

    op.execute(f"GRANT EXECUTE ON FUNCTION {_EXPAND_SIGNATURE} TO {_READER_ROLE}")
    op.execute(f"GRANT EXECUTE ON FUNCTION {_PATHS_SIGNATURE} TO {_READER_ROLE}")
    op.execute(_EXPAND_COMMENT)
    op.execute(_PATHS_COMMENT)
    op.execute(_REFRESH_COMMENT)

    op.execute("SELECT memory.refresh_graph()")


def downgrade() -> None:
    """Restore `memory.refresh_graph()`'s pre-0026 bare-id body, then re-run it once to match.

    `api.graph_expand`/`api.graph_paths` are recreated from the same partitioned schema they already
    had at 0025 (this revision never changed their contract, only reproved it builds), so downgrading
    them is a content no-op done here purely to pair with the DROP this revision's upgrade issued.
    """
    builder = _builder()
    partitioned_schema = _graph_schema(partitioned=True)
    unpartitioned_schema = _graph_schema(partitioned=False)

    op.execute(f"DROP FUNCTION {_EXPAND_SIGNATURE}")
    op.execute(f"DROP FUNCTION {_PATHS_SIGNATURE}")
    op.execute(f"DROP FUNCTION {_REFRESH_SIGNATURE}")

    op.execute(builder.expand_body(partitioned_schema))
    op.execute(builder.paths_body(partitioned_schema))
    op.execute(builder.refresh_body(unpartitioned_schema))

    op.execute(f"GRANT EXECUTE ON FUNCTION {_EXPAND_SIGNATURE} TO {_READER_ROLE}")
    op.execute(f"GRANT EXECUTE ON FUNCTION {_PATHS_SIGNATURE} TO {_READER_ROLE}")
    op.execute(_EXPAND_COMMENT)
    op.execute(_PATHS_COMMENT)
    op.execute(_OLD_REFRESH_COMMENT)

    op.execute("SELECT memory.refresh_graph()")
