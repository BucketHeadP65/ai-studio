r"""The cross-knowledge-base trap, replayed against the Apache AGE backend: a foreign interior node must never surface.

Two collections ("alpha", "beta") both declare a page with the bare id `trap.x` — legal since
`kb.pages`' key is `(collection, id)`. A clean path `alpha/trap.a -> alpha/trap.x ->
alpha/trap.b` stays entirely inside "alpha"; a second, structurally real path routes through the
OTHER collection's same-id vertex: `alpha/trap.a -> beta/trap.x -> alpha/trap.b`. Both routes exist
as ordinary `kb.edges` rows and both get projected into the Apache AGE graph — an endpoint-only
collection check (`WHERE a.collection = 'alpha' AND b.collection = 'alpha'`) would accept the second
route too, since only its interior node is foreign. A scoped `api.graph_expand`/`api.graph_paths`
call must exclude it: this is the mutation the per-hop `UNWIND nodes(p)` filter exists to catch, so
reverting that filter to an endpoint-only check turns every isolation assertion below red while the
existence assertions (proving the trap route is really there, not merely absent) stay green.

Apache AGE is the default local bed (`BED_GRAPH_BACKEND == "age"`); this module skips under the
opt-in ParadeDB/recursive-CTE bed, whose per-hop relational join has no analogous merge-then-filter
risk to replay. The whole module skips when Docker is unavailable (the `migrated` fixture's `pg`
guard).
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import asyncpg
import pytest

from engram.testing.infrastructure.knowledge_base_db import BED_GRAPH_BACKEND
from tests.integration.test_migrations import _asyncpg_dsn

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

# The Apache AGE property graph 0004 projects, byte-identical to test_migrations._AGE_GRAPH_NAME.
_AGE_GRAPH_NAME = "kb_graph"

_ALPHA = "alpha"
_BETA = "beta"

_TRAP_A = "trap.a"
_TRAP_B = "trap.b"
_TRAP_X = "trap.x"

_CLEAN_REL = "clean_hop"
_LEAK_REL = "leak_hop"

# (collection, id, title) — trap.a and trap.b exist only in "alpha"; trap.x exists in BOTH
# collections under the same bare id, the cross-knowledge-base collision the composite `(id, collection)`
# vertex identity must keep distinct.
_PAGES: tuple[tuple[str, str, str], ...] = (
    (_ALPHA, _TRAP_A, "Trap A (alpha)"),
    (_ALPHA, _TRAP_B, "Trap B (alpha)"),
    (_ALPHA, _TRAP_X, "Trap X (alpha's own copy)"),
    (_BETA, _TRAP_X, "Trap X (beta's own copy)"),
)

# (src_collection, src_id, rel, dst_collection, dst_id) — the clean pair stays inside "alpha"
# throughout; the leak pair routes through beta's trap.x while both its own endpoints (trap.a,
# trap.b) stay in "alpha", exactly the endpoint-only check's blind spot.
_EDGES: tuple[tuple[str, str, str, str, str], ...] = (
    (_ALPHA, _TRAP_A, _CLEAN_REL, _ALPHA, _TRAP_X),
    (_ALPHA, _TRAP_X, _CLEAN_REL, _ALPHA, _TRAP_B),
    (_ALPHA, _TRAP_A, _LEAK_REL, _BETA, _TRAP_X),
    (_BETA, _TRAP_X, _LEAK_REL, _ALPHA, _TRAP_B),
)


async def _seed_trap(conn: asyncpg.Connection) -> None:
    """Insert the trap's pages and edges, then project them into the AGE graph.

    Direct SQL inserts, mirroring `tests.integration.test_graph_retrieval._seed`. The explicit
    `SELECT kb.refresh_graph()` call is required under AGE (unlike the recursive-CTE backend,
    which walks `kb.edges` live) — it is the writer-only step the index load step normally
    runs after replacing the edge set.
    """
    await conn.executemany(
        "INSERT INTO kb.pages (collection, id, page_type, slug, title, status, card_text, body_hash)"
        " VALUES ($1, $2, 'entity', $3, $4, 'verified', $4, $3)",
        [(collection, page_id, f"{collection}-{page_id}", title) for collection, page_id, title in _PAGES],
    )
    await conn.executemany(
        "INSERT INTO kb.edges (src_collection, src_id, src_kind, rel, dst_collection, dst_id, dst_kind)"
        " VALUES ($1, $2, 'page', $3, $4, $5, 'page')",
        list(_EDGES),
    )
    await conn.execute("SELECT kb.refresh_graph()")


def _rels(rows: list[asyncpg.Record]) -> set[str]:
    """The distinct non-NULL `rel` values a traversal's rows carry (a seed/start row carries NULL)."""
    return {row["rel"] for row in rows if row["rel"] is not None}


@pytest.fixture
async def trap_conn(migrated: str) -> AsyncIterator[asyncpg.Connection]:
    """A fresh, head-migrated database's connection, seeded with the cross-knowledge-base trap.

    Skips under the ParadeDB/recursive-CTE bed — this replay is specific to the Apache AGE
    projection's identity and per-hop filtering, which the recursive-CTE backend has no analogue
    for (its per-hop join already scopes by collection relationally, with no merge to un-merge).
    """
    if BED_GRAPH_BACKEND != "age":
        pytest.skip(f"bed graph backend is {BED_GRAPH_BACKEND!r}, not age — the AGE composite-identity trap needs the AGE bed")

    connection = await asyncpg.connect(_asyncpg_dsn(migrated))
    try:
        await _seed_trap(connection)
        yield connection
    finally:
        await connection.close()


class TestTrapReallyExists:
    """The trap's own topology is real: both same-id vertices and both leak edges are projected.

    Reads the AGE label tables directly through `ag_catalog.agtype_access_operator` — the same
    property-extraction idiom the traversal builder's own generated SQL uses for its node-view join
    — rather than `cypher()`: AGE 1.6.0's `cypher()` accepts a bound parameter map only as a literal
    third argument (not a column expression), which a plain `SELECT` cannot supply outside a
    `plpgsql` body. Without this class, an isolation assertion that merely finds nothing would be
    indistinguishable from a vacuous pass over an empty graph.
    """

    async def test_both_collections_own_copy_of_the_shared_id_are_distinct_vertices(self, trap_conn: asyncpg.Connection):
        count = await trap_conn.fetchval(
            f'SELECT count(*) FROM {_AGE_GRAPH_NAME}."Node"'
            " WHERE ag_catalog.agtype_access_operator(properties, '\"id\"'::ag_catalog.agtype)::text = $1",
            _TRAP_X,
        )

        assert count == 2, f"expected trap.x projected as two distinct vertices (alpha's and beta's), got {count}"

    async def test_both_leak_edges_are_projected(self, trap_conn: asyncpg.Connection):
        count = await trap_conn.fetchval(
            f'SELECT count(*) FROM {_AGE_GRAPH_NAME}."REL"'
            " WHERE ag_catalog.agtype_access_operator(properties, '\"rel\"'::ag_catalog.agtype)::text = $1",
            _LEAK_REL,
        )

        assert count == 2, f"expected both leak_hop edges projected (isolation must come from query-time filtering, not missing data), got {count}"


class TestScopedTraversalNeverSurfacesTheForeignRoute:
    """`api.graph_expand`/`api.graph_paths`, scoped to "alpha", must never carry the leak route.

    Reverting the per-hop `UNWIND nodes(p)` filter to an endpoint-only check would let every
    assertion here see `leak_hop` — the mutation these tests are built to catch.
    """

    async def test_graph_paths_finds_only_the_clean_path(self, trap_conn: asyncpg.Connection):
        rows = await trap_conn.fetch("SELECT * FROM api.graph_paths($1, $2, $3, $4)", _ALPHA, _TRAP_A, _TRAP_B, 4)

        assert rows, "expected the clean in-collection path (alpha/trap.a -> alpha/trap.x -> alpha/trap.b) to be found"
        assert _LEAK_REL not in _rels(rows), f"the leak path routing through beta's trap.x surfaced in an alpha-scoped result: {rows}"
        rank1 = [row["id"] for row in rows if row["path_rank"] == 1]
        assert rank1 == [_TRAP_A, _TRAP_X, _TRAP_B], f"expected the clean path's node order, got {rank1}"

    async def test_graph_expand_reaches_only_the_clean_route(self, trap_conn: asyncpg.Connection):
        rows = await trap_conn.fetch("SELECT * FROM api.graph_expand($1, $2, $3)", _ALPHA, [_TRAP_A], 2)

        assert {row["id"] for row in rows} == {_TRAP_A, _TRAP_X, _TRAP_B}, f"expected only the clean route's three nodes, got {rows}"
        assert _rels(rows) == {_CLEAN_REL}, f"expected only clean_hop edges reached, got {_rels(rows)}"

    async def test_graph_expand_one_hop_never_reaches_betas_copy_of_the_shared_id_via_the_leak_edge(self, trap_conn: asyncpg.Connection):
        # a→x is ambiguous at hop 1 (a clean edge into alpha's trap.x, a leak edge into beta's) —
        # scoped to alpha, only the clean hop may survive.
        rows = await trap_conn.fetch("SELECT * FROM api.graph_expand($1, $2, $3)", _ALPHA, [_TRAP_A], 1)

        reached = {row["id"] for row in rows} - {_TRAP_A}
        assert reached == {_TRAP_X}, f"expected exactly one hop-1 neighbour (alpha's own trap.x), got {reached}"
        assert _rels(rows) == {_CLEAN_REL}, f"expected the reaching edge to be clean_hop, not leak_hop; got {_rels(rows)}"


class TestTheScopedAnswerIsTheSameEveryTime:
    """A convergent graph with a foreign same-id vertex still answers byte-identically on every run.

    The trap graph is the hard case for ordering: two routes into `trap.b`, one of them foreign, so
    a walk that merged them or ordered them by arrival would drift between runs.
    """

    async def test_five_scoped_expansions_return_byte_identical_ordered_rows(self, trap_conn: asyncpg.Connection):
        runs = [
            [
                str(record["row"])
                for record in await trap_conn.fetch(
                    "SELECT row_to_json(t) AS row FROM api.graph_expand($1, $2, $3) t", _ALPHA, [_TRAP_A], 2
                )
            ]
            for _ in range(5)
        ]

        assert all(run == runs[0] for run in runs)
        assert runs[0], "expected the scoped expansion to return rows"
