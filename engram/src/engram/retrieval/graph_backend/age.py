r"""The Apache AGE graph backend, an openCypher variable-length traversal.

`AgeGraphBackend` emits `api.graph_expand` and `api.graph_paths` over the Apache AGE property graph
`wiki_kg`, projected from `wiki.edges`. Each function wraps a variable-length `cypher` match with the
optional relation filter and returns the same ordered columns as the recursive CTE backend.

`create_graph_ddl` is the one-time projection DDL. The graph's data is filled by the writer-only
`wiki.refresh_graph()` at index time, so the query bodies only read.

Both functions are `SECURITY DEFINER` with `search_path` pinned to `pg_catalog, ag_catalog, wiki, public, pg_temp`,
cap `k` at 50 and carry their M-Schema COMMENT. The migration adds the `GRANT EXECUTE ... TO kb_reader`.
"""

from __future__ import annotations

from typing import Literal

from core.ai.retrieval.contracts import AgeGraphTraversalBuilder

from engram.retrieval.graph_schema import KB_GRAPH_SCHEMA

from .backend import EXPAND_COMMENT, PATHS_COMMENT

# Generates the AGE bodies and projection DDL from a graph schema. The graph name must stay `wiki_kg`:
# migrations replay these statements under that name, and revision `0046_knowledge_base_names` renames
# the live graph to `kb_graph`.
_BUILDER = AgeGraphTraversalBuilder("wiki_kg")

# The COMMENT on `wiki.refresh_graph()`, returned as a separate single command.
_REFRESH_COMMENT = (
    "COMMENT ON FUNCTION wiki.refresh_graph() IS "
    "'Rebuild the wiki_kg AGE property graph from wiki.edges. Writer-only — run by the index load "
    "step after the edge set is replaced; the read-only api.graph_expand/api.graph_paths functions only "
    "traverse the projection it produces.'"
)


class AgeGraphBackend:
    """Emits the Apache AGE openCypher link-graph SQL over the `wiki_kg` graph.

    The function bodies map `agtype` results back to the same columns the CTE backend returns.
    """

    name: Literal["cte", "age"] = "age"

    def create_graph_ddl(self) -> list[str]:
        """Return the one-time AGE extension and graph projection DDL as single commands.

        Returns:
            The `CREATE EXTENSION age`, `create_graph('wiki_kg')` and label commands, one per element
            and never empty, each safe for a single asyncpg `op.execute`.
        """
        return _BUILDER.create_graph_ddl(KB_GRAPH_SCHEMA)

    def expand_statements(self) -> list[str]:
        """Return the `api.graph_expand` definition + COMMENT as single commands.

        Returns:
            Two single-command elements, the `CREATE FUNCTION api.graph_expand(...)` neighbourhood
            match capped at `k`, then its M-Schema COMMENT, each safe for a single asyncpg `op.execute`.
        """
        return [_BUILDER.expand_body(KB_GRAPH_SCHEMA), EXPAND_COMMENT]

    def paths_statements(self) -> list[str]:
        """Return the `api.graph_paths` definition + COMMENT as single commands.

        Returns:
            Two single-command elements, the `CREATE FUNCTION api.graph_paths(...)` path match capped
            at `k`, then its M-Schema COMMENT, each safe for a single asyncpg `op.execute`.
        """
        return [_BUILDER.paths_body(KB_GRAPH_SCHEMA), PATHS_COMMENT]

    def refresh_statements(self) -> list[str]:
        """Return `wiki.refresh_graph()`, the writer-only rebuild of the `wiki_kg` graph, and its COMMENT.

        Returns:
            Two single-command elements, `CREATE FUNCTION wiki.refresh_graph()` and its COMMENT, each
            safe for a single asyncpg `op.execute`.
        """
        return [_BUILDER.refresh_body(KB_GRAPH_SCHEMA), _REFRESH_COMMENT]
