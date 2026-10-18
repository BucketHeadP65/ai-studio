"""The inspection's pool on the sessions database: its own schema, and the record's tables in reach of the same connection."""

from __future__ import annotations

import asyncio

from dataclasses import dataclass

import asyncpg
import pytest_asyncio

from core.ai.history.contracts import PoolDatabase, create_record_schema, row_as, rows_as
from core.ai.inspection.contracts import InspectionStore, create_inspection_schema

from harness.adapters.wiring import open_record_pool
from harness.features.inspection.infrastructure.postgres import INSPECTION_DDL_LOCK, INSPECTION_NAMESPACE, open_inspection_pool
from tests.integration.conftest import as_asyncpg_dsn

TABLES = "SELECT table_name FROM information_schema.tables WHERE table_schema = $1 ORDER BY table_name"


@dataclass(frozen=True)
class TableRow:
    table_name: str


@dataclass(frozen=True)
class PathRow:
    search_path: str


@pytest_asyncio.fixture
async def dsn(pg: str) -> str:
    return as_asyncpg_dsn(pg)


async def _reset_inspection(dsn: str) -> None:
    """Drop the inspection schema so a test starts from a database that has never seen it."""
    connection = await asyncpg.connect(dsn)
    try:
        await connection.execute("DROP SCHEMA IF EXISTS inspection CASCADE")
    finally:
        await connection.close()


async def _tables_in(dsn: str, schema: str) -> list[str]:
    connection = await asyncpg.connect(dsn)
    try:
        return [row.table_name for row in rows_as(TableRow, await connection.fetch(TABLES, schema))]
    finally:
        await connection.close()


async def test_the_pool_creates_its_schema_and_reaches_the_record(dsn: str) -> None:
    sessions = await asyncpg.create_pool(dsn, min_size=1, max_size=2)
    record = await open_record_pool(sessions, dsn=dsn)
    await create_record_schema(PoolDatabase(record))
    try:
        pool = await open_inspection_pool(sessions, dsn=dsn)
        try:
            await create_inspection_schema(PoolDatabase(pool))
            async with pool.acquire() as connection:
                names = [row.table_name for row in rows_as(TableRow, await connection.fetch(TABLES, INSPECTION_NAMESPACE))]
                path = row_as(PathRow, await connection.fetchrow("SHOW search_path")).search_path
                await connection.execute("SELECT count(*) FROM run_events")
            assert names == ["cursors", "findings", "leads", "questions"]
            assert path.replace(" ", "") == "inspection,history"
            assert await InspectionStore(PoolDatabase(pool)).position("inspection.dispatch") is None
        finally:
            await pool.close()
    finally:
        await record.close()
        await sessions.close()


async def test_the_pool_creates_its_tables_in_its_own_schema_on_a_fresh_database(dsn: str) -> None:
    sessions = await asyncpg.create_pool(dsn, min_size=1, max_size=2)
    record = await open_record_pool(sessions, dsn=dsn)
    await create_record_schema(PoolDatabase(record))
    try:
        await _reset_inspection(dsn)
        pool = await open_inspection_pool(sessions, dsn=dsn)
        try:
            assert await _tables_in(dsn, "inspection") == ["cursors", "findings", "leads", "questions"]
            history_tables = await _tables_in(dsn, "history")
            assert "cursors" not in history_tables
            assert "findings" not in history_tables
            assert "leads" not in history_tables
        finally:
            await pool.close()
    finally:
        await record.close()
        await sessions.close()


async def test_two_processes_opening_the_pool_at_once_both_succeed(dsn: str) -> None:
    await _reset_inspection(dsn)
    sessions = await asyncpg.create_pool(dsn, min_size=2, max_size=2)
    try:
        pool_a, pool_b = await asyncio.gather(open_inspection_pool(sessions, dsn=dsn), open_inspection_pool(sessions, dsn=dsn))
        try:
            assert pool_a is not None
            assert pool_b is not None
            assert await _tables_in(dsn, "inspection") == ["cursors", "findings", "leads", "questions"]
        finally:
            await pool_a.close()
            await pool_b.close()
    finally:
        await sessions.close()


async def test_opening_the_pool_waits_while_another_holds_the_ddl_lock(dsn: str) -> None:
    holder = await asyncpg.connect(dsn)
    try:
        await holder.execute("SELECT pg_advisory_lock($1)", INSPECTION_DDL_LOCK)
        sessions = await asyncpg.create_pool(dsn, min_size=1, max_size=2)
        try:
            task = asyncio.create_task(open_inspection_pool(sessions, dsn=dsn))
            try:
                await asyncio.sleep(0.5)
                assert not task.done()
            finally:
                await holder.execute("SELECT pg_advisory_unlock($1)", INSPECTION_DDL_LOCK)
            pool = await asyncio.wait_for(task, 10)
            await pool.close()
        finally:
            await sessions.close()
    finally:
        await holder.close()
