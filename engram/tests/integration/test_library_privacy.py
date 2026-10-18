r"""Privacy at the api layer: a collection declaring `private: true` is served to its own reader role only.

Two collections are seeded into one head-migrated database. "shared-kb" indexes with the default
`private` flag, "private-kb" indexes with `private: true` on its fingerprint switches, the shape
`engram.indexing.domain.switches.RetrievalSwitches` writes. Every assertion below runs either as the
anonymous `kb_reader` serving role, the way the doors read, or as the private collection's own reader
role, which the indexer provisions. Setting `app.collection` grants nothing.

What this pins:

* an anonymous session (no `app.collection`) sees no private row anywhere on the api surface —
  cards, sections, claims, facets, raw chunks, page types, the aggregates that only count, hybrid
  search, fetch, the envelope, the graph walk, and the switches door itself;
* an invisible collection reads exactly like a collection that does not exist: an empty result of
  the same shape, never a refusal;
* both endpoints of an edge must be visible, so a private collection never surfaces even as a
  neighbour id on a shared collection's own graph;
* the private collection's own reader role reads everything, unchanged;
* a reader that writes `app.collection` itself, in the same statement it reads, still sees nothing private;
* `ReadOnlySqlGateway` given `PrivateReaders` reads a private collection through its own role, and one
  without them reads only the shared collection.
"""

from __future__ import annotations

import json

from typing import TYPE_CHECKING

import asyncpg
import pytest

from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy.pool import NullPool

from engram.retrieval.infrastructure.gateway import ReadOnlySqlGateway
from engram.retrieval.infrastructure.reader_roles import PrivateReaders
from engram.testing.infrastructure.knowledge_base_db import TEST_READER_SECRET, provision_private_reader
from tests.integration.test_migrations import _asyncpg_dsn

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

_SHARED = "shared-kb"
_PRIVATE = "private-kb"

_SHARED_PAGE = "ent.shared"
_PRIVATE_PAGE = "ent.secret"

# The one word both collections' card text carries, so a single hybrid search ranks over both and
# the anonymous session's missing hit is a filtered row, never a query that matched nothing.
_SEARCH_TERM = "chromatophore"

_PAGES: tuple[tuple[str, str, str], ...] = (
    (_SHARED, _SHARED_PAGE, "Shared Entity"),
    (_PRIVATE, _PRIVATE_PAGE, "Secret Entity"),
)

# (src_collection, src_id, rel, dst_collection, dst_id) — one edge inside the shared collection, and
# one crossing from it into the private one: the crossing edge is the D4 case, visible at its source
# and invisible at its destination.
_EDGES: tuple[tuple[str, str, str, str, str], ...] = (
    (_SHARED, _SHARED_PAGE, "relates_to", _SHARED, _SHARED_PAGE),
    (_SHARED, _SHARED_PAGE, "relates_to", _PRIVATE, _PRIVATE_PAGE),
    (_PRIVATE, _PRIVATE_PAGE, "relates_to", _PRIVATE, _PRIVATE_PAGE),
)

_SWITCHES = '{"embeddings": false, "sql": false, "graph": false, "private": %s}'


async def _seed(conn: asyncpg.Connection) -> None:
    """Write both collections' pages, sections, claims, chunks, vocabulary and fingerprints."""
    await conn.executemany(
        "INSERT INTO kb.pages (collection, id, page_type, slug, title, status, card_text, body_hash, facets)"
        " VALUES ($1, $2, 'entity', $3, $4, 'live', $5, $3, '{\"tasks\": [\"retrieval\"]}'::jsonb)",
        [(collection, page_id, f"{collection}-{page_id}", title, f"{title} {_SEARCH_TERM}") for collection, page_id, title in _PAGES],
    )
    await conn.executemany(
        "INSERT INTO kb.sections (collection, id, page_id, ord, canon, path, heading, body, body_hash, tokens)"
        " VALUES ($1, $2, $3, 0, 'definition', 'Definition'::ltree, 'Definition', $4, 'h', 3)",
        [(collection, f"{page_id}#s1", page_id, f"{title} section body {_SEARCH_TERM}") for collection, page_id, title in _PAGES],
    )
    await conn.executemany(
        "INSERT INTO kb.claims (collection, id, page_id, block_id, kind, text, status, subject_collection, subject_id)"
        " VALUES ($1, $2, $3, $2, 'support', $4, 'active', $1, $3)",
        [(collection, f"{page_id}#c1", page_id, f"{title} claims something about {_SEARCH_TERM}") for collection, page_id, title in _PAGES],
    )
    await conn.executemany(
        "INSERT INTO kb.sources_raw (collection, source_id, title, source_hash, provider, provider_key, meta)"
        " VALUES ($1, $2, $3, 'h', 'intake', $2, '{}'::jsonb)",
        [(collection, f"src.{collection}", f"{collection} mirror") for collection, _page_id, _title in _PAGES],
    )
    await conn.executemany(
        "INSERT INTO kb.raw_chunks (collection, id, source_id, seq, section_path, heading, body)"
        " VALUES ($1, $2, $3, 0, 'Body'::ltree, 'Body', $4)",
        [(collection, f"raw.{collection}", f"src.{collection}", f"{collection} raw body") for collection, _page_id, _title in _PAGES],
    )
    await conn.executemany(
        "INSERT INTO kb.vocab (collection, vocab, value, definition, meta)"
        " VALUES ($1, 'schema_guide', $2, 'a surface', '{}'::jsonb)",
        [(collection, f"api.cards@{collection}") for collection, _page_id, _title in _PAGES],
    )
    await conn.executemany(
        "INSERT INTO kb.page_type_contracts (collection, page_type, contract) VALUES ($1, 'entity', '{}'::jsonb)",
        [(collection,) for collection, _page_id, _title in _PAGES],
    )
    await conn.executemany(
        "INSERT INTO kb.edges (src_collection, src_id, src_kind, rel, dst_collection, dst_id, dst_kind)"
        " VALUES ($1, $2, 'page', $3, $4, $5, 'page')",
        list(_EDGES),
    )
    await conn.executemany(
        "INSERT INTO kb.index_fingerprint (collection, indexer_version, spec_hash, vocabulary_hash, embed_model, switches)"
        " VALUES ($1, '1', 's', 'r', 'm', $2::jsonb)",
        [(_SHARED, _SWITCHES % "false"), (_PRIVATE, _SWITCHES % "true")],
    )
    # Writer-only step the index load stage runs: a no-op under the recursive-CTE backend, the
    # real projection rebuild under Apache AGE, which the graph assertions walk.
    await conn.execute("SELECT kb.refresh_graph()")


@pytest.fixture
async def seeded(migrated: str) -> str:
    """The head-migrated database with both collections seeded; answers its admin URL."""
    connection = await asyncpg.connect(_asyncpg_dsn(migrated))
    try:
        await _seed(connection)
    finally:
        await connection.close()
    return migrated


@pytest.fixture
async def anonymous(seeded: str, reader_url: str) -> AsyncIterator[asyncpg.Connection]:
    """A `kb_reader` connection that never announces a collection."""
    del seeded
    connection = await asyncpg.connect(_asyncpg_dsn(reader_url))
    try:
        yield connection
    finally:
        await connection.close()


@pytest.fixture
async def own_session(seeded: str) -> AsyncIterator[asyncpg.Connection]:
    """A connection logged in as the private collection's own reader role."""
    connection = await asyncpg.connect(_asyncpg_dsn(await provision_private_reader(seeded, _PRIVATE)))
    try:
        yield connection
    finally:
        await connection.close()


class TestTheSeedIsReal:
    """The private rows exist: every absence below is a filtered row, never an empty database."""

    async def test_both_collections_carry_a_page(self, seeded: str):
        connection = await asyncpg.connect(_asyncpg_dsn(seeded))
        try:
            collections = await connection.fetch("SELECT collection FROM kb.pages ORDER BY collection")
        finally:
            await connection.close()

        assert [row["collection"] for row in collections] == [_PRIVATE, _SHARED]


class TestAnonymousSession:
    """With no `app.collection`, the whole api surface serves the shared collection only."""

    async def test_the_predicate_answers_a_plain_boolean(self, anonymous: asyncpg.Connection):
        visible = await anonymous.fetch(
            "SELECT api.visible_collection($1) AS private, api.visible_collection($2) AS shared,"
            " api.visible_collection('never-indexed') AS unknown",
            _PRIVATE,
            _SHARED,
        )

        assert dict(visible[0]) == {"private": False, "shared": True, "unknown": True}

    async def test_cards_serve_only_the_shared_collection(self, anonymous: asyncpg.Connection):
        rows = await anonymous.fetch("SELECT id, src_collection FROM api.cards ORDER BY id")

        assert [row["src_collection"] for row in rows] == [_SHARED]

    @pytest.mark.parametrize(
        ("relation", "column"),
        [
            ("api.sections", "collection"),
            ("api.claims", "collection"),
            ("api.facets", "collection"),
            ("api.raw_chunks", "collection"),
            ("api.page_types", "collection"),
            ("api.schema_guide", "collection"),
        ],
    )
    async def test_no_relation_serves_a_private_row(self, anonymous: asyncpg.Connection, relation: str, column: str):
        count = await anonymous.fetchval(f"SELECT count(*) FROM {relation} WHERE {column} = $1", _PRIVATE)  # noqa: S608 (parametrized relation name, not user input)

        assert count == 0, f"{relation} served a {_PRIVATE} row to a session that is not {_PRIVATE}"

    async def test_fetch_answers_the_nonexistent_shape(self, anonymous: asyncpg.Connection):
        private = await anonymous.fetch("SELECT * FROM api.fetch($1, $2)", _PRIVATE, _PRIVATE_PAGE)
        shared = await anonymous.fetch("SELECT * FROM api.fetch($1, $2)", _SHARED, _SHARED_PAGE)

        assert private == []
        assert len(shared) == 1

    async def test_the_envelope_is_empty_for_a_private_collection(self, anonymous: asyncpg.Connection):
        pages = await anonymous.fetch("SELECT * FROM api.envelope_collection($1)", _PRIVATE)
        page_types = await anonymous.fetch("SELECT * FROM api.envelope_collection_page_types($1, ARRAY[]::text[])", _PRIVATE)

        assert pages == []
        assert page_types == []

    async def test_the_switches_door_answers_an_empty_object(self, anonymous: asyncpg.Connection):
        private = await anonymous.fetchval("SELECT api.retrieval_switches($1)", _PRIVATE)
        shared = await anonymous.fetchval("SELECT api.retrieval_switches($1)", _SHARED)

        assert private == "{}"
        assert isinstance(shared, str)
        assert '"private": false' in shared

    async def test_search_ranks_no_private_hit(self, anonymous: asyncpg.Connection):
        hits = await anonymous.fetch(
            "SELECT id FROM api.search($1, 20, NULL, NULL, '{}'::jsonb, NULL, true)",
            _SEARCH_TERM,
        )

        ids = {row["id"] for row in hits}
        assert _SHARED_PAGE in ids, "the shared page should still rank, or this assertion is vacuous"
        assert _PRIVATE_PAGE not in ids

    async def test_the_graph_walk_answers_nothing_for_a_private_collection(self, anonymous: asyncpg.Connection):
        expand = await anonymous.fetch("SELECT * FROM api.graph_expand($1, ARRAY[$2], 2, NULL, 20)", _PRIVATE, _PRIVATE_PAGE)
        paths = await anonymous.fetch("SELECT * FROM api.graph_paths($1, $2, $2, 4, NULL, 10)", _PRIVATE, _PRIVATE_PAGE)

        assert expand == []
        assert paths == []


class TestEdgeEndpoints:
    """Both endpoints must be visible: a private neighbour never surfaces, not even as an id."""

    async def test_edges_crossing_into_the_private_collection_vanish(self, anonymous: asyncpg.Connection):
        rows = await anonymous.fetch("SELECT src_collection, dst_collection FROM api.edges")

        assert [(row["src_collection"], row["dst_collection"]) for row in rows] == [(_SHARED, _SHARED)]

    async def test_the_shared_walk_never_reaches_a_private_neighbour(self, anonymous: asyncpg.Connection):
        reached = await anonymous.fetch("SELECT id, dst_id FROM api.graph_expand($1, ARRAY[$2], 2, NULL, 20)", _SHARED, _SHARED_PAGE)

        assert all(row["id"] != _PRIVATE_PAGE and row["dst_id"] != _PRIVATE_PAGE for row in reached), (
            "a walk inside the shared collection reached the private collection's page"
        )

    async def test_both_edges_are_visible_to_the_private_collection(self, own_session: asyncpg.Connection):
        count = await own_session.fetchval("SELECT count(*) FROM api.edges")

        assert count == len(_EDGES), "the private collection's own session should see every edge it takes part in"


class TestOwnSession:
    """Logged in as its own reader role, the private collection reads exactly as it always did."""

    async def test_cards_serve_both_collections(self, own_session: asyncpg.Connection):
        rows = await own_session.fetch("SELECT src_collection FROM api.cards ORDER BY src_collection")

        assert [row["src_collection"] for row in rows] == [_PRIVATE, _SHARED]

    async def test_fetch_reads_its_own_page(self, own_session: asyncpg.Connection):
        rows = await own_session.fetch("SELECT id FROM api.fetch($1, $2)", _PRIVATE, _PRIVATE_PAGE)

        assert [row["id"] for row in rows] == [_PRIVATE_PAGE]

    async def test_the_envelope_lists_its_own_pages(self, own_session: asyncpg.Connection):
        pages = await own_session.fetch("SELECT id FROM api.envelope_collection($1)", _PRIVATE)

        assert [row["id"] for row in pages] == [_PRIVATE_PAGE]

    async def test_the_switches_door_serves_its_own_declaration(self, own_session: asyncpg.Connection):
        switches = await own_session.fetchval("SELECT api.retrieval_switches($1)", _PRIVATE)

        assert isinstance(switches, str)
        assert '"private": true' in switches

    async def test_search_ranks_its_own_page(self, own_session: asyncpg.Connection):
        hits = await own_session.fetch(
            "SELECT id, folded_units FROM api.search($1, 20, NULL, NULL, '{}'::jsonb, $2, true)", _SEARCH_TERM, _PRIVATE
        )

        folded = {unit["id"] for row in hits for unit in json.loads(row["folded_units"])}
        assert {row["id"] for row in hits} | folded == {_PRIVATE_PAGE, f"{_PRIVATE_PAGE}#s1", f"{_PRIVATE_PAGE}#c1"}

    async def test_the_graph_walk_runs_inside_its_own_collection(self, own_session: asyncpg.Connection):
        reached = await own_session.fetch("SELECT id FROM api.graph_expand($1, ARRAY[$2], 2, NULL, 20)", _PRIVATE, _PRIVATE_PAGE)

        assert [row["id"] for row in reached] == [_PRIVATE_PAGE]


class TestGatewayIdentity:
    """`ReadOnlySqlGateway` reads a private knowledge base through its own role, and only when given `PrivateReaders`."""

    async def test_an_anonymous_gateway_reads_only_the_shared_collection(self, seeded: str, reader_url: str):
        del seeded
        engine = create_async_engine(reader_url, poolclass=NullPool)
        try:
            result = await ReadOnlySqlGateway(engine).rows("SELECT src_collection FROM api.cards ORDER BY src_collection")
        finally:
            await engine.dispose()

        assert [row["src_collection"] for row in result.rows] == [_SHARED]

    async def test_a_gateway_opened_for_the_private_collection_reads_it_through_its_role(self, seeded: str, reader_url: str):
        await provision_private_reader(seeded, _PRIVATE)
        engine = create_async_engine(reader_url, poolclass=NullPool)
        readers = PrivateReaders(reader_url, TEST_READER_SECRET)
        try:
            gateway = ReadOnlySqlGateway(engine, collection=_PRIVATE, private_readers=readers)
            result = await gateway.rows("SELECT src_collection FROM api.cards ORDER BY src_collection")
        finally:
            await readers.dispose()
            await engine.dispose()

        assert [row["src_collection"] for row in result.rows] == [_PRIVATE, _SHARED]

    async def test_announcing_the_private_collection_without_its_role_reads_only_the_shared_one(self, seeded: str, reader_url: str):
        await provision_private_reader(seeded, _PRIVATE)
        engine = create_async_engine(reader_url, poolclass=NullPool)
        try:
            result = await ReadOnlySqlGateway(engine, collection=_PRIVATE).rows("SELECT src_collection FROM api.cards")
        finally:
            await engine.dispose()

        assert [row["src_collection"] for row in result.rows] == [_SHARED]

    async def test_a_reader_with_no_secret_reads_the_private_collection_as_empty(self, seeded: str, reader_url: str):
        await provision_private_reader(seeded, _PRIVATE)
        engine = create_async_engine(reader_url, poolclass=NullPool)
        readers = PrivateReaders(reader_url, "")
        try:
            gateway = ReadOnlySqlGateway(engine, collection=_PRIVATE, private_readers=readers)
            result = await gateway.rows("SELECT src_collection FROM api.cards")
        finally:
            await readers.dispose()
            await engine.dispose()

        assert [row["src_collection"] for row in result.rows] == [_SHARED]

    async def test_for_collection_carries_the_identity_onto_a_new_gateway(self, seeded: str, reader_url: str):
        await provision_private_reader(seeded, _PRIVATE)
        engine = create_async_engine(reader_url, poolclass=NullPool)
        readers = PrivateReaders(reader_url, TEST_READER_SECRET)
        try:
            anonymous = ReadOnlySqlGateway(engine, private_readers=readers)
            named = anonymous.for_collection(_PRIVATE)
            before = await anonymous.rows("SELECT src_collection FROM api.cards")
            after = await named.rows("SELECT src_collection FROM api.cards")
        finally:
            await readers.dispose()
            await engine.dispose()

        assert len(before.rows) == 1
        assert len(after.rows) == len(_PAGES)

    async def test_a_shared_collection_reads_through_the_anonymous_engine(self, seeded: str, reader_url: str):
        await provision_private_reader(seeded, _PRIVATE)
        engine = create_async_engine(reader_url, poolclass=NullPool)
        readers = PrivateReaders(reader_url, TEST_READER_SECRET)
        try:
            gateway = ReadOnlySqlGateway(engine, collection=_SHARED, private_readers=readers)
            role = await gateway.rows("SELECT session_user AS role")
        finally:
            await readers.dispose()
            await engine.dispose()

        assert [row["role"] for row in role.rows] == ["kb_reader"]


class TestTheSettingIsNotTheBoundary:
    """A reader that writes `app.collection` itself still never reads another session's private collection."""

    async def test_one_statement_that_claims_the_private_collection_reads_no_private_card(self, anonymous: asyncpg.Connection):
        rows = await anonymous.fetch(
            "WITH claimed AS MATERIALIZED (SELECT set_config('app.collection', $1, true) AS collection)"
            " SELECT cards.id FROM claimed CROSS JOIN LATERAL"
            " (SELECT c.id FROM api.cards c WHERE claimed.collection IS NOT NULL) AS cards",
            _PRIVATE,
        )

        ids = {row["id"] for row in rows}
        assert _SHARED_PAGE in ids, "the shared card should still read, or this assertion is vacuous"
        assert _PRIVATE_PAGE not in ids

    async def test_one_statement_that_claims_the_private_collection_ranks_no_private_hit(self, anonymous: asyncpg.Connection):
        rows = await anonymous.fetch(
            "WITH claimed AS MATERIALIZED (SELECT set_config('app.collection', $1, true) AS collection)"
            " SELECT hits.id FROM claimed CROSS JOIN LATERAL"
            " api.search($2, 20, NULL, NULL, '{}'::jsonb, claimed.collection, true) AS hits",
            _PRIVATE,
            _SEARCH_TERM,
        )

        assert _PRIVATE_PAGE not in {row["id"] for row in rows}
