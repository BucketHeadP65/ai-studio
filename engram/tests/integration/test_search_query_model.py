"""`api.search` compares a query vector only with the vectors of the model that made it.

Three shared collections hold one card each, embedded with the same vector. "same-model" records the query's model,
"other-model" records another, and "unrecorded" has no fingerprint row. The query text matches no card, so every hit
comes from the semantic leg. Every assertion runs as `kb_reader`.

What this pins:

* with `query_model` set, the semantic leg leaves out a collection indexed under another model;
* a collection that records no model keeps its semantic leg;
* with `query_model` left NULL, every collection is compared, as before.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import asyncpg
import pytest

from tests.integration.test_migrations import _asyncpg_dsn

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

_SAME = "same-model"
_OTHER = "other-model"
_UNRECORDED = "unrecorded"

_VECTOR = "[" + ",".join(["1"] + ["0"] * 1023) + "]"

_SEARCH = (
    "SELECT src_collection, vector_rank FROM api.search('zzzz', 20, $1::halfvec, 'card', '{}'::jsonb, NULL, true,"
    " NULL, NULL, $2) ORDER BY src_collection"
)


@pytest.fixture
async def reader(migrated: str, reader_url: str) -> AsyncIterator[asyncpg.Connection]:
    """A `kb_reader` connection over three collections whose cards share one vector."""
    admin = await asyncpg.connect(_asyncpg_dsn(migrated))
    try:
        await admin.executemany(
            "INSERT INTO kb.pages (collection, id, page_type, slug, title, status, card_text, body_hash, facets, embedding)"
            " VALUES ($1, 'ent.one', 'entity', $1, 'One', 'live', 'alpha', 'h', '{}'::jsonb, $2::halfvec)",
            [(collection, _VECTOR) for collection in (_SAME, _OTHER, _UNRECORDED)],
        )
        await admin.executemany(
            "INSERT INTO kb.index_fingerprint (collection, indexer_version, spec_hash, vocabulary_hash, embed_model, switches)"
            " VALUES ($1, '1', 's', 'r', $2, '{}'::jsonb)",
            [(_SAME, "embed-large"), (_OTHER, "embed-small")],
        )
    finally:
        await admin.close()
    connection = await asyncpg.connect(_asyncpg_dsn(reader_url))
    try:
        yield connection
    finally:
        await connection.close()


async def test_the_semantic_leg_leaves_out_a_collection_indexed_under_another_model(reader: asyncpg.Connection):
    rows = await reader.fetch(_SEARCH, _VECTOR, "embed-large")

    assert [(row["src_collection"], row["vector_rank"] is not None) for row in rows] == [(_SAME, True), (_UNRECORDED, True)]


async def test_no_query_model_compares_every_collection(reader: asyncpg.Connection):
    rows = await reader.fetch(_SEARCH, _VECTOR, None)

    assert [row["src_collection"] for row in rows] == [_OTHER, _SAME, _UNRECORDED]
