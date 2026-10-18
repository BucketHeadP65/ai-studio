"""`open_asyncpg_pool` opens a pool whose every connection starts with the settings it was given."""

from __future__ import annotations

import asyncpg
import pytest

from core.db import open_asyncpg_pool

from .conftest import DATABASE_URL


async def test_open_pool_starts_every_connection_with_its_server_settings() -> None:
    try:
        pool = await open_asyncpg_pool(DATABASE_URL, max_size=2, server_settings={"search_path": "public"}, target="to the test database")
    except (OSError, asyncpg.PostgresError) as exc:
        pytest.skip(f"no Postgres at {DATABASE_URL}: {type(exc).__name__}")
    try:
        assert await pool.fetchval("SHOW search_path") == "public"
        assert pool.get_max_size() == 2
    finally:
        await pool.close()
