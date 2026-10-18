"""A Postgres schema of this tier's own, on the stack's database, dropped and rebuilt for every test."""

from __future__ import annotations

import os

from typing import TYPE_CHECKING

import asyncpg
import pytest
import pytest_asyncio

from core.ai.history.contracts import PoolDatabase, create_record_schema

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

DATABASE_URL = os.environ.get("CORE_TEST_DATABASE_URL", "postgresql://wiki:wiki@127.0.0.1:5434/knowledge_bases")
TEST_SCHEMA = "core_record_test"
DROP_RECORD_TABLES = """
    DROP TABLE IF EXISTS
        continuation_heads,
        continuation_attempts,
        tool_effects,
        step_parameters,
        request_parameters,
        steps,
        instructions,
        message_stamps,
        message_forms,
        messages,
        run_events,
        runs
    CASCADE
"""


@pytest_asyncio.fixture
async def pool() -> AsyncIterator[asyncpg.Pool]:
    """A pool whose every connection works inside this tier's own schema, or a skip when there is no Postgres."""
    try:
        opened = await asyncpg.create_pool(DATABASE_URL, min_size=1, max_size=4, server_settings={"search_path": f"{TEST_SCHEMA},public"})
    except (OSError, asyncpg.PostgresError) as exc:
        pytest.skip(f"no Postgres at {DATABASE_URL}: {type(exc).__name__}")
    if opened is None:
        pytest.skip(f"no Postgres at {DATABASE_URL}")
    async with opened.acquire() as connection:
        await connection.execute(f"CREATE SCHEMA IF NOT EXISTS {TEST_SCHEMA}")
    try:
        yield opened
    finally:
        await opened.close()


@pytest_asyncio.fixture
async def database(pool: asyncpg.Pool) -> AsyncIterator[PoolDatabase]:
    """The record's database on an empty schema, emptied again when the test ends."""
    built = PoolDatabase(pool)
    async with built.acquire() as connection, connection.transaction():
        await connection.execute(DROP_RECORD_TABLES)
    await create_record_schema(built)
    yield built
    async with built.acquire() as connection, connection.transaction():
        await connection.execute(DROP_RECORD_TABLES)
