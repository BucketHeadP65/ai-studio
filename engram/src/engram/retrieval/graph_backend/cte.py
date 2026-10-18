r"""The recursive-CTE graph backend, the portable walk over `wiki.edges`.

`CteGraphBackend` emits `api.graph_expand` and `api.graph_paths` as `WITH RECURSIVE` traversals of
`wiki.edges`, joined to `api.cards` for node titles and types, carrying each edge's `provenance_claim`.
It runs on any Postgres with no graph extension.

Both functions are `SECURITY DEFINER`, pin `search_path` to `pg_catalog, wiki, public, pg_temp`, cap `k` at 50,
guard each walk against cycles and filter by the optional `rel_types` array, where NULL means every
relation. They project `EXPAND_COLUMNS` and `PATHS_COLUMNS`, the same columns as the AGE backend.
The migration adds the `GRANT EXECUTE ... TO kb_reader`.
"""

from __future__ import annotations

from typing import Literal

from core.ai.retrieval.contracts import CteGraphTraversalBuilder

from engram.retrieval.graph_schema import KB_GRAPH_SCHEMA

from .backend import EXPAND_COMMENT, PATHS_COMMENT

# Generates the recursive-CTE bodies from a graph schema.
_BUILDER = CteGraphTraversalBuilder()

# The COMMENT on the no-op `wiki.refresh_graph()`, returned as a separate single command.
_REFRESH_COMMENT = (
    "COMMENT ON FUNCTION wiki.refresh_graph() IS "
    "'No-op under the recursive-CTE backend (api.graph_* walk wiki.edges live, with no projected "
    "graph). Present so the index load step can refresh the graph backend-agnostically.'"
)


class CteGraphBackend:
    """Emits the portable recursive-CTE link-graph SQL over `wiki.edges`.

    Needs no graph projection. The function bodies project the same columns as the AGE backend.
    """

    name: Literal["cte", "age"] = "cte"

    def create_graph_ddl(self) -> list[str]:
        """Return no statements, since this backend walks `wiki.edges` directly.

        Returns:
            `[]`: no graph extension or projection is required for this backend.
        """
        return _BUILDER.create_graph_ddl(KB_GRAPH_SCHEMA)

    def expand_statements(self) -> list[str]:
        """Return the `api.graph_expand` definition + COMMENT as single commands.

        Returns:
            Two single-command elements, the `CREATE FUNCTION api.graph_expand(...)` walk, then its
            M-Schema COMMENT, each safe for a single asyncpg `op.execute`.
        """
        return [_BUILDER.expand_body(KB_GRAPH_SCHEMA), EXPAND_COMMENT]

    def paths_statements(self) -> list[str]:
        """Return the `api.graph_paths` definition + COMMENT as single commands.

        Returns:
            Two single-command elements, the `CREATE FUNCTION api.graph_paths(...)` path enumeration,
            then its M-Schema COMMENT, each safe for a single asyncpg `op.execute`.
        """
        return [_BUILDER.paths_body(KB_GRAPH_SCHEMA), PATHS_COMMENT]

    def refresh_statements(self) -> list[str]:
        """Return `wiki.refresh_graph()` as a no-op function, and its COMMENT.

        Returns:
            Two single-command elements, `CREATE FUNCTION wiki.refresh_graph()` with an empty body and
            its COMMENT, each safe for a single asyncpg `op.execute`.
        """
        return [_BUILDER.refresh_body(KB_GRAPH_SCHEMA), _REFRESH_COMMENT]
