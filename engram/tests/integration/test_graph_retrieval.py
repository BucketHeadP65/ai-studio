r"""Functional `api.graph_expand` / `api.graph_paths` over a seeded `kb.edges`, on whichever backend the bed runs.

Where :mod:`tests.integration.test_migrations` proves the chain *creates* the graph functions (and
projects the AGE graph) under each backend, this module proves they **return the right rows**: a
deliberately awkward `kb.edges` graph is seeded and both traversals are exercised end to end —

* **Currency** — a superseded intermediary and the descendant reachable only through it are both
  absent by default, and both present under `include_superseded`;
* **Determinism** — a convergent, high-degree neighbourhood returns byte-identical ordered rows on
  every run, whichever order the rows were written in;
* **Provenance** — two authored `cites` assertions between the same pair both survive selection;
* **Truncation** — a `k` smaller than the neighbourhood answers exactly `k` logical nodes and says
  more exist, without claiming to know how many;
* **Isolation** — another collection's page under the same bare id never surfaces.

The bed decides the backend: the default local Postgres serves Apache AGE, and
`KNOWLEDGE_BASE_TEST_DIALECT=pg_search` serves the recursive CTE. Both run the same assertions, which
is the parity the serve layer binds to. The whole module skips when Docker is unavailable (the `pg`
fixture, via `migrated`, raises `pytest.skip`).

The functions under test are the ones the applied chain itself published (0048), reached through the
`api` wrappers the serve layer calls, so these assertions are about the SQL a migrated database
really runs. The fixture seeds the graph and runs `kb.refresh_graph()`, the writer-only projection
step an index run performs, and nothing else.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import asyncpg
import pytest

from sqlalchemy import NullPool
from sqlalchemy.ext.asyncio import create_async_engine

from engram.indexing.infrastructure import orm
from engram.retrieval.domain import GraphExpand
from engram.retrieval.infrastructure.gateway import ReadOnlySqlGateway
from engram.retrieval.queries import GraphExpandHandler
from engram.testing.infrastructure.knowledge_base_db import provision_private_reader
from tests.integration.test_migrations import _asyncpg_dsn

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

    from core.ai.retrieval.contracts import GraphResult

_COLLECTION = "research"
_OTHER = "other"

# A third collection that declares itself private (`private: true` on its indexed switches), read
# below from its own session. Privacy and currency are independent: a collection kept out of the
# shared library is still answered by currency to the one session that may read it.
_PRIVATE = "private-kb"
_PRIVATE_SWITCHES = '{"embeddings": false, "sql": false, "graph": true, "private": true}'

_ENT_A = "ent.a"
_ENT_B = "ent.b"
_ENT_C = "ent.c"
_ENT_E = "ent.e"
_ENT_D = "ent.d"
_ENT_STALE = "ent.stale"
_ENT_BEHIND = "ent.behind"
_ENT_SUCCESSOR = "ent.successor"
_ENT_FOREIGN = "ent.foreign"

# The private collection's own chain: a seed, a superseded intermediary, and the node only that
# intermediary reaches, plus the page that replaced it.
_ENT_PRIVATE_SEED = "ent.private-seed"
_ENT_PRIVATE_STALE = "ent.private-stale"
_ENT_PRIVATE_BEHIND = "ent.private-behind"
_ENT_PRIVATE_SUCCESSOR = "ent.private-successor"

# 25 one-hop neighbours of A, written in reverse id order so insertion order can never pass for the
# `(hop, id)` order the traversal promises.
_FAN_OUT = tuple(f"ent.n{index:02d}" for index in range(25))

# (collection, id, superseded_by) — every node is a `page_type = 'entity'` page card so api.cards
# resolves type/title/status/superseded. `ent.stale` is the one page a newer page replaces, and
# `ent.behind` hangs off it alone.
_PAGES: tuple[tuple[str, str, str | None], ...] = (
    (_COLLECTION, _ENT_A, None),
    (_COLLECTION, _ENT_B, None),
    (_COLLECTION, _ENT_C, None),
    (_COLLECTION, _ENT_E, None),
    (_COLLECTION, _ENT_D, None),
    (_COLLECTION, _ENT_SUCCESSOR, None),
    (_COLLECTION, _ENT_STALE, _ENT_SUCCESSOR),
    (_COLLECTION, _ENT_BEHIND, None),
    *((_COLLECTION, node_id, None) for node_id in reversed(_FAN_OUT)),
    # The same bare id in another collection, plus a node only that collection reaches.
    (_OTHER, _ENT_A, None),
    (_OTHER, _ENT_FOREIGN, None),
    # The private collection's chain, shaped exactly like the shared one's stale route.
    (_PRIVATE, _ENT_PRIVATE_SEED, None),
    (_PRIVATE, _ENT_PRIVATE_SUCCESSOR, None),
    (_PRIVATE, _ENT_PRIVATE_STALE, _ENT_PRIVATE_SUCCESSOR),
    (_PRIVATE, _ENT_PRIVATE_BEHIND, None),
)

# (src_collection, src_id, rel, dst_collection, dst_id, provenance_claim).
_EDGES: tuple[tuple[str, str, str, str, str, str | None], ...] = (
    # Two authored assertions between the same pair: both must survive selection.
    (_COLLECTION, _ENT_A, "cites", _COLLECTION, _ENT_B, "ent.a#c1"),
    (_COLLECTION, _ENT_A, "cites", _COLLECTION, _ENT_B, "ent.a#c2"),
    # C is reached at the same hop by two different routes.
    (_COLLECTION, _ENT_A, "extends", _COLLECTION, _ENT_E, None),
    (_COLLECTION, _ENT_B, "cites", _COLLECTION, _ENT_C, "ent.b#c1"),
    (_COLLECTION, _ENT_E, "cites", _COLLECTION, _ENT_C, "ent.e#c1"),
    # A leaf three hops out, reached only across a relation the `cites` filter drops.
    (_COLLECTION, _ENT_C, "builds_on", _COLLECTION, _ENT_D, "ent.c#c1"),
    # The back-edge: the cycle both backends' guards have to hold against.
    (_COLLECTION, _ENT_C, "cites", _COLLECTION, _ENT_A, None),
    # The superseded intermediary and the descendant only it reaches.
    (_COLLECTION, _ENT_A, "cites", _COLLECTION, _ENT_STALE, "ent.a#c3"),
    (_COLLECTION, _ENT_STALE, "cites", _COLLECTION, _ENT_BEHIND, "ent.stale#c1"),
    # The high-degree fan-out.
    *((_COLLECTION, _ENT_A, "relates_to", _COLLECTION, node_id, None) for node_id in reversed(_FAN_OUT)),
    # The other collection's own route out of its same-id page.
    (_OTHER, _ENT_A, "cites", _OTHER, _ENT_FOREIGN, None),
    # The private collection's own superseded intermediary and what hangs off it alone.
    (_PRIVATE, _ENT_PRIVATE_SEED, "cites", _PRIVATE, _ENT_PRIVATE_STALE, None),
    (_PRIVATE, _ENT_PRIVATE_STALE, "cites", _PRIVATE, _ENT_PRIVATE_BEHIND, None),
)


async def _seed(conn: asyncpg.Connection) -> None:
    """Insert the node cards (`kb.pages`) and the typed edges (`kb.edges`), then project the graph.

    Direct SQL inserts, the asyncpg idiom of this module. Pages are written before the
    `superseded_by` links they point at, so the self-reference resolves. `kb.refresh_graph()` is the
    writer-only projection step the load step normally runs: a no-op under the recursive-CTE
    backend, and what puts the vertices (and their currency) in the graph under Apache AGE.
    """
    await conn.executemany(
        "INSERT INTO kb.pages (id, collection, page_type, slug, title, status, card_text, body_hash)"
        " VALUES ($1, $2, 'entity', $3, $4, 'verified', $5, $6)",
        [
            (page_id, collection, f"{collection}-{page_id}", page_id.upper(), f"entity: {page_id}", f"h-{collection}-{page_id}")
            for collection, page_id, _superseded_by in _PAGES
        ],
    )
    await conn.executemany(
        "UPDATE kb.pages SET superseded_by = $3 WHERE collection = $1 AND id = $2",
        [(collection, page_id, superseded_by) for collection, page_id, superseded_by in _PAGES if superseded_by is not None],
    )
    await conn.executemany(
        "INSERT INTO kb.edges (src_collection, src_id, src_kind, rel, dst_collection, dst_id, dst_kind, provenance_claim)"
        " VALUES ($1, $2, 'page', $3, $4, $5, 'page', $6)",
        list(_EDGES),
    )
    await conn.execute(
        "INSERT INTO kb.index_fingerprint (collection, indexer_version, spec_hash, vocabulary_hash, embed_model, switches)"
        " VALUES ($1, '1', 'spec', 'vocab', 'embed', $2::jsonb)",
        _PRIVATE,
        _PRIVATE_SWITCHES,
    )
    await conn.execute("SELECT kb.refresh_graph()")


@pytest.fixture
async def graph_db(migrated: str) -> str:
    """A migrated database with the seeded graph projected over it; yields its admin URL.

    `migrated` is taken as a direct fixture parameter (not `request.getfixturevalue`) so
    pytest-asyncio resolves the async fixture on the test's running loop.
    """
    connection = await asyncpg.connect(_asyncpg_dsn(migrated))
    try:
        await _seed(connection)
    finally:
        await connection.close()
    return migrated


@pytest.fixture
async def graph_conn(graph_db: str) -> AsyncIterator[asyncpg.Connection]:
    """A connection to the seeded database, for the assertions that read the function's rows directly."""
    connection = await asyncpg.connect(_asyncpg_dsn(graph_db))
    try:
        yield connection
    finally:
        await connection.close()


@pytest.fixture
async def private_conn(graph_db: str) -> AsyncIterator[asyncpg.Connection]:
    """A connection logged in as the private collection's own reader role, the only reader that may read it."""
    connection = await asyncpg.connect(_asyncpg_dsn(await provision_private_reader(graph_db, _PRIVATE)))
    try:
        yield connection
    finally:
        await connection.close()


@pytest.fixture
async def anonymous_conn(graph_db: str, reader_url: str) -> AsyncIterator[asyncpg.Connection]:
    """A `kb_reader` connection that reads as no knowledge base."""
    del graph_db
    connection = await asyncpg.connect(_asyncpg_dsn(reader_url))
    try:
        yield connection
    finally:
        await connection.close()


async def _expand(db_url: str, query: GraphExpand) -> GraphResult:
    """One expansion through the production gateway and handler, the way the serve layer runs it."""
    engine = create_async_engine(db_url, poolclass=NullPool)
    await orm.register_pgvector(engine)
    try:
        return await GraphExpandHandler(ReadOnlySqlGateway(engine), _COLLECTION)(query)
    finally:
        await engine.dispose()


async def _rows(conn: asyncpg.Connection, call: str, *args: object) -> list[str]:
    """One traversal's rows as ordered JSON text, so two runs compare byte for byte.

    `call` is one of this module's own two `api.graph_*` calls, never caller text.
    """
    records = await conn.fetch("SELECT row_to_json(t) AS row FROM " + call + " t", *args)  # noqa: S608 (a fixed call from this module)
    return [str(record["row"]) for record in records]


def _node_ids(rows: list[asyncpg.Record]) -> set[str]:
    """The distinct node ids a traversal returned."""
    return {row["id"] for row in rows}


class TestCurrency:
    """A superseded page, and anything reachable only through one, stays out until it is asked for."""

    async def test_the_default_walk_skips_the_superseded_node_and_its_descendant(self, graph_conn: asyncpg.Connection):
        rows = await graph_conn.fetch("SELECT * FROM api.graph_expand($1, $2, $3)", _COLLECTION, [_ENT_A], 3)

        reached = _node_ids(rows)
        assert _ENT_STALE not in reached
        assert _ENT_BEHIND not in reached, "a descendant reached only through a superseded page must not surface"
        assert _ENT_B in reached  # the rest of the neighbourhood is untouched

    async def test_the_opt_in_walks_the_superseded_branch(self, graph_conn: asyncpg.Connection):
        rows = await graph_conn.fetch(
            "SELECT * FROM api.graph_expand($1, $2, $3, NULL, $4, $5)", _COLLECTION, [_ENT_A], 3, 50, True
        )

        reached = _node_ids(rows)
        assert {_ENT_STALE, _ENT_BEHIND} <= reached

    async def test_a_route_through_a_superseded_node_is_skipped_by_default_and_kept_on_request(self, graph_conn: asyncpg.Connection):
        current = await graph_conn.fetch("SELECT * FROM api.graph_paths($1, $2, $3, $4)", _COLLECTION, _ENT_A, _ENT_BEHIND, 4)
        opted_in = await graph_conn.fetch(
            "SELECT * FROM api.graph_paths($1, $2, $3, $4, NULL, $5, $6)", _COLLECTION, _ENT_A, _ENT_BEHIND, 4, 10, True
        )

        assert current == []
        assert [row["id"] for row in opted_in if row["path_rank"] == 1] == [_ENT_A, _ENT_STALE, _ENT_BEHIND]

    async def test_the_seed_itself_is_never_judged_stale(self, graph_conn: asyncpg.Connection):
        # Asking what a replaced page connects to stays possible: the walk judges what it REACHES.
        rows = await graph_conn.fetch("SELECT * FROM api.graph_expand($1, $2, $3)", _COLLECTION, [_ENT_STALE], 1)

        assert _ENT_STALE in _node_ids(rows)


class TestDeterminism:
    """The same query returns the same ordered rows every time, on either backend."""

    async def test_five_expansions_return_byte_identical_ordered_rows(self, graph_conn: asyncpg.Connection):
        runs = [
            await _rows(graph_conn, "api.graph_expand($1, $2, $3)", _COLLECTION, [_ENT_A], 2) for _ in range(5)
        ]

        assert all(run == runs[0] for run in runs)
        assert runs[0], "expected the expansion to return rows"

    async def test_five_path_searches_return_byte_identical_ordered_rows(self, graph_conn: asyncpg.Connection):
        runs = [
            await _rows(graph_conn, "api.graph_paths($1, $2, $3, $4)", _COLLECTION, _ENT_A, _ENT_C, 4)
            for _ in range(5)
        ]

        assert all(run == runs[0] for run in runs)
        assert runs[0], "expected at least one route from A to C"

    async def test_the_neighbourhood_is_ordered_by_hop_then_id_not_by_insertion(self, graph_conn: asyncpg.Connection):
        rows = await graph_conn.fetch("SELECT * FROM api.graph_expand($1, $2, $3, NULL, $4)", _COLLECTION, [_ENT_A], 1, 50)

        reached = [row["id"] for row in rows]
        assert reached[0] == _ENT_A  # the seed arrives at hop 0
        fan_out = [node_id for node_id in reached if node_id in set(_FAN_OUT)]
        assert fan_out == sorted(fan_out), "the fan-out was written in reverse id order; the rows must come back in id order"
        assert len(fan_out) == len(_FAN_OUT)

    async def test_both_routes_that_reach_the_same_node_at_the_same_hop_survive(self, graph_conn: asyncpg.Connection):
        rows = await graph_conn.fetch("SELECT * FROM api.graph_expand($1, $2, $3, NULL, $4)", _COLLECTION, [_ENT_A], 2, 50)

        into_c = {(row["src_id"], row["provenance_claim"]) for row in rows if row["dst_id"] == _ENT_C}
        assert into_c == {(_ENT_B, "ent.b#c1"), (_ENT_E, "ent.e#c1")}


class TestProvenance:
    """Two authored assertions between the same pair are two pieces of evidence, not one."""

    async def test_both_parallel_assertions_come_back(self, graph_conn: asyncpg.Connection):
        rows = await graph_conn.fetch("SELECT * FROM api.graph_expand($1, $2, $3, NULL, $4)", _COLLECTION, [_ENT_A], 1, 50)

        claims = sorted(row["provenance_claim"] for row in rows if row["dst_id"] == _ENT_B and row["src_id"] == _ENT_A)
        assert claims == ["ent.a#c1", "ent.a#c2"]

    async def test_both_parallel_assertions_rank_as_two_routes(self, graph_conn: asyncpg.Connection):
        rows = await graph_conn.fetch("SELECT * FROM api.graph_paths($1, $2, $3, $4)", _COLLECTION, _ENT_A, _ENT_B, 2)

        claims = sorted(row["provenance_claim"] for row in rows if row["provenance_claim"] is not None)
        assert claims == ["ent.a#c1", "ent.a#c2"]
        assert {row["path_rank"] for row in rows} == {1, 2}

    async def test_a_reached_node_is_joined_to_its_card(self, graph_conn: asyncpg.Connection):
        rows = await graph_conn.fetch("SELECT * FROM api.graph_expand($1, $2, $3)", _COLLECTION, [_ENT_A], 1)

        row = next(row for row in rows if row["id"] == _ENT_B)
        assert (row["type"], row["title"], row["status"], row["superseded"]) == ("entity", _ENT_B.upper(), "verified", False)


class TestTruncationIsHonest:
    """A `k` smaller than the neighbourhood says more exists without claiming to know how much."""

    async def test_twenty_nodes_come_back_cut_with_an_uncounted_remainder(self, graph_db: str):
        result = await _expand(graph_db, GraphExpand(seed_ids=(_ENT_A,), hops=1, k=20))

        assert len({node.id for node in result.nodes}) == 20
        assert result.window.returned_items == 20
        assert result.window.truncated is True
        assert result.window.omitted_items is None
        assert result.window.reason

    async def test_the_traversal_selects_exactly_one_logical_node_past_the_cap(self, graph_conn: asyncpg.Connection):
        rows = await graph_conn.fetch("SELECT * FROM api.graph_expand($1, $2, $3, NULL, $4)", _COLLECTION, [_ENT_A], 1, 20)

        assert len(_node_ids(rows)) == 21, "one node past the cap is the probe that proves more exist"

    async def test_a_complete_neighbourhood_reports_nothing_omitted(self, graph_db: str):
        result = await _expand(graph_db, GraphExpand(seed_ids=(_ENT_C,), hops=1, k=20))

        assert result.window.truncated is False
        assert result.window.omitted_items == 0

    async def test_every_returned_edge_relates_two_returned_nodes(self, graph_db: str):
        result = await _expand(graph_db, GraphExpand(seed_ids=(_ENT_A,), hops=1, k=20))

        reached = {node.id for node in result.nodes}
        assert all(edge.src_id in reached and edge.dst_id in reached for edge in result.edges)


class TestCollectionIsolation:
    """Another collection's page under the same bare id is another page."""

    async def test_the_foreign_collections_neighbour_never_surfaces(self, graph_conn: asyncpg.Connection):
        rows = await graph_conn.fetch("SELECT * FROM api.graph_expand($1, $2, $3, NULL, $4)", _COLLECTION, [_ENT_A], 3, 50)

        assert _ENT_FOREIGN not in _node_ids(rows)

    async def test_the_other_collection_reaches_its_own_neighbour(self, graph_conn: asyncpg.Connection):
        # The trap is real: the foreign edge exists and is reachable from ITS own collection.
        rows = await graph_conn.fetch("SELECT * FROM api.graph_expand($1, $2, $3)", _OTHER, [_ENT_A], 1)

        assert _ENT_FOREIGN in _node_ids(rows)

    async def test_no_route_crosses_into_the_other_collection(self, graph_conn: asyncpg.Connection):
        rows = await graph_conn.fetch("SELECT * FROM api.graph_paths($1, $2, $3, $4)", _COLLECTION, _ENT_A, _ENT_FOREIGN, 4)

        assert rows == []


class TestCycleGuard:
    """The graph carries a back-edge to the seed and a leaf three hops out; neither derails a walk."""

    async def test_three_hops_reach_the_leaf_and_the_seed_stays_a_single_row(self, graph_conn: asyncpg.Connection):
        rows = await graph_conn.fetch("SELECT * FROM api.graph_expand($1, $2, $3, NULL, $4)", _COLLECTION, [_ENT_A], 3, 50)

        reached = _node_ids(rows)
        assert _ENT_D in reached, "the leaf three hops out must be reached"
        # The c->a back-edge must not re-expand the seed: it arrives at hop 0 and carries no in-edge.
        seed_rows = [row for row in rows if row["id"] == _ENT_A]
        assert len(seed_rows) == 1
        assert seed_rows[0]["src_id"] is None

    async def test_a_route_to_the_leaf_threads_the_whole_chain(self, graph_conn: asyncpg.Connection):
        rows = await graph_conn.fetch("SELECT * FROM api.graph_paths($1, $2, $3, $4)", _COLLECTION, _ENT_A, _ENT_D, 4)

        rank1 = [row["id"] for row in rows if row["path_rank"] == 1]
        assert rank1[0] == _ENT_A
        assert rank1[-2:] == [_ENT_C, _ENT_D]
        assert len(set(rank1)) == len(rank1), "no node may repeat inside one route"


class TestRelationFilter:
    """The relation filter still narrows the walk it always narrowed."""

    async def test_restricting_to_one_relation_eliminates_the_only_route(self, graph_conn: asyncpg.Connection):
        # A->D needs the c->builds_on->d edge; restricting to `cites` removes it, so no route remains.
        rows = await graph_conn.fetch("SELECT * FROM api.graph_paths($1, $2, $3, $4, $5)", _COLLECTION, _ENT_A, _ENT_D, 4, ["cites"])

        assert rows == []

    async def test_a_routes_score_is_its_length(self, graph_conn: asyncpg.Connection):
        rows = await graph_conn.fetch("SELECT * FROM api.graph_paths($1, $2, $3, $4)", _COLLECTION, _ENT_A, _ENT_C, 4)

        rank1 = [row for row in rows if row["path_rank"] == 1]
        assert len(rank1) == 3  # a -> (b|e) -> c
        assert rank1[0]["score"] == pytest.approx(1.0 / 3.0)  # a two-hop route scores 1 / (1 + 2)
        assert all(row["score"] == rank1[0]["score"] for row in rank1), "a score belongs to a route, not to a row"

    async def test_restricting_to_one_relation_drops_the_others(self, graph_conn: asyncpg.Connection):
        rows = await graph_conn.fetch("SELECT * FROM api.graph_expand($1, $2, $3, $4, $5)", _COLLECTION, [_ENT_A], 2, ["cites"], 50)

        reached = _node_ids(rows)
        assert _ENT_B in reached
        assert _ENT_E not in reached  # reached by `extends` alone
        assert not set(_FAN_OUT) & reached  # reached by `relates_to` alone

    async def test_a_node_reached_only_across_an_excluded_relation_stays_out(self, graph_conn: asyncpg.Connection):
        """`ent.d` sits three hops out behind two `cites` hops, so a `builds_on` walk must not reach it.

        The filter is about the whole route, not its last leg: a walk restricted to one relation may
        travel that relation and no other, whichever backend answers.
        """
        rows = await graph_conn.fetch("SELECT * FROM api.graph_expand($1, $2, $3, $4, $5)", _COLLECTION, [_ENT_A], 3, ["builds_on"], 50)

        assert _node_ids(rows) == {_ENT_A}, "nothing hangs off ent.a by `builds_on`, so only the seed comes back"

    async def test_an_empty_relation_list_filters_no_expansion(self, graph_conn: asyncpg.Connection):
        """Naming no relation is the absence of a filter, whether the argument arrives NULL or empty."""
        empty = await _rows(graph_conn, "api.graph_expand($1, $2, $3, $4, $5)", _COLLECTION, [_ENT_A], 2, [], 50)
        unfiltered = await _rows(graph_conn, "api.graph_expand($1, $2, $3, $4, $5)", _COLLECTION, [_ENT_A], 2, None, 50)

        assert empty == unfiltered
        assert len(empty) > 1, "the fixture's neighbourhood is more than the seed, so this is not two empty answers"

    async def test_an_empty_relation_list_filters_no_route(self, graph_conn: asyncpg.Connection):
        """The same rule on the route search: an empty list leaves every route the caller would get unfiltered."""
        empty = await _rows(graph_conn, "api.graph_paths($1, $2, $3, $4, $5)", _COLLECTION, _ENT_A, _ENT_C, 4, [])
        unfiltered = await _rows(graph_conn, "api.graph_paths($1, $2, $3, $4, $5)", _COLLECTION, _ENT_A, _ENT_C, 4, None)

        assert empty == unfiltered
        assert empty, "a -> (b|e) -> c is a real route, so this is not two empty answers"

    async def test_the_result_carries_the_reconciled_columns(self, graph_conn: asyncpg.Connection):
        expansion = await graph_conn.fetch("SELECT * FROM api.graph_expand($1, $2, $3)", _COLLECTION, [_ENT_A], 1)
        routes = await graph_conn.fetch("SELECT * FROM api.graph_paths($1, $2, $3, $4)", _COLLECTION, _ENT_A, _ENT_C, 4)

        expand_columns = ("id", "type", "title", "status", "superseded", "src_id", "dst_id", "rel", "provenance_claim")
        assert tuple(expansion[0].keys()) == expand_columns
        assert tuple(routes[0].keys()) == (*expand_columns, "path_rank", "score")


class TestAPrivateCollectionIsJudgedByCurrencyToo:
    """A collection kept out of the shared library still answers its own session by currency.

    Privacy decides who may read a collection; currency decides what a reader gets back. The two are
    independent, so the private collection's own session must see the same walk the shared one does:
    a superseded intermediary skipped, and the node behind it out of reach with it. Under Apache AGE
    a node's currency is a vertex property the writer-only projection puts there, and that writer
    runs with no session collection of its own — so the projection has to read currency from a
    relation that is not filtered by who is asking.
    """

    async def test_the_default_walk_stops_at_the_superseded_intermediary(self, private_conn: asyncpg.Connection):
        rows = await private_conn.fetch("SELECT * FROM api.graph_expand($1, ARRAY[$2], 2, NULL, 20)", _PRIVATE, _ENT_PRIVATE_SEED)

        assert [row["id"] for row in rows] == [_ENT_PRIVATE_SEED]

    async def test_no_route_runs_through_the_superseded_intermediary(self, private_conn: asyncpg.Connection):
        rows = await private_conn.fetch(
            "SELECT * FROM api.graph_paths($1, $2, $3, 4, NULL, 10)", _PRIVATE, _ENT_PRIVATE_SEED, _ENT_PRIVATE_BEHIND
        )

        assert rows == []

    async def test_asking_for_superseded_nodes_reaches_the_whole_chain(self, private_conn: asyncpg.Connection):
        rows = await private_conn.fetch(
            "SELECT * FROM api.graph_expand($1, ARRAY[$2], 2, NULL, 20, true)", _PRIVATE, _ENT_PRIVATE_SEED
        )

        assert _node_ids(rows) == {_ENT_PRIVATE_SEED, _ENT_PRIVATE_STALE, _ENT_PRIVATE_BEHIND}

    async def test_an_anonymous_session_still_reads_nothing_of_it(self, anonymous_conn: asyncpg.Connection):
        rows = await anonymous_conn.fetch(
            "SELECT * FROM api.graph_expand($1, ARRAY[$2], 2, NULL, 20, true)", _PRIVATE, _ENT_PRIVATE_SEED
        )

        assert rows == [], "the currency source the projection reads must not widen who may walk the collection"
