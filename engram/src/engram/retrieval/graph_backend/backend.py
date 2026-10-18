"""The `GraphBackendSql` port, the backend-specific link-graph SQL contract.

A backend satisfies it by shape and needs no inheritance. `EXPAND_COLUMNS` and `PATHS_COLUMNS` are
the `RETURNS TABLE` column orders every backend's function bodies must project.
"""

from __future__ import annotations

from typing import Literal, Protocol, runtime_checkable

# The ordered `api.graph_expand` result columns. One row per reached node: the node columns plus the
# edge that reached it. A seed row carries NULL edge columns.
EXPAND_COLUMNS: tuple[str, ...] = ("id", "type", "title", "status", "superseded", "src_id", "dst_id", "rel", "provenance_claim")

# The ordered `api.graph_paths` result columns: the expand columns plus the `path_rank` group and the
# path `score`, repeated on every row of a rank. A start-of-path row carries NULL edge columns.
PATHS_COLUMNS: tuple[str, ...] = (*EXPAND_COLUMNS, "path_rank", "score")

# The functions' M-Schema COMMENTs, shared by both backends. Each is one command, separate from the
# CREATE FUNCTION. The migration adds the GRANT.
EXPAND_COMMENT = (
    "COMMENT ON FUNCTION api.graph_expand(text[], int, text[], int) IS "
    "'Typed-relation neighbourhood expansion: from one or more seed ids, walk the link graph out to N hops "
    "(default 2), optionally filtered to rel_types, returning one row per reached node — its id/type/title/status/"
    "superseded plus the edge that reached it (src_id, dst_id, rel, provenance_claim; NULL on a seed). k caps how "
    "many rows come back (max 50). Example: SELECT * FROM api.graph_expand(ARRAY[''<page-id>''], hops => 2);'"
)
PATHS_COMMENT = (
    "COMMENT ON FUNCTION api.graph_paths(text, text, int, text[], int) IS "
    "'Shortest typed-relation paths between two nodes: enumerate the paths from from_id to to_id within max_hops "
    "(default 4), optionally filtered to rel_types, returning one row per node along each path — its id/type/title/"
    "status/superseded plus the edge into it (src_id, dst_id, rel, provenance_claim; NULL on the start node), the "
    "path_rank group and the path score. k caps the number of paths. Example: SELECT * FROM api.graph_paths(''<page-id>'', ''<other-page-id>'');'"
)


@runtime_checkable
class GraphBackendSql(Protocol):
    """Emits backend-specific link-graph traversal SQL for the `api.graph_*` functions.

    Every backend emits the same function signatures, the `EXPAND_COLUMNS` and `PATHS_COLUMNS`
    result columns, the `SECURITY DEFINER` posture and the `k` cap. Only the traversal differs.

    Attributes:
        name: Which backend this emits for, `"cte"` (recursive CTE) or `"age"` (Apache AGE).
    """

    name: Literal["cte", "age"]

    def create_graph_ddl(self) -> list[str]:
        """Emit the one-time graph-projection DDL this backend needs before the functions.

        Each element is one SQL command, run with its own `op.execute`.

        Returns:
            The projection commands for this backend (one per element), or `[]`
            when no projection is needed.
        """
        ...

    def expand_statements(self) -> list[str]:
        """Return the `api.graph_expand` DDL as single-command statements for this backend.

        Each element is one SQL command, run with its own `op.execute`.

        Returns:
            The `CREATE FUNCTION api.graph_expand(...)` definition, a `SECURITY DEFINER` walk from
            `seed_ids` out to `hops`, optionally filtered by `rel_types`, ranked by hop distance,
            capped at `k` and projecting `EXPAND_COLUMNS`, then its `COMMENT ON FUNCTION`.
        """
        ...

    def paths_statements(self) -> list[str]:
        """Return the `api.graph_paths` DDL as single-command statements for this backend.

        Each element is one SQL command, run with its own `op.execute`.

        Returns:
            The `CREATE FUNCTION api.graph_paths(...)` definition, a `SECURITY DEFINER` traversal of
            the paths from `from_id` to `to_id` within `max_hops`, optionally filtered by `rel_types`,
            capped at `k` and projecting `PATHS_COLUMNS`, then its `COMMENT ON FUNCTION`.
        """
        ...

    def refresh_statements(self) -> list[str]:
        """Return the `wiki.refresh_graph()` DDL as single-command statements.

        `wiki.refresh_graph()` is the writer-only routine the index load step calls right after
        replacing `wiki.edges`. Every backend emits it, as a no-op when there is no derived graph.
        It is the only write to the graph, so the `api.graph_*` functions only read and the read-only
        `kb_reader` role can run them.

        Returns:
            The `CREATE FUNCTION wiki.refresh_graph()` definition and its
            `COMMENT ON FUNCTION` (one SQL command per element).
        """
        ...
