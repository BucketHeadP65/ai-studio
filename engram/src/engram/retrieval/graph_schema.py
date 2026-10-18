"""The link graph as a `GraphSchema`, the data the graph builder generates the traversal SQL from.

The traversal walks `wiki.edges` (`src_id` to `dst_id`, typed by `rel`), carries each edge's
`provenance_claim` onto every reached node and joins `api.cards` for a node's display columns.
`KB_GRAPH_SCHEMA` must keep the `wiki.edges` name: migration `0004_graph` executes its projection DDL
directly, before migration `0010` renames the schema.
"""

from __future__ import annotations

from core.ai.retrieval.contracts import GraphSchema

# The graph schema the migrations generate the traversal functions from. Each reached node is joined
# to `api.cards` for its display columns and carries the edge's `provenance_claim`.
KB_GRAPH_SCHEMA = GraphSchema(
    edge_table="wiki.edges",
    src_col="src_id",
    dst_col="dst_id",
    rel_col="rel",
    edge_payload_cols=("provenance_claim",),
    node_view="api.cards",
    node_cols=("id", "type", "title", "status", "superseded"),
    node_col_types=("text", "text", "text", "text", "boolean"),
)


# The relation the Apache AGE projection reads a page's currency from, covering every collection.
_PAGE_CURRENCY_SOURCE = "(SELECT p.id, p.collection AS src_collection, p.superseded_by IS NOT NULL AS superseded FROM kb.pages p)"


def kb_graph_schema(*, edge_table: str) -> GraphSchema:
    """The graph shape a traversal published today walks: partitioned by collection, current by default.

    `src_collection` keeps every hop inside the caller's collection. `api.cards.superseded` keeps a
    replaced page, and anything reachable only through one, out of an ordinary answer. The Apache AGE
    projection reads currency from `kb.pages`.

    Args:
        edge_table: The relation the traversal walks (`kb.visible_edges` under the recursive-CTE
            backend, `kb.edges` under Apache AGE).

    Returns:
        The descriptor both traversal builders generate today's `api.graph_expand` /
        `api.graph_paths` from.
    """
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
