"""The coordination tables compile against the PostgreSQL dialect and carry the guards the design needs."""

from __future__ import annotations

from sqlalchemy.dialects import postgresql
from sqlalchemy.schema import CreateIndex, CreateTable

from core.cqrs.infrastructure import schema


def test_every_table_lives_in_the_coordination_schema():
    assert {table.schema for table in schema.coordination.tables.values()} == {"coordination"}
    assert set(schema.coordination.tables) == {"coordination.jobs", "coordination.outbox", "coordination.event_deliveries"}


def test_the_live_key_index_is_partial_and_unique():
    index = next(i for i in schema.jobs.indexes if i.name == "ux_jobs_live_key")
    ddl = str(CreateIndex(index).compile(dialect=postgresql.dialect()))
    assert "UNIQUE" in ddl
    assert "WHERE state IN ('queued', 'running', 'failed') AND key <> ''" in ddl


def test_the_tables_compile_for_postgres():
    for table in schema.coordination.tables.values():
        ddl = str(CreateTable(table).compile(dialect=postgresql.dialect()))
        assert "CREATE TABLE coordination." in ddl
