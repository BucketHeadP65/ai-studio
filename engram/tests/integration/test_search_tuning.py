"""`api.search` stops every leg at a budget and reads its numbers from `kb.retrieval_tuning`, clamped to its bounds.

One shared collection holds thirty cards with the same embedding and a 300-character card text. The query text matches
no card, so every hit comes from the semantic leg. Searches run as `kb_reader`, and the owner changes the default
tuning row.

What this pins:

* the row starts with the defaults, and a search returns up to `k` rows under them;
* a smaller budget stops the semantic leg, so fewer rows reach the fusion;
* a weight of zero on a leg scores its hits zero;
* a snippet length below the bound is held at the bound;
* the reader can neither read the table nor add a row to it, and a row refuses a value that is not a number.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import asyncpg
import pytest

from tests.integration.test_migrations import _asyncpg_dsn

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

_COLLECTION = "notes"
_VECTOR = "[" + ",".join(["1"] + ["0"] * 1023) + "]"
_TEXT = "lorem " * 50
_SEARCH = "SELECT * FROM api.search('zzzz', 20, $1::halfvec, 'card', '{}'::jsonb, $2, true)"


@pytest.fixture
async def admin(migrated: str) -> AsyncIterator[asyncpg.Connection]:
    """An owner connection over thirty cards that share one embedding."""
    connection = await asyncpg.connect(_asyncpg_dsn(migrated))
    try:
        await connection.executemany(
            "INSERT INTO kb.pages (collection, id, page_type, slug, title, status, card_text, body_hash, facets, embedding)"
            " VALUES ($1, $2, 'entity', $2, $2, 'live', $3, $2, '{}'::jsonb, $4::halfvec)",
            [(_COLLECTION, f"ent.{number:02d}", _TEXT, _VECTOR) for number in range(30)],
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


async def _tune(admin: asyncpg.Connection, tuning: str) -> None:
    """Merge `tuning` into the default row."""
    await admin.execute("UPDATE kb.retrieval_tuning SET tuning = tuning || $1::jsonb WHERE collection IS NULL", tuning)


async def test_the_row_starts_with_the_defaults(admin: asyncpg.Connection, reader: asyncpg.Connection):
    tuning = await admin.fetchval("SELECT tuning::text FROM kb.retrieval_tuning WHERE collection IS NULL")
    rows = await reader.fetch(_SEARCH, _VECTOR, _COLLECTION)

    assert '"budget": 50' in str(tuning)
    assert len(rows) == 20


async def test_a_smaller_budget_stops_the_semantic_leg(admin: asyncpg.Connection, reader: asyncpg.Connection):
    await _tune(admin, '{"budget": 5, "k_ceiling": 5}')

    rows = await reader.fetch(_SEARCH, _VECTOR, _COLLECTION)

    assert len(rows) == 5


async def test_a_budget_never_falls_below_k(admin: asyncpg.Connection, reader: asyncpg.Connection):
    await _tune(admin, '{"budget": 5}')

    rows = await reader.fetch(_SEARCH, _VECTOR, _COLLECTION)

    assert len(rows) == 20


async def test_a_zero_weight_scores_its_legs_hits_zero(admin: asyncpg.Connection, reader: asyncpg.Connection):
    await _tune(admin, '{"weights": {"card:semantic": 0}}')

    rows = await reader.fetch(_SEARCH, _VECTOR, _COLLECTION)

    assert rows
    assert {row["score"] for row in rows} == {0.0}


async def test_a_snippet_length_below_the_bound_is_held_at_the_bound(admin: asyncpg.Connection, reader: asyncpg.Connection):
    await _tune(admin, '{"snippet_chars": 10}')

    rows = await reader.fetch(_SEARCH, _VECTOR, _COLLECTION)

    assert {len(row["snippet"]) for row in rows} == {80}
    assert all(row["snippet_truncated"] for row in rows)


async def test_the_reader_can_neither_read_nor_change_the_row(reader: asyncpg.Connection):
    with pytest.raises(asyncpg.InsufficientPrivilegeError):
        await reader.fetch("SELECT * FROM kb.retrieval_tuning")
    with pytest.raises((asyncpg.InsufficientPrivilegeError, asyncpg.ReadOnlySQLTransactionError)):
        await reader.execute("INSERT INTO kb.retrieval_tuning (collection, tuning) VALUES ($1, '{}'::jsonb)", _COLLECTION)


async def test_the_row_refuses_a_value_that_is_not_a_number(admin: asyncpg.Connection):
    with pytest.raises(asyncpg.CheckViolationError):
        await _tune(admin, '{"budget": "many"}')
    with pytest.raises(asyncpg.CheckViolationError):
        await _tune(admin, '{"weights": {"card:semantic": "high"}}')
