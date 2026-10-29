"""A throwaway Postgres container for the session, and a schema of this tier's own in it, rebuilt for every test.

`CORE_TEST_DATABASE_URL` points the tier at another throwaway database instead. The tier skips when
Docker is not running and no such database is named.
"""

from __future__ import annotations

import os

from typing import TYPE_CHECKING

import asyncpg
import pytest
import pytest_asyncio

from core.ai.history.contracts import PoolDatabase, create_record_schema

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Iterator

POSTGRES_IMAGE = "postgres:18"
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


def _docker_available() -> bool:
    """Whether a Docker daemon answers."""
    try:
        from docker import from_env

        return bool(from_env().ping())
    except Exception:  # noqa: BLE001 (any docker or transport failure means no Docker)
        return False


@pytest.fixture(scope="session")
def database_url() -> Iterator[str]:
    """The admin URL of this session's throwaway Postgres, or a skip when there is none."""
    named = os.environ.get("CORE_TEST_DATABASE_URL")
    if named:
        yield named
        return
    if not _docker_available():
        pytest.skip("Docker unavailable: skipping the integration tests")
    from testcontainers.community.postgres import PostgresContainer

    container = PostgresContainer(POSTGRES_IMAGE, username="core", password="core", dbname="core", driver=None)
    container.start()
    try:
        yield container.get_connection_url()
    finally:
        container.stop()


@pytest_asyncio.fixture
async def pool(database_url: str) -> AsyncIterator[asyncpg.Pool]:
    """A pool whose every connection works inside this tier's own schema."""
    opened = await asyncpg.create_pool(database_url, min_size=1, max_size=4, server_settings={"search_path": f"{TEST_SCHEMA},public"})
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
