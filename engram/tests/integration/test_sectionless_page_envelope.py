"""A page with no sections arrives whole through the leveled read, read as `kb_reader`.

The about page an index builds from `_meta/` is such a page: a `kb.pages` row whose text is its `card_text`, with no
`kb.sections` rows. `EnvelopeHandler` reads its text through `api.fetch` when `api.envelope_page` lists no section.

What this pins:

* the about page's envelope carries its whole text in `content`, not `None`;
* a page with sections still reads its text from its sections.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import asyncpg
import pytest

from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy.pool import NullPool

from engram.retrieval.envelope import EnvelopeHandler
from engram.retrieval.infrastructure.gateway import ReadOnlySqlGateway
from tests.integration.test_migrations import _asyncpg_dsn

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

_COLLECTION = "notes"
_ABOUT = f"meta.about@{_COLLECTION}"
_ABOUT_TEXT = "# Purpose & scope\n\nWhat this knowledge base is for."


@pytest.fixture
async def gateway(migrated: str, reader_url: str) -> AsyncIterator[ReadOnlySqlGateway]:
    """A `kb_reader` gateway over a collection holding the about page and one page with a section."""
    admin = await asyncpg.connect(_asyncpg_dsn(migrated))
    try:
        await admin.executemany(
            "INSERT INTO kb.pages (collection, id, page_type, slug, title, status, card_text, body_hash, facets)"
            " VALUES ($1, $2, $3, $2, $4, 'verified', $5, $2, '{}'::jsonb)",
            [
                (_COLLECTION, _ABOUT, "meta", "About this memory", _ABOUT_TEXT),
                (_COLLECTION, "ent.one", "entity", "One", "The card of one."),
            ],
        )
        await admin.execute(
            "INSERT INTO kb.sections (collection, id, page_id, heading, path, ord, body, body_hash)"
            " VALUES ($1, 'ent.one#body', 'ent.one', 'Body', 'body'::ltree, 0, 'The body of one.', 'h')",
            _COLLECTION,
        )
    finally:
        await admin.close()
    engine = create_async_engine(reader_url, poolclass=NullPool)
    try:
        yield ReadOnlySqlGateway(engine, collection=_COLLECTION)
    finally:
        await engine.dispose()


async def test_the_about_page_arrives_whole(gateway: ReadOnlySqlGateway):
    envelope = await EnvelopeHandler(gateway, collection=_COLLECTION)(f"page:{_COLLECTION}/{_ABOUT}")

    assert envelope.children == ()
    assert envelope.content == _ABOUT_TEXT


async def test_a_page_with_a_section_reads_its_text_from_the_section(gateway: ReadOnlySqlGateway):
    envelope = await EnvelopeHandler(gateway, collection=_COLLECTION)(f"page:{_COLLECTION}/ent.one")

    assert [child.ref for child in envelope.children] == [f"section:{_COLLECTION}/ent.one#body"]
    assert envelope.content == "## Body\n\nThe body of one."
