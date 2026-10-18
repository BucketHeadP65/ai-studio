"""The search tool leaves out a knowledge base's own `meta.*` pages before it cuts to `k`, and keeps raw hits.

One shared collection holds three `meta.*` cards and three ordinary cards that carry the same word, the meta cards
carrying it more often so they rank first, and one raw chunk carrying another word. Every search runs through
`SearchHandler` as `kb_reader`, keyword only.

What this pins:

* a search for `k` rows answers `k` ordinary hits even when meta pages outrank them;
* `run_sql` still reads the meta pages, which it returns on purpose;
* a raw chunk, whose hit names no page, reaches the search tool.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import asyncpg
import pytest

from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy.pool import NullPool

from engram.retrieval.domain import Search
from engram.retrieval.infrastructure.gateway import ReadOnlySqlGateway
from engram.retrieval.queries import SearchHandler
from engram.testing.fakes.embedding import FakeEmbedding
from tests.integration.test_migrations import _asyncpg_dsn

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

_COLLECTION = "notes"
_WORD = "heliotrope"
_RAW_WORD = "astrolabe"

_META = ("meta.about@notes", "meta.purpose", "meta.schema")
_ORDINARY = ("ent.one", "ent.two", "ent.three")


@pytest.fixture
async def gateway(migrated: str, reader_url: str) -> AsyncIterator[ReadOnlySqlGateway]:
    """A `kb_reader` gateway over the seeded collection."""
    admin = await asyncpg.connect(_asyncpg_dsn(migrated))
    try:
        await admin.executemany(
            "INSERT INTO kb.pages (collection, id, page_type, slug, title, status, card_text, body_hash, facets)"
            " VALUES ($1, $2, 'entity', $2, $2, 'live', $3, $2, '{}'::jsonb)",
            [(_COLLECTION, page_id, f"{_WORD} {_WORD} {_WORD} {_WORD}") for page_id in _META]
            + [(_COLLECTION, page_id, f"{_WORD} and other words") for page_id in _ORDINARY],
        )
        await admin.execute(
            "INSERT INTO kb.sources_raw (collection, source_id, title, source_hash, provider, provider_key, meta)"
            " VALUES ($1, 'src.one', 'One mirror', 'h', 'intake', 'src.one', '{}'::jsonb)",
            _COLLECTION,
        )
        await admin.execute(
            "INSERT INTO kb.raw_chunks (collection, id, source_id, seq, section_path, heading, body)"
            " VALUES ($1, 'raw.one', 'src.one', 0, 'Body'::ltree, 'Body', $2)",
            _COLLECTION,
            f"a passage about the {_RAW_WORD}",
        )
    finally:
        await admin.close()
    engine = create_async_engine(reader_url, poolclass=NullPool)
    try:
        yield ReadOnlySqlGateway(engine, collection=_COLLECTION)
    finally:
        await engine.dispose()


async def test_a_search_for_k_rows_answers_k_ordinary_hits(gateway: ReadOnlySqlGateway):
    result = await SearchHandler(gateway, FakeEmbedding())(Search(query=_WORD, k=3, collection=_COLLECTION, hybrid=False, level="card"))

    assert sorted(str(row["id"]) for row in result.rows) == sorted(_ORDINARY)


async def test_run_sql_still_reads_the_meta_pages(gateway: ReadOnlySqlGateway):
    result = await gateway.rows("SELECT id FROM api.search(:word, 10, level => 'card')", {"word": _WORD})

    assert set(_META) <= {row["id"] for row in result.rows}


async def test_a_raw_chunk_reaches_the_search_tool(gateway: ReadOnlySqlGateway):
    result = await SearchHandler(gateway, FakeEmbedding())(Search(query=_RAW_WORD, k=5, collection=_COLLECTION, hybrid=False))

    assert [(row["hit_level"], row["id"]) for row in result.rows] == [("raw", "raw.one")]
