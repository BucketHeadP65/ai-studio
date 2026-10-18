"""Unit tests that the stored cut plans' SQL compiles for PostgreSQL, keeps a stored plan and binds one parameter for any number of hashes."""

from __future__ import annotations

from typing import TYPE_CHECKING

from sqlalchemy.dialects import postgresql
from sqlalchemy.ext.asyncio import create_async_engine

from engram.indexing.infrastructure.cut_plans import CutPlanRow, SqlAlchemyCutPlans

if TYPE_CHECKING:
    from sqlalchemy import Select
    from sqlalchemy.dialects.postgresql import Insert

    from engram.indexing.contracts import CutPlanQueries, CutPlans


def _compiles(statement: Insert | Select[tuple[str, list[int]]]) -> str:
    return str(statement.compile(dialect=postgresql.dialect()))


def test_the_insert_writes_kb_cut_plans_and_keeps_a_plan_already_stored() -> None:
    sql = _compiles(SqlAlchemyCutPlans.insert_statement([CutPlanRow(collection="research", text_hash="h1", planner="example-model/prompt-1", starts=[5, 9])]))

    assert "INSERT INTO kb.cut_plans" in sql
    assert "ON CONFLICT (collection, text_hash, planner) DO NOTHING" in sql


def test_the_read_binds_every_text_hash_as_one_parameter() -> None:
    statement = SqlAlchemyCutPlans.select_statement("research", "example-model/prompt-1", [f"h{index}" for index in range(40_000)])

    compiled = statement.compile(dialect=postgresql.dialect())

    assert "FROM kb.cut_plans" in str(compiled)
    assert "unnest" in str(compiled)
    assert len(compiled.params) == 3


def test_one_adapter_serves_the_write_and_the_read_port() -> None:
    adapter = SqlAlchemyCutPlans(create_async_engine("postgresql+asyncpg://example:example@localhost/example"))
    plans: CutPlans = adapter
    queries: CutPlanQueries = adapter

    assert plans is queries
