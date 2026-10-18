"""The coordination tables, created from their own metadata in a database that has none."""

from collections.abc import AsyncIterator
from urllib.parse import urlsplit, urlunsplit

import asyncpg
import pytest
import pytest_asyncio

from sqlalchemy import text

from core.cqrs.contracts import create_coordination_schema
from core.db import async_dsn, build_engine
from tests.integration.conftest import DATABASE_URL

THROWAWAY = "core_coordination_test"
TABLES = "SELECT table_name FROM information_schema.tables WHERE table_schema = 'coordination' ORDER BY table_name"


def _named(url: str, database: str) -> str:
    parts = urlsplit(url)
    return urlunsplit((parts.scheme, parts.netloc, f"/{database}", parts.query, ""))


@pytest_asyncio.fixture
async def throwaway_url() -> AsyncIterator[str]:
    """A database of its own for the test, dropped afterwards, or a skip when there is no Postgres."""
    try:
        admin = await asyncpg.connect(DATABASE_URL)
    except (OSError, asyncpg.PostgresError) as exc:
        pytest.skip(f"no Postgres at {DATABASE_URL}: {type(exc).__name__}")
    try:
        await admin.execute(f"DROP DATABASE IF EXISTS {THROWAWAY}")
        await admin.execute(f"CREATE DATABASE {THROWAWAY}")
        yield _named(DATABASE_URL, THROWAWAY)
        await admin.execute(f"DROP DATABASE IF EXISTS {THROWAWAY} WITH (FORCE)")
    finally:
        await admin.close()


async def test_the_schema_and_its_tables_appear_and_creating_twice_is_harmless(throwaway_url: str) -> None:
    engine = build_engine(async_dsn(throwaway_url))
    try:
        await create_coordination_schema(engine)
        await create_coordination_schema(engine)
        async with engine.connect() as connection:
            names = [row[0] for row in (await connection.execute(text(TABLES))).all()]
    finally:
        await engine.dispose()

    assert names == ["event_deliveries", "jobs", "outbox"]
