"""Inside `read_snapshot()` every read of one engine and collection sees one state of the knowledge base.

One shared collection holds one card. The owner adds a second card between two reads. Every read runs as `kb_reader`.

What this pins:

* two reads in one block see the same cards, though the owner committed a card between them;
* the block's transaction is a read-only repeatable read;
* after `release_snapshot()` the next read opens a new snapshot and sees the commit;
* reads gathered at the same time inside a block share its connection one at a time;
* a read that fails leaves the block able to read again;
* outside a block each read sees the latest commit.
"""

from __future__ import annotations

import asyncio

from typing import TYPE_CHECKING

import asyncpg
import pytest

from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy.pool import NullPool

from engram.retrieval.infrastructure.gateway import ReadOnlySqlGateway, read_snapshot, release_snapshot
from tests.integration.test_migrations import _asyncpg_dsn

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

_COLLECTION = "notes"
_COUNT = "SELECT count(*) AS cards FROM api.cards WHERE src_collection = :collection"
_PAGE = (
    "INSERT INTO kb.pages (collection, id, page_type, slug, title, status, card_text, body_hash, facets)"
    " VALUES ($1, $2, 'entity', $2, $2, 'live', 'text', $2, '{}'::jsonb)"
)


@pytest.fixture
async def admin(migrated: str) -> AsyncIterator[asyncpg.Connection]:
    """An owner connection over a collection holding one card."""
    connection = await asyncpg.connect(_asyncpg_dsn(migrated))
    try:
        await connection.execute(_PAGE, _COLLECTION, "ent.one")
        yield connection
    finally:
        await connection.close()


@pytest.fixture
async def gateway(admin: asyncpg.Connection, reader_url: str) -> AsyncIterator[ReadOnlySqlGateway]:
    """A `kb_reader` gateway reading as the collection."""
    del admin
    engine = create_async_engine(reader_url, poolclass=NullPool)
    try:
        yield ReadOnlySqlGateway(engine, collection=_COLLECTION)
    finally:
        await engine.dispose()


async def _cards(gateway: ReadOnlySqlGateway) -> int:
    """How many cards the collection holds, as the gateway reads it."""
    result = await gateway.rows(_COUNT, {"collection": _COLLECTION})
    return int(str(result.rows[0]["cards"]))


async def test_two_reads_in_one_block_see_one_state(gateway: ReadOnlySqlGateway, admin: asyncpg.Connection):
    async with read_snapshot():
        before = await _cards(gateway)
        await admin.execute(_PAGE, _COLLECTION, "ent.two")
        after = await _cards(gateway)

    assert (before, after) == (1, 1)
    assert await _cards(gateway) == 2


async def test_the_block_reads_in_a_read_only_repeatable_read(gateway: ReadOnlySqlGateway):
    async with read_snapshot():
        result = await gateway.rows(
            "SELECT current_setting('transaction_isolation') AS isolation, current_setting('transaction_read_only') AS read_only"
        )

    assert dict(result.rows[0]) == {"isolation": "repeatable read", "read_only": "on"}


async def test_a_release_lets_the_next_read_see_the_commit(gateway: ReadOnlySqlGateway, admin: asyncpg.Connection):
    async with read_snapshot():
        before = await _cards(gateway)
        await admin.execute(_PAGE, _COLLECTION, "ent.two")
        await release_snapshot()
        after = await _cards(gateway)

    assert (before, after) == (1, 2)


async def test_reads_gathered_in_a_block_share_its_connection(gateway: ReadOnlySqlGateway):
    async with read_snapshot():
        backends = await asyncio.gather(*(gateway.rows("SELECT pg_backend_pid() AS pid") for _ in range(5)))

    assert len({result.rows[0]["pid"] for result in backends}) == 1


async def test_a_failed_read_leaves_the_block_able_to_read(gateway: ReadOnlySqlGateway):
    async with read_snapshot():
        with pytest.raises(SQLAlchemyError):
            await gateway.rows("SELECT 1 / 0")
        cards = await _cards(gateway)

    assert cards == 1


async def test_outside_a_block_each_read_sees_the_latest_commit(gateway: ReadOnlySqlGateway, admin: asyncpg.Connection):
    before = await _cards(gateway)
    await admin.execute(_PAGE, _COLLECTION, "ent.two")

    assert (before, await _cards(gateway)) == (1, 2)
