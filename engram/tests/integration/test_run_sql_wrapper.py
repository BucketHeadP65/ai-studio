"""`run_sql` runs a checked statement as `kb_reader` inside `SELECT * FROM (...) AS q LIMIT n`.

What this pins against a real database:

* the queries a person writes still run inside the wrapper: a CTE, an ORDER BY, two columns with one name;
* the database stops one row past the cap, and the answer says rows were left out;
* anything that is not a query fails inside the wrapper, by syntax, before it can write.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from mcp.server.fastmcp.exceptions import ToolError
from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy.pool import NullPool

from engram.retrieval.infrastructure.gateway import ReadOnlySqlGateway
from engram.retrieval.sql_tools import make_run_sql

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

    from engram.retrieval.sql_tools import RunSqlTool


@pytest.fixture
async def run_sql(reader_url: str) -> AsyncIterator[RunSqlTool]:
    """The `run_sql` tool over an anonymous `kb_reader` gateway."""
    engine = create_async_engine(reader_url, poolclass=NullPool)
    try:
        yield make_run_sql(ReadOnlySqlGateway(engine), None)
    finally:
        await engine.dispose()


async def test_a_cte_with_an_order_runs_inside_the_wrapper(run_sql: RunSqlTool):
    answer = await run_sql("WITH n AS (SELECT generate_series(1, 3) AS i) SELECT i FROM n ORDER BY i DESC", 50)

    assert answer["rows"] == [{"i": 3}, {"i": 2}, {"i": 1}]


async def test_two_columns_with_one_name_run_inside_the_wrapper(run_sql: RunSqlTool):
    answer = await run_sql("SELECT c.id, c.id FROM (SELECT 'x' AS id) AS c", 50)

    assert answer["rows"] == [{"id": "x"}]


async def test_the_database_stops_one_row_past_the_cap(run_sql: RunSqlTool):
    answer = await run_sql("SELECT generate_series(1, 1000000) AS i", 3)

    assert answer["rows"] == [{"i": 1}, {"i": 2}, {"i": 3}]
    assert "3+ more rows" in str(answer["note"])


@pytest.mark.parametrize(
    "statement",
    [
        "INSERT INTO api.cards (id) VALUES ('x')",
        "CREATE TABLE api.planted (id int)",
        "SET ROLE wiki",
    ],
)
async def test_a_statement_that_is_not_a_query_fails_by_syntax(run_sql: RunSqlTool, statement: str):
    with pytest.raises(ToolError, match="syntax error"):
        await run_sql(statement, 50)
