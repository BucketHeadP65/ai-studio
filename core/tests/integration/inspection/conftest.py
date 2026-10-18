"""The inspection's tables on the tier's own Postgres schema, dropped and rebuilt for every test."""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest_asyncio

from core.ai.history.contracts import PoolDatabase
from core.ai.inspection.infrastructure.schema import create_inspection_schema

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

    import asyncpg

DROP_INSPECTION_TABLES = "DROP TABLE IF EXISTS findings, questions, leads, cursors CASCADE"


@pytest_asyncio.fixture
async def inspection_database(pool: asyncpg.Pool) -> AsyncIterator[PoolDatabase]:
    """The inspection's database on an empty schema, emptied again when the test ends."""
    built = PoolDatabase(pool)
    async with built.acquire() as connection, connection.transaction():
        await connection.execute(DROP_INSPECTION_TABLES)
    await create_inspection_schema(built)
    yield built
    async with built.acquire() as connection, connection.transaction():
        await connection.execute(DROP_INSPECTION_TABLES)
