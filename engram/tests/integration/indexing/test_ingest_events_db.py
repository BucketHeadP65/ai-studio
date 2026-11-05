"""Integration: the events a caller hands `embedded_authoring` land in `coordination.outbox` with the page, or not at all.

Drives the real write path (`embedded_authoring` over a freshly migrated database, the real
`SqlAlchemyKnowledgeBaseUnitOfWork`). A write that lands leaves its page and one outbox row per event. A write the
handler refuses, into a collection a knowledge base indexes, leaves neither. Skips without Docker like its siblings.
"""

from __future__ import annotations

import pytest

from core.cqrs.contracts import Event
from sqlalchemy import NullPool, text
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine

from engram.agent_memory.contracts import agent_memory_schema
from engram.indexing.contracts import CollectionIsIndexedError
from engram.indexing.infrastructure.repository import ManifestEntries
from engram.retrieval.infrastructure.embedded import embedded_authoring
from engram.testing.fakes.embedding import FakeEmbedding

WRITTEN = "events-ingest"
"""A collection no knowledge base indexes, written only through the ingest."""

INDEXED = "events-indexed"
"""A collection with a manifest row, so the ingest refuses to write into it."""

PAGE_ID = "mem.global.main.world.postgres-port"


class _Noted(Event):
    """A fact the caller records with its page."""

    note: str


def _page() -> dict[str, object]:
    return {
        "frontmatter": {
            "id": PAGE_ID,
            "type": "memory",
            "title": "Local Postgres port",
            "summary": "The local database answers on one port.",
            "status": "live",
            "category": "world",
        },
        "sections": [{"heading": "Note", "body": "Answers on 5433."}],
    }


async def _scalar_rows(migrated: str, sql: str, **params: str) -> list[tuple[str, ...]]:
    engine = create_async_engine(migrated, poolclass=NullPool)
    try:
        async with engine.connect() as conn:
            result = await conn.execute(text(sql), params)
            return [tuple(str(value) for value in row) for row in result.all()]
    finally:
        await engine.dispose()


async def _index(migrated: str, collection: str) -> None:
    engine = create_async_engine(migrated, poolclass=NullPool)
    try:
        async with AsyncSession(engine) as session, session.begin():
            await ManifestEntries(session).upsert_many(
                [
                    {
                        "collection": collection,
                        "path": "Notes/indexed.md",
                        "kind": "note",
                        "content_hash": "c",
                        "identity_hash": "i",
                        "page_id": None,
                        "source_key": None,
                    }
                ],
                collection=collection,
            )
    finally:
        await engine.dispose()


async def test_a_write_with_one_event_leaves_the_page_and_one_outbox_row_of_that_event(migrated: str) -> None:
    marker = _Noted(note="the port moved")

    async with embedded_authoring(migrated, spec=agent_memory_schema(), embedder=FakeEmbedding()) as ingest:
        await ingest(_page(), collection=WRITTEN, events=(marker,))

    pages = await _scalar_rows(migrated, "SELECT id FROM kb.pages WHERE collection = :collection", collection=WRITTEN)
    outbox = await _scalar_rows(migrated, "SELECT event_id, event_type, payload ->> 'note' FROM coordination.outbox")
    assert pages == [(PAGE_ID,)]
    assert outbox == [(str(marker.event_id), "_Noted", "the port moved")]


async def test_a_refused_write_leaves_neither_the_page_nor_the_event(migrated: str) -> None:
    await _index(migrated, INDEXED)

    async with embedded_authoring(migrated, spec=agent_memory_schema(), embedder=FakeEmbedding()) as ingest:
        with pytest.raises(CollectionIsIndexedError):
            await ingest(_page(), collection=INDEXED, events=(_Noted(note="the port moved"),))

    pages = await _scalar_rows(migrated, "SELECT id FROM kb.pages WHERE collection = :collection", collection=INDEXED)
    outbox = await _scalar_rows(migrated, "SELECT event_id FROM coordination.outbox")
    assert pages == []
    assert outbox == []
