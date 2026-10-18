"""The coordination tables, on their own `MetaData` in the `coordination` schema.

The alembic chain that creates them in the knowledge-base database lives in engram.
`create_coordination_schema` creates them anywhere else. These definitions are what the Postgres
adapters query and what the drift test holds the migrated database to.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    Column,
    DateTime,
    Identity,
    Index,
    Integer,
    MetaData,
    PrimaryKeyConstraint,
    Table,
    Text,
    Uuid,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncEngine

coordination = MetaData(schema="coordination")

jobs = Table(
    "jobs",
    coordination,
    Column("id", BigInteger, Identity(), primary_key=True),
    Column("kind", Text, nullable=False),
    Column("key", Text, nullable=False, server_default=text("''")),
    Column("payload", JSONB, nullable=False, server_default=text("'{}'::jsonb")),
    Column("state", Text, nullable=False, server_default=text("'queued'")),
    Column("attempts", Integer, nullable=False, server_default=text("0")),
    Column("max_attempts", Integer, nullable=False, server_default=text("3")),
    Column("available_at", DateTime(timezone=True), nullable=False, server_default=text("now()")),
    Column("locked_at", DateTime(timezone=True)),
    Column("locked_by", Text),
    Column("lease_seconds", Integer, nullable=False, server_default=text("300")),
    Column("last_error", Text),
    Column("result", Text),
    Column("created_at", DateTime(timezone=True), nullable=False, server_default=text("now()")),
    Column("started_at", DateTime(timezone=True)),
    Column("finished_at", DateTime(timezone=True)),
    CheckConstraint("state IN ('queued', 'running', 'failed', 'done', 'abandoned')", name="ck_jobs_state"),
    Index("ux_jobs_live_key", "kind", "key", unique=True, postgresql_where=text("state IN ('queued', 'running', 'failed') AND key <> ''")),
    Index("ix_jobs_claim", "state", "available_at"),
)
"""A unit of work for a job runner. `(kind, key)` is unique while a job is live, so the same work
cannot be queued twice from any process."""

outbox = Table(
    "outbox",
    coordination,
    Column("id", BigInteger, Identity(), primary_key=True),
    Column("event_id", Uuid, nullable=False, unique=True),
    Column("event_type", Text, nullable=False),
    Column("payload", JSONB, nullable=False),
    Column("created_at", DateTime(timezone=True), nullable=False, server_default=text("now()")),
    Column("delivered_at", DateTime(timezone=True)),
    Index("ix_outbox_undelivered", "id", postgresql_where=text("delivered_at IS NULL")),
)
"""Events written inside the producing transaction, delivered by a job runner in id order."""

event_deliveries = Table(
    "event_deliveries",
    coordination,
    Column("event_id", Uuid, nullable=False),
    Column("subscriber", Text, nullable=False),
    Column("delivered_at", DateTime(timezone=True), nullable=False, server_default=text("now()")),
    PrimaryKeyConstraint("event_id", "subscriber", name="pk_event_deliveries"),
)
"""One row per (event, subscriber) that has been handled. The key is what makes a repeat visible."""


async def create_coordination_schema(engine: AsyncEngine) -> None:
    """Create the `coordination` schema and its tables in the engine's database, skipping every one already there.

    It is for a database no alembic chain provisions. A database engram migrates already carries them and is left as it is.

    Args:
        engine: The engine of the database to create them in.
    """
    async with engine.begin() as connection:
        await connection.execute(text("CREATE SCHEMA IF NOT EXISTS coordination"))
        await connection.run_sync(coordination.create_all)
