"""`api.search` puts a lone exact title first, and keeps at most `rows_per_group` hits of one page or source.

One shared collection holds a long page whose card and six sections all carry the query's words, a short page whose
title is exactly the query and whose card carries the words once, and one source whose five raw chunks carry another
word. Every search runs keyword only, as `kb_reader`, with the cap on hits per document off, so the cap on rows per group
shows alone.

What this pins:

* one page's card and sections keep three rows together, however many match;
* a card whose title is the query goes first, outside its page's count;
* a second card with the same title pins neither;
* one source's raw chunks keep three rows;
* `rows_per_group` in the tuning row changes the count;
* the comment an agent reads names both rules and the owner's numbers, and one step down puts 0061's comment back.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import asyncpg
import pytest

from engram.testing.infrastructure.knowledge_base_db import run_downgrade, run_upgrade_head
from tests.integration.test_migrations import _asyncpg_dsn, _load_migration_module

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

_COLLECTION = "notes"
_QUERY = "graph retrieval"
_LONG = "ent.long"
_TITLED = "ent.titled"
_SEARCH = "SELECT id, page_id, hit_level FROM api.search($1, 20, NULL, $2, '{}'::jsonb, $3, true)"
_COMMENT = "SELECT obj_description(p.oid, 'pg_proc') FROM pg_proc p JOIN pg_namespace n ON n.oid = p.pronamespace WHERE n.nspname = 'api' AND p.proname = 'search'"


async def _page(admin: asyncpg.Connection, page_id: str, title: str, card_text: str) -> None:
    """One live page with its card."""
    await admin.execute(
        "INSERT INTO kb.pages (collection, id, page_type, slug, title, status, card_text, body_hash, facets)"
        " VALUES ($1, $2, 'entity', $2, $3, 'live', $4, $2, '{}'::jsonb)",
        _COLLECTION,
        page_id,
        title,
        card_text,
    )


@pytest.fixture
async def admin(migrated: str) -> AsyncIterator[asyncpg.Connection]:
    """An owner connection over the long page, the titled page and the source, with no cap on hits per document."""
    connection = await asyncpg.connect(_asyncpg_dsn(migrated))
    try:
        await connection.execute("UPDATE kb.retrieval_tuning SET tuning = tuning || '{\"max_hits_per_document\": 0}'::jsonb WHERE id = 1")
        await _page(connection, _LONG, "Long notes", f"{_QUERY} {_QUERY} {_QUERY}")
        await connection.executemany(
            "INSERT INTO kb.sections (collection, id, page_id, ord, canon, path, heading, body, body_hash, tokens)"
            " VALUES ($1, $2, $3, $4, 'definition', 'Definition'::ltree, 'Definition', $5, 'h', 3)",
            [(_COLLECTION, f"{_LONG}#s{n}", _LONG, n, f"{_QUERY} {_QUERY} part {n}") for n in range(6)],
        )
        await _page(connection, _TITLED, "Graph Retrieval", "a short card about graph retrieval")
        await connection.execute(
            "INSERT INTO kb.sources_raw (collection, source_id, title, source_hash, provider, provider_key, meta)"
            " VALUES ($1, 'src.long', 'Long source', 'h', 'intake', 'src.long', '{}'::jsonb)",
            _COLLECTION,
        )
        await connection.executemany(
            "INSERT INTO kb.raw_chunks (collection, id, source_id, seq, section_path, heading, body)"
            " VALUES ($1, $2, 'src.long', $3, 'Body'::ltree, 'Body', 'an astrolabe passage')",
            [(_COLLECTION, f"raw.{n}", n) for n in range(5)],
        )
        yield connection
    finally:
        await connection.close()


@pytest.fixture
async def reader(admin: asyncpg.Connection, reader_url: str) -> AsyncIterator[asyncpg.Connection]:
    """A `kb_reader` connection over the same database."""
    del admin
    connection = await asyncpg.connect(_asyncpg_dsn(reader_url))
    try:
        yield connection
    finally:
        await connection.close()


async def test_one_pages_card_and_sections_keep_three_rows_together(reader: asyncpg.Connection):
    rows = await reader.fetch(_SEARCH, _QUERY, None, _COLLECTION)

    assert len([row for row in rows if row["page_id"] == _LONG]) == 3


async def test_a_card_whose_title_is_the_query_goes_first(reader: asyncpg.Connection):
    rows = await reader.fetch(_SEARCH, "  GRAPH retrieval ", None, _COLLECTION)

    assert (rows[0]["id"], rows[0]["hit_level"]) == (_TITLED, "card")


async def test_a_title_two_cards_hold_pins_neither(admin: asyncpg.Connection, reader: asyncpg.Connection):
    await _page(admin, "ent.twin", "Graph Retrieval", "another card about graph retrieval")

    rows = await reader.fetch(_SEARCH, _QUERY, None, _COLLECTION)

    assert rows[0]["page_id"] == _LONG


async def test_one_sources_raw_chunks_keep_three_rows(reader: asyncpg.Connection):
    rows = await reader.fetch(_SEARCH, "astrolabe", "raw", _COLLECTION)

    assert len(rows) == 3


async def test_the_tuning_row_changes_the_count(admin: asyncpg.Connection, reader: asyncpg.Connection):
    await admin.execute("UPDATE kb.retrieval_tuning SET tuning = tuning || '{\"rows_per_group\": 5}'::jsonb WHERE id = 1")

    rows = await reader.fetch(_SEARCH, "astrolabe", "raw", _COLLECTION)

    assert len(rows) == 5


async def _comment(url: str) -> str:
    """The comment on `api.search`."""
    connection = await asyncpg.connect(_asyncpg_dsn(url))
    try:
        return str(await connection.fetchval(_COMMENT))
    finally:
        await connection.close()


async def test_the_comment_names_both_rules_and_the_owners_numbers(migrated: str):
    comment = await _comment(migrated)

    assert "comes first when no other card holds that title" in comment
    assert "keeps at most its best few hits (3 unless the owner sets another)" in comment
    assert "480 characters unless the owner sets another, between 80 and 2,000" in comment
    assert "50 unless the owner sets it lower" in comment


async def test_one_step_down_puts_the_earlier_comment_back(migrated: str):
    at_head = await _comment(migrated)

    run_downgrade(migrated, "0062")
    below = await _comment(migrated)
    run_upgrade_head(migrated)

    assert below == _load_migration_module("0063")._PREVIOUS_COMMENT_TEXT
    assert await _comment(migrated) == at_head
