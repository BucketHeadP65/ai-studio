r"""`Graph` over a real, migrated ParadeDB — the whole-knowledge-base graph + `page_type` / `rel` filters.

The read path: seed `kb.pages` (the nodes) and `kb.edges` (the typed relations) directly
through the indexing slice's repositories — the same direct-seeding idiom as
`tests/integration/test_repositories.py`, which is the right granularity for graph *shape* and
*filter* assertions (no BM25/embedding machinery is needed here) — then query through the
production read-only :class:`~engram.retrieval.infrastructure.gateway.ReadOnlySqlGateway` and the new
:class:`~engram.retrieval.queries.GraphHandler`.

The handler is constructed **directly** against the test gateway/engine, not through DI.
`GraphHandler` reads `api.cards` (nodes) and `api.edges`
(typed relations) — the only two surfaces the contract uses — so:

* no filters returns the whole seeded graph (every node, every edge);
* `page_type` narrows the node set to one type, and edges are restricted to surviving endpoints
  (an edge whose `src` or `dst` is filtered out is dropped);
* `rel` narrows the edge set to one relation (the node set is unchanged);
* a claim-level edge endpoint (an id that is not a page card) is excluded from the edge set, since
  every edge endpoint must be a surviving node.

The whole module skips when Docker is unavailable (the `pg` fixture, via `migrated`, raises
`pytest.skip`). Tests are *synchronous*: each seeding/read step wraps its own `asyncio.run` —
the same shape as `tests/integration/test_search.py`.
"""

from __future__ import annotations

import asyncio

from typing import TYPE_CHECKING

import pytest

from sqlalchemy import NullPool
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine

from engram.indexing.infrastructure import orm
from engram.indexing.infrastructure.repository import Edges, Pages
from engram.retrieval.domain import Graph
from engram.retrieval.infrastructure.gateway import ReadOnlySqlGateway
from engram.retrieval.queries import GraphHandler

if TYPE_CHECKING:
    from collections.abc import Coroutine

    from engram.retrieval.domain import GraphResult

# ── seed data: four pages (two papers, two entities) + three typed edges ─────
# Nodes span >1 type so a page_type filter has something to narrow. One edge endpoint is a
# claim id (not a page card) so the surviving-endpoint restriction has something to drop.

_PAGES: tuple[dict[str, object], ...] = (
    {"id": "paper.graph", "page_type": "paper", "title": "Graph Indexing", "status": "synthesized"},
    {"id": "paper.cache", "page_type": "paper", "title": "Vector Cache", "status": "synthesized"},
    {"id": "ent.graphrag", "page_type": "entity", "title": "GraphRAG", "status": "verified"},
    {"id": "ent.bm25", "page_type": "entity", "title": "BM25", "status": "draft"},
)

# (src_id, rel, dst_id, provenance_claim). The third edge's dst is a claim id with no page card —
# it must be dropped (both endpoints must be surviving nodes).
_EDGES: tuple[tuple[str, str, str, str | None], ...] = (
    ("paper.graph", "evaluates", "ent.graphrag", "paper.graph#c1"),
    ("paper.cache", "builds_on", "ent.bm25", None),
    ("paper.graph", "cites", "paper.cache#c2", None),
)


def _page_row(page: dict[str, object]) -> dict[str, object]:
    """A minimal `kb.pages` upsert row honouring every NOT NULL column (collection/page_type/slug/status/card_text/body_hash)."""
    page_id = str(page["id"])
    return {
        "id": page_id,
        "collection": "research",
        "page_type": page["page_type"],
        "slug": page_id.split(".", 1)[-1],
        "title": page["title"],
        "status": page["status"],
        "summary": None,
        "card_text": f"{page['page_type']}: {page['title']}",
        "body_hash": f"h-{page_id}",
    }


def _edge_row(edge: tuple[str, str, str, str | None]) -> dict[str, object]:
    """A `kb.edges` insert row — src/dst page_types are nominal (the api.edges view does not expose them)."""
    src_id, rel, dst_id, provenance = edge
    return {
        "src_collection": "research",
        "src_id": src_id,
        "src_kind": "page",
        "rel": rel,
        "dst_collection": "research",
        "dst_id": dst_id,
        "dst_kind": "page",
        "provenance_claim": provenance,
    }


async def _seed(engine_url: str) -> None:
    """Upsert the pages and replace the edge set in one session (the test_repositories idiom)."""
    engine = create_async_engine(engine_url, poolclass=NullPool)
    await orm.register_pgvector(engine)
    try:
        async with AsyncSession(engine, expire_on_commit=False) as db:
            await Pages(db).upsert_many([_page_row(page) for page in _PAGES])
            await Edges(db).replace_all([_edge_row(edge) for edge in _EDGES], collection="research")
            await db.commit()
    finally:
        await engine.dispose()


def _run[T](coro: Coroutine[object, object, T]) -> T:
    """Drive a coroutine from a synchronous test body (no loop is running)."""
    return asyncio.run(coro)


@pytest.fixture
def graph_db(migrated: str) -> str:
    """Seed the migrated DB with pages + edges; return its (admin) URL.

    The `api` views run as their owner, so the admin URL reads them. The handler is built
    directly against a gateway over this URL, not through DI.
    """
    _run(_seed(migrated))
    return migrated


def _graph(db_url: str, query: Graph) -> GraphResult:
    """Run one :class:`Graph` query through a gateway over ``db_url`` and the :class:`GraphHandler`."""

    async def _go() -> GraphResult:
        engine = create_async_engine(db_url, poolclass=NullPool)
        await orm.register_pgvector(engine)
        try:
            return await GraphHandler(ReadOnlySqlGateway(engine), "research")(query)
        finally:
            await engine.dispose()

    return _run(_go())


def _node_ids(result: GraphResult) -> set[str]:
    return {node.id for node in result.nodes}


def _edge_rels(result: GraphResult) -> set[str]:
    return {edge.rel for edge in result.edges}


def _edge_pairs(result: GraphResult) -> set[tuple[str, str]]:
    return {(edge.src_id, edge.dst_id) for edge in result.edges}


class TestWholeGraph:
    def test_no_filters_returns_every_seeded_node(self, graph_db: str):
        result = _graph(graph_db, Graph())

        assert _node_ids(result) == {"paper.graph", "paper.cache", "ent.graphrag", "ent.bm25"}

    def test_no_filters_returns_edges_between_surviving_nodes_only(self, graph_db: str):
        result = _graph(graph_db, Graph())

        # The two page→page/entity edges survive; the edge into the claim id paper.cache#c2 is dropped.
        assert _edge_pairs(result) == {("paper.graph", "ent.graphrag"), ("paper.cache", "ent.bm25")}

    def test_node_shape_matches_the_cards_view(self, graph_db: str):
        result = _graph(graph_db, Graph())

        node = next(n for n in result.nodes if n.id == "ent.graphrag")
        assert (node.type, node.title, node.status, node.superseded) == ("entity", "GraphRAG", "verified", False)

    def test_edge_shape_carries_provenance_claim(self, graph_db: str):
        result = _graph(graph_db, Graph())

        evaluates = next(e for e in result.edges if e.rel == "evaluates")
        assert (evaluates.src_id, evaluates.dst_id, evaluates.provenance_claim) == ("paper.graph", "ent.graphrag", "paper.graph#c1")

    def test_edge_without_provenance_is_null(self, graph_db: str):
        result = _graph(graph_db, Graph())

        builds_on = next(e for e in result.edges if e.rel == "builds_on")
        assert builds_on.provenance_claim is None


class TestPageTypeFilter:
    def test_page_type_narrows_nodes_to_one_type(self, graph_db: str):
        result = _graph(graph_db, Graph(page_type="entity"))

        assert _node_ids(result) == {"ent.graphrag", "ent.bm25"}

    def test_page_type_drops_edges_whose_endpoint_was_filtered_out(self, graph_db: str):
        # Both surviving edges have a paper as src; filtering to entities removes every edge endpoint.
        result = _graph(graph_db, Graph(page_type="entity"))

        assert result.edges == ()


class TestRelFilter:
    def test_rel_narrows_edges_to_one_relation(self, graph_db: str):
        result = _graph(graph_db, Graph(rel="evaluates"))

        assert _edge_rels(result) == {"evaluates"}
        assert _edge_pairs(result) == {("paper.graph", "ent.graphrag")}

    def test_rel_filter_leaves_the_node_set_whole(self, graph_db: str):
        result = _graph(graph_db, Graph(rel="evaluates"))

        assert _node_ids(result) == {"paper.graph", "paper.cache", "ent.graphrag", "ent.bm25"}
