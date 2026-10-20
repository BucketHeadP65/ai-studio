"""`open_asyncpg_pool` opens a pool whose every connection starts with the settings it was given."""

from __future__ import annotations

from core.db import open_asyncpg_pool


async def test_open_pool_starts_every_connection_with_its_server_settings(database_url: str) -> None:
    pool = await open_asyncpg_pool(database_url, max_size=2, server_settings={"search_path": "public"}, target="to the test database")
    try:
        assert await pool.fetchval("SHOW search_path") == "public"
        assert pool.get_max_size() == 2
    finally:
        await pool.close()
