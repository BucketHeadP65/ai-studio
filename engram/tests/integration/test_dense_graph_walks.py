"""The graph walks visit each node once, so a dense graph answers within a few seconds at every depth.

One shared collection holds thirty pages, each linked to every other: 870 edges, a cycle through every pair and a hub
at every node. Its simple paths number in the hundreds of millions at six hops. Every call runs as `kb_reader` under a
three-second statement timeout.

What this pins:

* an expansion answers at two hops and at six;
* a route search answers at its default depth and at its deepest;
* `api.graph_reach` counts every node a seed reaches and says the walk was complete;
* it gives the hops to a target, and says nothing reaches a target outside the seed's component;
* it stops at its node limit and says the walk was not complete.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import asyncpg
import pytest

from tests.integration.test_migrations import _asyncpg_dsn

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

_COLLECTION = "notes"
_NODES = tuple(f"dense.{number:02d}" for number in range(30))
_EXPAND = "SELECT DISTINCT id FROM api.graph_expand($1, ARRAY[$2], $3, NULL, 50)"
_PATHS = "SELECT DISTINCT path_rank FROM api.graph_paths($1, $2, $3, $4, NULL, 10)"
_REACH = "SELECT * FROM api.graph_reach($1, ARRAY[$2], NULL, $3)"
_STAR = tuple(f"star.{number:05d}" for number in range(10_002))


@pytest.fixture
async def admin(migrated: str) -> AsyncIterator[asyncpg.Connection]:
    """An owner connection over thirty pages that each link to every other."""
    connection = await asyncpg.connect(_asyncpg_dsn(migrated))
    try:
        await connection.executemany(
            "INSERT INTO kb.pages (collection, id, page_type, slug, title, status, card_text, body_hash, facets)"
            " VALUES ($1, $2, 'entity', $2, $2, 'live', $2, $2, '{}'::jsonb)",
            [(_COLLECTION, node) for node in _NODES],
        )
        await connection.executemany(
            "INSERT INTO kb.edges (src_collection, src_id, src_kind, rel, dst_collection, dst_id, dst_kind)"
            " VALUES ($1, $2, 'page', 'links', $1, $3, 'page')",
            [(_COLLECTION, source, target) for source in _NODES for target in _NODES if source != target],
        )
        await connection.execute("SELECT kb.refresh_graph()")
        yield connection
    finally:
        await connection.close()


@pytest.fixture
async def reader(admin: asyncpg.Connection, reader_url: str) -> AsyncIterator[asyncpg.Connection]:
    """A `kb_reader` connection that stops any statement after three seconds."""
    del admin
    connection = await asyncpg.connect(_asyncpg_dsn(reader_url))
    try:
        await connection.execute("SET statement_timeout = '3s'")
        yield connection
    finally:
        await connection.close()


@pytest.mark.parametrize("hops", [2, 6])
async def test_an_expansion_answers_at_every_depth(reader: asyncpg.Connection, hops: int):
    rows = await reader.fetch(_EXPAND, _COLLECTION, _NODES[0], hops)

    assert {row["id"] for row in rows} == set(_NODES)


@pytest.mark.parametrize("max_hops", [4, 8])
async def test_a_route_search_answers_at_every_depth(reader: asyncpg.Connection, max_hops: int):
    rows = await reader.fetch(_PATHS, _COLLECTION, _NODES[0], _NODES[-1], max_hops)

    assert len(rows) == 11


async def test_reach_counts_every_node_and_says_it_was_complete(reader: asyncpg.Connection):
    row = await reader.fetchrow(_REACH, _COLLECTION, _NODES[0], None)

    assert row is not None
    assert dict(row) == {"reached_nodes": 30, "deepest_hop": 1, "target_hop": None, "complete": True}


async def test_reach_gives_the_hops_to_a_target(admin: asyncpg.Connection, reader: asyncpg.Connection):
    await admin.execute(
        "INSERT INTO kb.edges (src_collection, src_id, src_kind, rel, dst_collection, dst_id, dst_kind)"
        " VALUES ($1, $2, 'page', 'links', $1, 'tail.a', 'page'), ($1, 'tail.a', 'page', 'links', $1, 'tail.b', 'page')",
        _COLLECTION,
        _NODES[5],
    )
    await admin.execute("SELECT kb.refresh_graph()")

    row = await reader.fetchrow(_REACH, _COLLECTION, _NODES[0], "tail.b")

    assert row is not None
    assert (row["target_hop"], row["complete"]) == (3, False)


async def test_reach_says_nothing_reaches_a_target_outside_the_component(admin: asyncpg.Connection, reader: asyncpg.Connection):
    await admin.execute(
        "INSERT INTO kb.edges (src_collection, src_id, src_kind, rel, dst_collection, dst_id, dst_kind)"
        " VALUES ($1, 'island.a', 'page', 'links', $1, 'island.b', 'page')",
        _COLLECTION,
    )
    await admin.execute("SELECT kb.refresh_graph()")

    row = await reader.fetchrow(_REACH, _COLLECTION, _NODES[0], "island.b")

    assert row is not None
    assert (row["target_hop"], row["complete"]) == (None, True)


async def test_reach_stops_at_its_node_limit(admin: asyncpg.Connection, reader: asyncpg.Connection):
    await admin.executemany(
        "INSERT INTO kb.edges (src_collection, src_id, src_kind, rel, dst_collection, dst_id, dst_kind)"
        " VALUES ($1, $2, 'page', 'links', $1, $3, 'page')",
        [(_COLLECTION, _STAR[0], target) for target in _STAR[1:]],
    )
    await admin.execute("SELECT kb.refresh_graph()")

    row = await reader.fetchrow(_REACH, _COLLECTION, _STAR[0], None)

    assert row is not None
    assert (row["reached_nodes"], row["complete"]) == (10_002, False)
