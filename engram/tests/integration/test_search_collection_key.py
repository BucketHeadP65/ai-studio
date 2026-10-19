r"""`api.search` keys every row by its collection and its id, so two knowledge bases that share an id never mix.

Two collections hold the same page id, section id, claim id, source id and raw chunk id. "alpha" is
shared and "beta" is private. Beta alone carries `_BETA_WORD` on its card, section, claim and raw
chunk, and both carry `_SHARED_WORD` on their cards. Every assertion runs as the `kb_reader` role.

What this pins:

* a search alpha's reader runs, scoped to alpha or to every visible collection, finds nothing for
  beta's word, because alpha holds none of it, so any hit is a row of beta reached through an id
  alpha also holds;
* a session that sees both collections gets one card per collection for a word both hold, never one
  row fused from two.
"""

from __future__ import annotations

import json

from typing import TYPE_CHECKING

import asyncpg
import pytest

from engram.testing.infrastructure.knowledge_base_db import provision_private_reader
from tests.integration.test_migrations import _asyncpg_dsn

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

_ALPHA = "alpha"
_BETA = "beta"
_PAGE = "ent.same"
_SOURCE = "src.same"

_BETA_WORD = "chromatophore"
_SHARED_WORD = "photophore"

_SWITCHES = '{"embeddings": false, "sql": false, "graph": false, "private": %s}'


def _text(collection: str, part: str) -> str:
    """The text one collection stores on one part, carrying beta's word on beta alone."""
    words = f"{collection} {part}"
    return f"{words} {_BETA_WORD}" if collection == _BETA else words


async def _seed(conn: asyncpg.Connection) -> None:
    """Write the same ids into both collections, with beta's word on beta's rows only."""
    collections = (_ALPHA, _BETA)
    await conn.executemany(
        "INSERT INTO kb.pages (collection, id, page_type, slug, title, status, card_text, body_hash, facets)"
        " VALUES ($1, $2, 'entity', $3, $4, 'live', $5, $3, '{}'::jsonb)",
        [(collection, _PAGE, f"{collection}-same", f"{collection} entity", f"{_text(collection, 'card')} {_SHARED_WORD}") for collection in collections],
    )
    await conn.executemany(
        "INSERT INTO kb.sections (collection, id, page_id, ord, canon, path, heading, body, body_hash, tokens)"
        " VALUES ($1, $2, $3, 0, 'definition', 'Definition'::ltree, 'Definition', $4, 'h', 3)",
        [(collection, f"{_PAGE}#s1", _PAGE, _text(collection, "section")) for collection in collections],
    )
    await conn.executemany(
        "INSERT INTO kb.claims (collection, id, page_id, block_id, kind, text, status, subject_collection, subject_id)"
        " VALUES ($1, $2, $3, $2, 'support', $4, 'active', $1, $3)",
        [(collection, f"{_PAGE}#c1", _PAGE, _text(collection, "claim")) for collection in collections],
    )
    await conn.executemany(
        "INSERT INTO kb.sources_raw (collection, source_id, title, source_hash, provider, provider_key, meta)"
        " VALUES ($1, $2, $3, 'h', 'intake', $2, '{}'::jsonb)",
        [(collection, _SOURCE, f"{collection} mirror") for collection in collections],
    )
    await conn.executemany(
        "INSERT INTO kb.raw_chunks (collection, id, source_id, seq, section_path, heading, body)"
        " VALUES ($1, 'raw.same', $2, 0, 'Body'::ltree, 'Body', $3)",
        [(collection, _SOURCE, _text(collection, "raw")) for collection in collections],
    )
    await conn.executemany(
        "INSERT INTO kb.index_fingerprint (collection, indexer_version, spec_hash, vocabulary_hash, embed_model, switches)"
        " VALUES ($1, '1', 's', 'r', 'm', $2::jsonb)",
        [(_ALPHA, _SWITCHES % "false"), (_BETA, _SWITCHES % "true")],
    )


@pytest.fixture
async def seeded(migrated: str) -> str:
    """The head-migrated database with both collections seeded, answering its admin URL."""
    connection = await asyncpg.connect(_asyncpg_dsn(migrated))
    try:
        await _seed(connection)
    finally:
        await connection.close()
    return migrated


@pytest.fixture
async def alpha_reader(seeded: str, reader_url: str) -> AsyncIterator[asyncpg.Connection]:
    """A `kb_reader` connection announcing the shared collection alpha as its own."""
    del seeded
    connection = await asyncpg.connect(_asyncpg_dsn(reader_url))
    try:
        await connection.execute("SELECT set_config('app.collection', $1, false)", _ALPHA)
        yield connection
    finally:
        await connection.close()


@pytest.fixture
async def beta_reader(seeded: str) -> AsyncIterator[asyncpg.Connection]:
    """A connection logged in as the private collection beta's own reader role, so it sees both."""
    connection = await asyncpg.connect(_asyncpg_dsn(await provision_private_reader(seeded, _BETA)))
    try:
        yield connection
    finally:
        await connection.close()


class TestTheSeedIsReal:
    """Beta's word ranks for beta: every absence below is a filtered row, never a word nothing matches."""

    async def test_beta_finds_its_own_word(self, beta_reader: asyncpg.Connection):
        hits = await beta_reader.fetch(
            "SELECT hit_level, folded_units FROM api.search($1, 20, NULL, NULL, '{}'::jsonb, $2, true)", _BETA_WORD, _BETA
        )

        folded = {unit["hit_level"] for row in hits for unit in json.loads(row["folded_units"])}
        assert {row["hit_level"] for row in hits} | folded == {"card", "section", "claim", "raw"}


class TestNoRowCrossesThroughASharedId:
    """A search over alpha never returns beta's rows, whatever id the two collections share."""

    @pytest.mark.parametrize("collection", [_ALPHA, None], ids=["scoped-to-alpha", "every-visible-collection"])
    async def test_alpha_finds_nothing_for_a_word_only_beta_holds(self, alpha_reader: asyncpg.Connection, collection: str | None):
        hits = await alpha_reader.fetch(
            "SELECT id, hit_level FROM api.search($1, 20, NULL, NULL, '{}'::jsonb, $2, true)",
            _BETA_WORD,
            collection,
        )

        assert [(row["hit_level"], row["id"]) for row in hits] == []


class TestOneRowPerCollection:
    """Two collections' rows that share an id stay two hits."""

    async def test_a_word_both_cards_hold_ranks_one_card_per_collection(self, beta_reader: asyncpg.Connection):
        hits = await beta_reader.fetch(
            "SELECT id FROM api.search($1, 20, NULL, 'card', '{}'::jsonb, NULL, true)",
            _SHARED_WORD,
        )

        assert [row["id"] for row in hits] == [_PAGE, _PAGE]
