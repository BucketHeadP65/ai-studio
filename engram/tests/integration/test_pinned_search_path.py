"""Every `api` and `kb` function that pins a `search_path` reads `pg_catalog` first and `pg_temp` last.

What this pins:

* at head, no pinned function lets a temporary object or a listed schema shadow a built-in;
* the generated search still answers through its new path, as the reader;
* one step down and back up restores the same settings.
"""

from __future__ import annotations

import asyncpg

from engram.testing.infrastructure.knowledge_base_db import run_downgrade, run_upgrade_head
from tests.integration.test_migrations import _asyncpg_dsn

_PINNED = """
SELECT p.oid::regprocedure::text AS signature, c.setting
FROM pg_proc p
JOIN pg_namespace n ON n.oid = p.pronamespace
CROSS JOIN LATERAL unnest(p.proconfig) AS c(setting)
WHERE n.nspname IN ('api', 'kb') AND c.setting LIKE 'search_path=%'
ORDER BY 1
"""


async def _pinned(url: str) -> dict[str, list[str]]:
    """Each pinned function's `search_path`, by signature."""
    connection = await asyncpg.connect(_asyncpg_dsn(url))
    try:
        rows = await connection.fetch(_PINNED)
    finally:
        await connection.close()
    return {row["signature"]: [schema.strip() for schema in row["setting"].removeprefix("search_path=").split(",")] for row in rows}


async def test_every_pinned_path_reads_the_catalog_first_and_temporary_objects_last(migrated: str):
    pinned = await _pinned(migrated)

    assert "api.visible_collection(text)" in pinned, "the hand-written functions must be covered too, or this is vacuous"
    wrong = {signature: path for signature, path in pinned.items() if path[0] != "pg_catalog" or path[-1] != "pg_temp"}
    assert wrong == {}


async def test_the_search_still_answers_as_the_reader(migrated: str, reader_url: str):
    del migrated
    connection = await asyncpg.connect(_asyncpg_dsn(reader_url))
    try:
        rows = await connection.fetch("SELECT * FROM api.search('anything', 5)")
    finally:
        await connection.close()

    assert rows == []


async def test_one_step_down_and_back_restores_the_same_settings(migrated: str):
    at_head = await _pinned(migrated)

    run_downgrade(migrated, "0053")
    below = await _pinned(migrated)
    run_upgrade_head(migrated)

    assert all(path[-1] == "pg_catalog" and "pg_temp" not in path for path in below.values())
    assert await _pinned(migrated) == at_head
