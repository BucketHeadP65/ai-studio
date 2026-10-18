r"""Golden-query contract test locking the serve query layer to the `api.*` surface.

The serve handlers (`engram.retrieval.queries`) translate each query message into a single
call against the read-only `api` schema the migrations create. That cross-boundary contract
has two halves a future migration can silently break:

* the **parameter names** each `api.*` function declares (a renamed param makes the
  handler's `name => :bind` / positional call raise), and
* the **OUT columns** each `api.*` function/view projects (the handler reads cells off the
  row by name — `row["hit_level"]`, `row["src_id"]`, … — to hydrate the frozen
  `engram.retrieval.domain` models, so a renamed/dropped column makes the projection read
  `KeyError`/`None` at MCP/dashboard runtime).

This module executes the **real SQL constants** the handlers run (`engram.retrieval.queries`'s
`SEARCH_SQL`, `FETCH_SQL`, … — the published read surface, executed here exactly as the handlers
run them) against a freshly-migrated, empty testcontainer
database (the `migrated` fixture). An empty DB returns zero rows, but the result still carries
the column names, so each test asserts:

1. the query **executes without error** — proving the bound param names + arg types match the
   live `api.*` signature, and
2. `set(result.keys())` **equals** the exact OUT-column set the matching handler consumes —
   the contract is encoded as an explicit literal here, so a serve-side change that needs a new
   column also forces this test to be updated (the guard is documented and bidirectional).

So a migration renaming a param or an OUT column fails *here*, not at MCP/dashboard runtime.
The whole module skips when Docker is unavailable (the `pg` fixture, via `migrated`, raises
`pytest.skip`); `asyncio_mode = auto` runs the `async def` tests without a decorator.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from sqlalchemy import NullPool, text
from sqlalchemy.ext.asyncio import create_async_engine

from engram.retrieval.queries import (
    _GRAPH_EDGES_SQL,
    _GRAPH_NODES_SQL,
    _LOOKUP_SQL,
    FETCH_SQL,
    GRAPH_EXPAND_SQL,
    GRAPH_PATHS_SQL,
    SEARCH_SQL,
)

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Mapping

    from sqlalchemy.ext.asyncio import AsyncConnection

# ── the OUT-column contracts each serve handler consumes (the literals under test) ──
# Derived from the handler bodies in `engram.retrieval.queries` + the frozen models in
# `engram.retrieval.domain`, and confirmed against the migrations' `RETURNS TABLE(...)` /
# view SELECT lists (0002_api_surface.py, 0004_graph.py via the GraphBackendSql bodies).

# api.search → SearchHit: id, hit_level (the 0002 OUT rename of `level`), page_id, page_title,
# snippet, score, src_collection (the collection a hit belongs to), plus the per-leg facts behind that score — lexical_rank, vector_rank,
# vector_distance — and snippet_truncated, which says whether the excerpt was cut
# (0002 _create_functions / search_dialect.{paradedb,pg_textsearch} RETURNS TABLE).
_SEARCH_COLUMNS = frozenset(
    {
        "id",
        "src_collection",
        "hit_level",
        "page_id",
        "page_title",
        "snippet",
        "score",
        "lexical_rank",
        "vector_rank",
        "vector_distance",
        "snippet_truncated",
    }
)

# api.fetch → FetchResult.{id, level, title, body} (0002 `RETURNS TABLE (id, level, title, body)`).
_FETCH_COLUMNS = frozenset({"id", "level", "title", "body"})

# api.lookup_value → 0002 `RETURNS TABLE (value, definition, n_pages)` (LookupValueHandler passes
# the rows through verbatim as a SqlResult).
_LOOKUP_COLUMNS = frozenset({"value", "definition", "n_pages"})

# api.cards via _GRAPH_NODES_SQL → GraphNode (GraphHandler._graph_node reads id/type/title/status/
# superseded). The handler SELECTs exactly these columns from the api.cards view.
_CARDS_COLUMNS = frozenset({"id", "type", "title", "status", "superseded"})

# api.edges via _GRAPH_EDGES_SQL → GraphEdge (GraphHandler._graph_edge reads src_id/dst_id/rel/
# provenance_claim). The handler SELECTs exactly these columns from the api.edges view.
_EDGES_COLUMNS = frozenset({"src_id", "dst_id", "rel", "provenance_claim"})

# api.graph_expand → the node columns (GraphNode) plus the edge that reached each node (GraphEdge),
# one row per reached node. Byte-identical across the CTE/AGE backends
# (engram.retrieval.graph_backend.backend.EXPAND_COLUMNS).
_EXPAND_COLUMNS = frozenset({"id", "type", "title", "status", "superseded", "src_id", "dst_id", "rel", "provenance_claim"})

# api.graph_paths → the same node + edge columns as expand, plus the `path_rank` group the handler
# buckets rows into and the per-path `score` it ranks on (GraphPathsHandler._paths_by_rank /
# _graph_path read row["path_rank"] and row["score"]). = backend.PATHS_COLUMNS.
_PATHS_COLUMNS = _EXPAND_COLUMNS | {"path_rank", "score"}


@pytest.fixture
async def conn(migrated: str) -> AsyncIterator[AsyncConnection]:
    """An admin `AsyncConnection` to the freshly-migrated (empty) throwaway database.

    Admin (the `memory` role, not the read-only `kb_reader`) so every `api.*` object —
    including the views the reader can also see — is reachable without a grant concern; the
    contract under test is the param/column shape, which is role-independent. `NullPool`
    keeps no connection past the per-test database's lifetime.
    """
    engine = create_async_engine(migrated, poolclass=NullPool)
    try:
        async with engine.connect() as connection:
            yield connection
    finally:
        await engine.dispose()


async def _columns(connection: AsyncConnection, sql: str, params: Mapping[str, object]) -> frozenset[str]:
    """Execute `sql` with bound `params` and return its result column set.

    The empty DB yields zero rows, but `result.keys()` still lists the projected columns —
    and the `execute` itself proves the bound param names + arg types match the live `api.*`
    signature (a renamed param raises here). Reads run inside the caller's connection.
    """
    result = await connection.execute(text(sql), params)
    return frozenset(result.keys())


class TestSearchContract:
    """`api.search`: its params (query/k/query_vec/level/facets/collection/since/until) and OUT columns."""

    async def test_search_params_bind_and_out_columns_match(self, conn: AsyncConnection):
        # qvec=None → the BM25-only RRF leg (a NULL halfvec); binding every param by name proves
        # each api.search parameter still exists under its declared name.
        params = {
            "q": "x",
            "k": 1,
            "qvec": None,
            "level": None,
            "facets": "{}",
            "collection": "research",
            "since": None,
            "until": None,
            "query_model": None,
        }

        assert await _columns(conn, SEARCH_SQL, params) == _SEARCH_COLUMNS


class TestFetchContract:
    """`api.fetch(collection, unit_id)` — 0025 leads with the served collection + the OUT columns."""

    async def test_fetch_param_binds_and_out_columns_match(self, conn: AsyncConnection):
        assert await _columns(conn, FETCH_SQL, {"collection": "research", "unit_id": "nope"}) == _FETCH_COLUMNS


class TestLookupValueContract:
    """`api.lookup_value(collection, facet, term)` — 0025 leads with the served collection + the OUT columns."""

    async def test_lookup_params_bind_and_out_columns_match(self, conn: AsyncConnection):
        params = {"collection": "research", "facet": "task", "term": "x"}

        assert await _columns(conn, _LOOKUP_SQL, params) == _LOOKUP_COLUMNS


class TestGraphViewContract:
    """The `api.cards` / `api.edges` views the graph handler SELECTs from — their column sets."""

    async def test_cards_view_out_columns_match(self, conn: AsyncConnection):
        params = {"collection": "research", "page_type": None, "rel": None}

        assert await _columns(conn, _GRAPH_NODES_SQL, params) == _CARDS_COLUMNS

    async def test_edges_view_out_columns_match(self, conn: AsyncConnection):
        params = {"collection": "research", "page_type": None, "rel": None}

        assert await _columns(conn, _GRAPH_EDGES_SQL, params) == _EDGES_COLUMNS


class TestGraphFunctionContract:
    """`api.graph_expand` / `api.graph_paths` — their params + the reconciled OUT-column contracts.

    Both functions exist on the default analog bed (AGE) and the ParadeDB bed (CTE) — the
    contract is the backend-parity column order the serve layer binds to — so these run on
    whichever bed `migrated` provisioned, no backend gate. The handler's SQL is held to what the
    chain itself publishes, `include_superseded` included (0048), so a revision that stopped
    publishing an argument the handler binds fails here.
    """

    async def test_graph_expand_params_bind_and_out_columns_match(self, conn: AsyncConnection):
        params = {"collection": "research", "seed_ids": ["x"], "hops": 1, "rel_types": None, "k": 1, "include_superseded": False}

        assert await _columns(conn, GRAPH_EXPAND_SQL, params) == _EXPAND_COLUMNS

    async def test_graph_paths_params_bind_and_out_columns_match(self, conn: AsyncConnection):
        params = {"collection": "research", "from_id": "x", "to_id": "y", "max_hops": 2, "rel_types": None, "k": 1, "include_superseded": False}

        assert await _columns(conn, GRAPH_PATHS_SQL, params) == _PATHS_COLUMNS
