"""Integration test: `EngramMemoryStore` over the real vendor authority and the engram projection.

The full write stack, live: an asyncpg pool backs the vendor `PostgresMemoryStore` (migration
0031's `public.agent_memory*` tables are the compare-and-set authority), `embedded_authoring` projects
every write through engram's own indexing pipeline into `api.cards`, and the factory's own
`_CardRows` reads the collection's live cards back. The whole tier skips when Docker is
unavailable (the `migrated` fixture raises `pytest.skip`).
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import asyncpg
import pytest

from pydantic_ai_harness.memory import MemoryConflictError, PostgresMemoryStore, PostgresPool

from engram.agent_memory.contracts import EngramMemoryStore, agent_memory_schema, embedded_memory_store
from engram.agent_memory.infrastructure.postgres import _CardRows, _open_pool
from engram.retrieval.infrastructure.embedded import embedded_authoring
from engram.testing.fakes.embedding import FakeEmbedding, FakeQueryEmbedding
from tests.integration.test_migrations import _asyncpg_dsn

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

_COLLECTION = "agent-memory"
_SCOPE = "global/main"
_PATH = f"{_SCOPE}/world/postgres-port.md"
_PAGE_ID = "mem.global.main.world.postgres-port"
_NOTE = "# Local Postgres port\nAnswers on 5433.\n"


@pytest.fixture
async def pool(migrated: str) -> AsyncIterator[asyncpg.Pool]:
    """A real asyncpg pool over the freshly-migrated database — the vendor store's own pool type.

    `search_path` is pinned to `public` explicitly: the vendor store's `_ensure_schema` issues its
    DDL unqualified, resolving against whatever schema the connection's search path names first.
    The `migrated` fixture's admin role happens to be named `memory`, and a schema literally named
    `memory` exists (the knowledge base tables), so an unpinned `"$user", public` search path lets `$user`
    resolve to that schema first — a same-named-role collision specific to this test bed, not a
    fact about the production writer role migration `0031` documents its assumption against.
    """
    created = await asyncpg.create_pool(_asyncpg_dsn(migrated), server_settings={"search_path": "public"})
    try:
        yield created
    finally:
        await created.close()


@pytest.fixture
async def memory(migrated: str, pool: asyncpg.Pool) -> AsyncIterator[EngramMemoryStore]:
    """The composed stack: vendor authority + engram projection, wired the way production composes it."""
    # asyncpg's `Pool.acquire()` accepts an optional `timeout` kwarg the vendor's structural
    # `PostgresPool` protocol doesn't declare, so pyright can't verify the assignment by shape
    # alone; the runtime-checkable narrowing here proves what's actually true at runtime.
    assert isinstance(pool, PostgresPool)
    inner = PostgresMemoryStore(pool)
    cards = _CardRows(pool, collection=_COLLECTION)
    async with embedded_authoring(migrated, spec=agent_memory_schema(), embedder=FakeEmbedding()) as ingest:
        yield EngramMemoryStore(inner, ingest, cards, collection=_COLLECTION)


async def test_write_lands_a_row_in_the_authority_and_a_live_card(memory: EngramMemoryStore, pool: asyncpg.Pool) -> None:
    written = await memory.write(_PATH, _NOTE, expected_version=None)
    assert written.version is not None

    async with pool.acquire() as connection:
        authority_row = await connection.fetchrow("SELECT content FROM public.agent_memory WHERE path = $1", _PATH)
        card_row = await connection.fetchrow("SELECT status FROM api.cards WHERE id = $1", _PAGE_ID)

    assert authority_row is not None
    assert authority_row["content"] == _NOTE
    assert card_row is not None
    assert card_row["status"] == "live"


async def test_a_repeat_write_updates_the_authority_and_projects_one_live_card(memory: EngramMemoryStore, pool: asyncpg.Pool) -> None:
    written = await memory.write(_PATH, _NOTE, expected_version=None)
    updated_note = "# Local Postgres port, revised\nNow answers on 5544.\n"

    rewritten = await memory.write(_PATH, updated_note, expected_version=written.version)

    async with pool.acquire() as connection:
        authority_row = await connection.fetchrow("SELECT content FROM public.agent_memory WHERE path = $1", _PATH)
        live_cards = await connection.fetch(
            "SELECT title, summary FROM api.cards WHERE id = $1 AND status = 'live' AND NOT superseded", _PAGE_ID
        )

    assert rewritten.version is not None
    assert authority_row is not None
    assert authority_row["content"] == updated_note
    assert len(live_cards) == 1
    assert live_cards[0]["title"] == "Local Postgres port, revised"
    assert live_cards[0]["summary"] == "Now answers on 5544."


async def test_a_stale_expected_version_raises_memory_conflict(memory: EngramMemoryStore, pool: asyncpg.Pool) -> None:
    await memory.write(_PATH, _NOTE, expected_version=None)

    with pytest.raises(MemoryConflictError):
        await memory.write(_PATH, _NOTE, expected_version="stale-version")

    async with pool.acquire() as connection:
        card_row = await connection.fetchrow("SELECT title, summary FROM api.cards WHERE id = $1", _PAGE_ID)

    assert card_row is not None
    assert card_row["title"] == "Local Postgres port"
    assert card_row["summary"] == "Answers on 5433."


async def test_delete_flips_the_card_to_retired(memory: EngramMemoryStore, pool: asyncpg.Pool) -> None:
    written = await memory.write(_PATH, _NOTE, expected_version=None)
    await memory.delete(_PATH, expected_version=written.version)

    async with pool.acquire() as connection:
        card_row = await connection.fetchrow("SELECT status FROM api.cards WHERE id = $1", _PAGE_ID)

    assert card_row is not None
    assert card_row["status"] == "retired"


async def test_read_index_lists_exactly_the_live_note(memory: EngramMemoryStore) -> None:
    await memory.write(_PATH, _NOTE, expected_version=None)
    other_path = f"{_SCOPE}/world/other-fact.md"
    written_other = await memory.write(other_path, "# Another fact\nDetail.\n", expected_version=None)
    await memory.delete(other_path, expected_version=written_other.version)

    index = await memory.read(f"{_SCOPE}/MEMORY.md", max_chars=10_000)

    assert index is not None
    assert "postgres-port.md" in index.content
    assert "other-fact.md" not in index.content


async def test_open_pool_sizes_the_real_asyncpg_pool_to_the_given_min_and_max(migrated: str) -> None:
    """The exact seam `embedded_memory_store`'s `pool_min_size`/`pool_max_size` thread through."""
    pool = await _open_pool(migrated, min_size=1, max_size=3)
    try:
        assert pool.get_min_size() == 1
        assert pool.get_max_size() == 3
    finally:
        await pool.close()


async def test_the_factory_composes_a_store_that_finds_a_written_note_hybrid_and_lexical(migrated: str) -> None:
    """`embedded_memory_store` end to end: write once, then find it both ways `search` can be wired.

    Two more store instances (rather than the one just written through) so each opens its own pool
    with the `embed_query` this test wants — the factory's contract is that either composition
    reads back whatever the other already wrote to the same collection. The ranked search finds the
    note under its scope, the lexical one under its category folder, the only files it reads.
    """
    async with embedded_memory_store(migrated, embedder=FakeEmbedding(), collection=_COLLECTION) as writer:
        await writer.write(_PATH, _NOTE, expected_version=None)

    async with embedded_memory_store(
        migrated, embedder=FakeEmbedding(), embed_query=FakeQueryEmbedding(), collection=_COLLECTION
    ) as hybrid:
        hybrid_result = await hybrid.search(f"{_SCOPE}/", "postgres", limit=5, max_files=20, max_chars=2_000, max_file_chars=8_000)

    async with embedded_memory_store(migrated, embedder=FakeEmbedding(), collection=_COLLECTION) as lexical:
        lexical_result = await lexical.search(
            f"{_SCOPE}/world/", "postgres", limit=5, max_files=20, max_chars=2_000, max_file_chars=8_000
        )

    assert any(match.path == _PATH for match in hybrid_result.matches)
    assert any(match.path == _PATH for match in lexical_result.matches)
