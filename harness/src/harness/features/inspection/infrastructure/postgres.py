"""The Postgres adapters of the inspection slice: its pool on the sessions database, the jobs engine, and the record's agent runs.

The inspection's tables live in their own schema beside the record's. Its pool's connections are born with a search
path that reaches both, so the dispatch reads the record's events and moves its cursor on one connection. The commands
live in the `coordination` schema of the same database, reached through a SQLAlchemy engine.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Final

from core.ai.history.contracts import PoolDatabase, reads
from core.ai.inspection.contracts import create_inspection_schema
from core.cqrs.contracts import create_coordination_schema
from core.db import async_dsn, build_engine, open_asyncpg_pool

from harness.adapters.contracts import RECORD_NAMESPACE, ssl_terms

if TYPE_CHECKING:
    import asyncpg

    from core.ai.history.contracts import Database, RunRow
    from sqlalchemy.ext.asyncio import AsyncEngine

INSPECTION_NAMESPACE = "inspection"
"""The schema the inspection's tables live in, inside the sessions database, beside the record's."""

INSPECTION_DDL_LOCK: Final[int] = 84_378_291_001
"""The `pg_advisory_lock` key every process takes while it creates the inspection's schema and tables."""

COORDINATION_DDL_LOCK: Final[int] = 84_378_291_002
"""The `pg_advisory_xact_lock` key every process takes while it creates the coordination schema and tables."""


@dataclass(frozen=True)
class InspectionDatabase:
    """The pool the inspection's cursors, leads and findings live in, whose connections also reach the record."""

    pool: asyncpg.Pool


@dataclass(frozen=True)
class JobsEngine:
    """The engine the coordination tables of the sessions database are reached through."""

    engine: AsyncEngine


async def open_inspection_pool(sessions: asyncpg.Pool, *, dsn: str) -> asyncpg.Pool:
    """Open the inspection's pool over the database its sessions are on, creating its schema and tables first.

    Every connection is born with the inspection's schema first on its search path and the record's second, and no
    connection is moved after. The schema is created under `INSPECTION_DDL_LOCK`, taken with `pg_advisory_lock` on a
    sessions connection outside any transaction, and the tables are created over the new pool while the lock is still
    held. Any other process opening the pool at the same time waits at the lock. A pool whose tables fail to create is
    closed before the failure propagates.

    Args:
        sessions: The sessions database's pool, which the lock is taken on.
        dsn: The writer DSN the inspection's pool connects with.

    Returns:
        The inspection's pool, its schema and tables in place.

    Raises:
        RuntimeError: when asyncpg answered no pool at all.
    """
    async with sessions.acquire() as connection:
        await connection.execute("SELECT pg_advisory_lock($1)", INSPECTION_DDL_LOCK)
        try:
            await connection.execute(f"CREATE SCHEMA IF NOT EXISTS {INSPECTION_NAMESPACE}")
            pool = await open_asyncpg_pool(
                dsn,
                ssl=ssl_terms(dsn),
                server_settings={"search_path": f"{INSPECTION_NAMESPACE},{RECORD_NAMESPACE}"},
                target=f"onto the inspection in schema {INSPECTION_NAMESPACE!r}",
            )
            try:
                await create_inspection_schema(PoolDatabase(pool))
            except BaseException:
                await pool.close()
                raise
        finally:
            await connection.execute("SELECT pg_advisory_unlock($1)", INSPECTION_DDL_LOCK)
    return pool


async def open_jobs_engine(sessions: asyncpg.Pool, *, dsn: str) -> AsyncEngine:
    """Build the engine over the sessions database and create the coordination tables behind `COORDINATION_DDL_LOCK`.

    The lock is taken with `pg_advisory_xact_lock` on a sessions connection for exactly the length of that DDL, so two
    processes booting at once serialize it instead of deadlocking. An engine whose tables fail to create is disposed
    before the failure propagates. You must dispose the engine you get back.

    Args:
        sessions: The sessions database's pool, which the lock is taken on.
        dsn: The writer DSN the engine connects with.

    Returns:
        The engine, the coordination tables in place.
    """
    engine = build_engine(async_dsn(dsn))
    try:
        async with sessions.acquire() as connection, connection.transaction():
            await connection.execute("SELECT pg_advisory_xact_lock($1)", COORDINATION_DDL_LOCK)
            await create_coordination_schema(engine)
    except BaseException:
        await engine.dispose()
        raise
    return engine


@dataclass(frozen=True)
class RecordRuns:
    """The record's agent runs, read on the record's connections."""

    database: Database

    async def run(self, run_id: str) -> RunRow | None:
        """Read the agent run's row on one of the record's connections.

        Args:
            run_id: The agent run's id.

        Returns:
            The agent run's row, or `None` when the record holds no agent run with that id.
        """
        async with self.database.acquire() as connection:
            return await reads.run(connection, run_id)
