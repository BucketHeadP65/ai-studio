"""`run_sql` checks its statement, then runs it as written inside the row-cap wrapper, so it reads the meta pages that search and fetch hide."""

from __future__ import annotations

import asyncio

from typing import TYPE_CHECKING

import pytest

from core.ai.retrieval.contracts import TOOL_MAX_RETRIES, SqlResult
from mcp.server.fastmcp.exceptions import ToolError
from pydantic_ai import ModelRetry
from sqlalchemy.exc import OperationalError

from engram.retrieval import sql_tools
from engram.retrieval.capability import SqlKnowledgeBase
from engram.retrieval.queries import DescribeSchemaHandler
from engram.testing.fakes.gateway import FakeSqlGateway

if TYPE_CHECKING:
    from collections.abc import Mapping

STATEMENT = "SELECT * FROM api.search('research', 'guidance', 5)"
WRAPPED = "SELECT * FROM (SELECT * FROM api.search('research', 'guidance', 5)) AS q LIMIT 51"
META_ROW = {"id": "meta.documentation-policy", "page_type": "meta"}


def _gateway() -> FakeSqlGateway:
    return FakeSqlGateway(result=SqlResult(rows=(META_ROW,), columns=("id", "page_type")))


async def test_run_sql_sends_the_statement_as_written_and_returns_meta_rows() -> None:
    gateway = _gateway()

    result = await sql_tools.make_run_sql(gateway, None)(STATEMENT, 50)

    assert gateway.last.sql == WRAPPED
    assert result["rows"] == [META_ROW]


async def test_sandbox_run_sql_sends_the_statement_as_written_and_returns_meta_rows() -> None:
    gateway = _gateway()

    rows = await sql_tools.make_sandbox_run_sql(gateway, None)(STATEMENT, 50)

    assert gateway.last.sql == WRAPPED
    assert rows == [META_ROW]


@pytest.mark.parametrize("factory", [sql_tools.make_run_sql, sql_tools.make_sandbox_run_sql])
def test_the_run_sql_contract_states_it_returns_meta_pages(factory: object) -> None:
    assert callable(factory)
    assert "`meta.*` pages" in (factory.__doc__ or "")


async def test_run_sql_stops_the_database_one_row_past_the_cap() -> None:
    gateway = _gateway()

    await sql_tools.make_run_sql(gateway, None)("SELECT id FROM api.cards;", 5)

    assert gateway.last.sql == "SELECT * FROM (SELECT id FROM api.cards) AS q LIMIT 6"
    assert gateway.last.cap == 5


async def test_run_sql_drops_comments_before_the_statement_runs() -> None:
    gateway = _gateway()

    await sql_tools.make_run_sql(gateway, None)("SELECT id -- the ids\nFROM api.cards /* all */", 50)

    assert gateway.last.sql == "SELECT * FROM (SELECT id \nFROM api.cards) AS q LIMIT 51"


@pytest.mark.parametrize(
    ("statement", "reason"),
    [
        ("SELECT set_config('app.collection', 'other', true)", "That function is not available"),
        ("SELECT 1; SELECT 2", "Send exactly one statement"),
        ("WITH RECURSIVE t AS (SELECT 1) SELECT * FROM t", "WITH RECURSIVE is not allowed"),
    ],
)
async def test_run_sql_refuses_what_the_guard_refuses_and_sends_nothing(statement: str, reason: str) -> None:
    gateway = _gateway()

    with pytest.raises(ToolError, match=reason):
        await sql_tools.make_run_sql(gateway, None)(statement, 50)

    assert gateway.rows_calls == []


async def test_sandbox_run_sql_answers_a_refusal_as_a_retry_and_sends_nothing() -> None:
    gateway = _gateway()

    with pytest.raises(ModelRetry, match="That function is not available"):
        await sql_tools.make_sandbox_run_sql(gateway, None)("SELECT pg_sleep(30)", 50)

    assert gateway.rows_calls == []


class _RejectingGateway(FakeSqlGateway):
    """A gateway whose every statement fails the way the read-only role rejects a write."""

    async def rows(self, sql: str, params: Mapping[str, object] | None = None, *, cap: int = 50) -> SqlResult:
        del params, cap
        raise OperationalError(sql, {}, Exception("cannot execute UPDATE in a read-only transaction"))


async def test_run_sql_asked_to_retry_answers_a_refusal_as_a_retry_and_sends_nothing() -> None:
    gateway = _gateway()

    with pytest.raises(ModelRetry, match="That function is not available"):
        await sql_tools.make_run_sql(gateway, None, retry_refusals=True)("SELECT pg_sleep(30)", 50)

    assert gateway.rows_calls == []


async def test_run_sql_asked_to_retry_answers_a_statement_that_did_not_run_as_a_retry() -> None:
    with pytest.raises(ModelRetry, match="read-only transaction") as caught:
        await sql_tools.make_run_sql(_RejectingGateway(), None, retry_refusals=True)("SELECT id FROM api.cards", 50)

    assert sql_tools.READ_ONLY_HINT in str(caught.value)


async def test_run_sql_still_fails_the_call_on_a_refusal_by_default() -> None:
    with pytest.raises(ToolError, match="read-only transaction"):
        await sql_tools.make_run_sql(_RejectingGateway(), None)("SELECT id FROM api.cards", 50)


async def test_describe_schema_asked_to_retry_answers_an_unknown_object_as_a_retry() -> None:
    describe_schema = sql_tools.make_describe_schema(DescribeSchemaHandler(_gateway()), retry_refusals=True)

    with pytest.raises(ModelRetry, match=r"api\.does_not_exist"):
        await describe_schema(["api.does_not_exist"])


async def test_describe_schema_still_fails_the_call_on_an_unknown_object_by_default() -> None:
    describe_schema = sql_tools.make_describe_schema(DescribeSchemaHandler(_gateway()))

    with pytest.raises(ToolError, match=r"api\.does_not_exist"):
        await describe_schema(["api.does_not_exist"])


async def test_a_sql_knowledge_base_asked_to_retry_returns_its_refusals_to_the_model() -> None:
    gateway = _RejectingGateway()
    capability = SqlKnowledgeBase[None](
        gateway=gateway, describe_handler=DescribeSchemaHandler(gateway), domain="A knowledge base of notes.", retry_refusals=True
    )

    with pytest.raises(ModelRetry, match="read-only transaction"):
        await capability.direct_call_tool("run_sql", {"sql": "SELECT id FROM api.cards"})
    with pytest.raises(ModelRetry, match=r"api\.does_not_exist"):
        await capability.direct_call_tool("describe_schema", {"objects": ["api.does_not_exist"]})


def test_a_sql_knowledge_base_asked_to_retry_gives_its_tools_the_read_tools_retry_budget() -> None:
    gateway = FakeSqlGateway()

    retrying = SqlKnowledgeBase[None](gateway=gateway, describe_handler=DescribeSchemaHandler(gateway), domain="Notes.", retry_refusals=True)
    failing = SqlKnowledgeBase[None](gateway=gateway, describe_handler=DescribeSchemaHandler(gateway), domain="Notes.")

    assert {tool.max_retries for tool in retrying.get_toolset().tools.values()} == {TOOL_MAX_RETRIES}
    assert {tool.max_retries for tool in failing.get_toolset().tools.values()} == {None}


async def test_a_sql_knowledge_base_fails_the_call_on_a_refusal_unless_asked() -> None:
    gateway = _RejectingGateway()
    capability = SqlKnowledgeBase[None](gateway=gateway, describe_handler=DescribeSchemaHandler(gateway), domain="A knowledge base of notes.")

    with pytest.raises(ToolError, match="read-only transaction"):
        await capability.direct_call_tool("run_sql", {"sql": "SELECT id FROM api.cards"})


class _CountingEmbedder:
    """Embeds every text as a one-number vector and records the texts it was asked for."""

    def __init__(self, *, delay: float = 0.0) -> None:
        self.texts: list[str] = []
        self.delay = delay

    async def __call__(self, text: str, /) -> list[float]:
        self.texts.append(text)
        await asyncio.sleep(self.delay)
        return [0.5]


async def test_run_sql_embeds_an_identical_text_once_and_places_its_vector_twice() -> None:
    gateway = _gateway()
    embedder = _CountingEmbedder()
    statement = (
        "SELECT id FROM api.search('a', vector_query => 'same meaning') UNION ALL SELECT id FROM api.search('b', vector_query => 'same meaning')"
    )

    await sql_tools.make_run_sql(gateway, embedder)(statement, 50)

    assert embedder.texts == ["same meaning"]
    assert gateway.last.sql.count("query_vec => '[0.5]'::halfvec") == 2


async def test_run_sql_leaves_the_same_words_inside_a_string_alone() -> None:
    gateway = _gateway()
    embedder = _CountingEmbedder()

    await sql_tools.make_run_sql(gateway, embedder)("SELECT 'vector_query => ''x''' AS note", 50)

    assert embedder.texts == []
    assert "'vector_query => ''x'''" in gateway.last.sql


async def test_run_sql_refuses_a_text_written_any_other_way_before_embedding() -> None:
    gateway = _gateway()
    embedder = _CountingEmbedder()

    with pytest.raises(ToolError, match="Pass vector_query by name, as one quoted text"):
        await sql_tools.make_run_sql(gateway, embedder)("SELECT * FROM api.search('q', vector_query => E'x')", 50)

    assert embedder.texts == []
    assert gateway.rows_calls == []


async def test_run_sql_refuses_a_fifth_text_before_embedding() -> None:
    gateway = _gateway()
    embedder = _CountingEmbedder()
    statement = "SELECT " + ", ".join(f"(SELECT count(*) FROM api.search('q', vector_query => 'text {n}'))" for n in range(5))  # noqa: S608 (a statement under test, never run)

    with pytest.raises(ToolError, match="at most 4 vector_query texts"):
        await sql_tools.make_run_sql(gateway, embedder)(statement, 50)

    assert embedder.texts == []


async def test_run_sql_runs_keyword_only_when_embedding_misses_its_deadline() -> None:
    gateway = _gateway()
    embedder = _CountingEmbedder(delay=1.0)

    answer = await sql_tools.make_run_sql(gateway, embedder, embed_deadline=0.05)("SELECT * FROM api.search('q', vector_query => 'm')", 50)

    assert "query_vec => NULL" in gateway.last.sql
    assert "longer than 0.05 seconds" in str(answer["note"])


class _ModelEmbedder(_CountingEmbedder):
    """A counting embedder that names its model."""

    model_id = "embed-large"


async def test_run_sql_names_the_embedders_model_beside_each_vector() -> None:
    gateway = _gateway()

    await sql_tools.make_run_sql(gateway, _ModelEmbedder())("SELECT * FROM api.search('q', vector_query => 'm')", 50)

    assert "query_vec => '[0.5]'::halfvec, query_model => 'embed-large'" in gateway.last.sql


async def test_run_sql_names_no_model_for_an_embedder_that_names_none() -> None:
    gateway = _gateway()

    await sql_tools.make_run_sql(gateway, _CountingEmbedder())("SELECT * FROM api.search('q', vector_query => 'm')", 50)

    assert "query_model" not in gateway.last.sql
