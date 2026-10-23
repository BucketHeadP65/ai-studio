"""The in-process knowledge base capability: one definition of the tools, three voices of instruction.

Two claims are under test, and they are the reason the module exists.

**No drift.** The capability's `run_sql` / `describe_schema` come from the SAME factories the
MCP server registers. A second definition of the row cap or the read-only refusal is a
second answer to what a query costs, so the tests pin the shared origin rather than re-asserting
the behaviour in a second place.

**Domain out of the library.** The tooling text says how to query and never what the knowledge base is
about: the one domain-specific field is `domain`, supplied per instance. A research word baked
into the shipped text would be a research assumption every other knowledge base inherits.
"""

from __future__ import annotations

import json

from datetime import date
from decimal import Decimal
from typing import TYPE_CHECKING
from uuid import UUID

import pytest

from mcp.server.mcpserver.exceptions import ToolError
from pydantic_ai.models.test import TestModel
from pydantic_ai.tools import RunContext
from pydantic_ai.usage import RunUsage
from sqlalchemy.exc import OperationalError

from engram.retrieval import sql_tools
from engram.retrieval.capability import DEFAULT_TOOL_NAMES, SqlKnowledgeBase
from engram.retrieval.domain import ColumnInfo, ObjectInfo, SchemaDescription, SqlResult
from engram.retrieval.queries import DescribeSchemaHandler
from engram.testing.fakes.gateway import FakeSqlGateway

if TYPE_CHECKING:
    from collections.abc import Callable, Mapping, Sequence

    from core.ai.embedding import QueryEmbedder

DOMAIN = "A knowledge base of pickling recipes, their brine ratios and the ferments they came from."


class RefusingGateway(FakeSqlGateway):
    """A gateway whose reads are rejected the way the read-only role rejects a write."""

    async def rows(self, sql: str, params: Mapping[str, object] | None = None, *, cap: int = 50) -> SqlResult:
        del params, cap
        raise OperationalError(sql, {}, Exception("cannot execute UPDATE in a read-only transaction"))


def _knowledge_base(
    gateway: FakeSqlGateway | None = None,
    *,
    embed_query: QueryEmbedder | None = None,
    guidance: Callable[[], str | None] | None = None,
    tool_names: tuple[str, str] = DEFAULT_TOOL_NAMES,
) -> SqlKnowledgeBase[None]:
    """A capability over a fake gateway, with the domain sentence every instance must carry."""
    reader = gateway if gateway is not None else FakeSqlGateway()
    return SqlKnowledgeBase[None](
        gateway=reader,
        describe_handler=DescribeSchemaHandler(reader),
        domain=DOMAIN,
        embed_query=embed_query,
        guidance=guidance,
        tool_names=tool_names,
    )


def _ctx() -> RunContext[None]:
    return RunContext(deps=None, model=TestModel(), usage=RunUsage())


class TestSharedToolDefinitions:
    """The capability and the MCP server serve the same two tools, from the same factories."""

    def test_the_toolset_holds_exactly_the_two_read_tools(self) -> None:
        toolset = _knowledge_base().get_toolset()
        assert toolset is not None
        assert sorted(toolset.tools) == sorted(DEFAULT_TOOL_NAMES)

    def test_the_descriptions_are_the_servers_own_not_a_second_copy(self) -> None:
        """Identity, not equality: a copied string is exactly the drift this module exists to stop."""
        tools = _knowledge_base().get_toolset().tools
        assert tools["run_sql"].description is sql_tools.RUN_SQL_DESCRIPTION
        assert tools["describe_schema"].description is sql_tools.DESCRIBE_SCHEMA_DESCRIPTION

    async def test_run_sql_reads_under_the_shared_row_cap(self) -> None:
        """The row cap is the gateway's, applied by the shared factory — not re-implemented here."""
        gateway = FakeSqlGateway()
        await _knowledge_base(gateway).direct_call_tool("run_sql", {"sql": "SELECT 1", "row_limit": 500})
        assert gateway.last.cap == sql_tools.MAX_ROWS

    async def test_a_rejected_statement_fails_with_the_shared_actionable_hint(self) -> None:
        """A query that did not run must FAIL, carrying the one read-only hint both doors give."""
        with pytest.raises(ToolError, match=sql_tools.READ_ONLY_HINT):
            await _knowledge_base(RefusingGateway()).direct_call_tool("run_sql", {"sql": "UPDATE kb.pages SET id = 1"})

    async def test_describe_schema_renders_through_the_injected_handler(self) -> None:
        gateway = FakeSqlGateway(schema=SchemaDescription(objects=()))
        await _knowledge_base(gateway).direct_call_tool("describe_schema", {})
        assert gateway.api_objects_calls == 1


class TestDescribeSchemaCatalogAndDetail:
    """`describe_schema` through the in-process door: catalog with no `objects`, detail with named ones."""

    def _schema(self) -> SchemaDescription:
        return SchemaDescription(
            objects=(
                ObjectInfo(
                    name="api.cards",
                    kind="view",
                    comment="Compact page rows. A longer second sentence detail mode alone shows.",
                    columns=(ColumnInfo(name="id", data_type="text", comment="Page id column."),),
                ),
                ObjectInfo(name="api.search", kind="function", comment="Hybrid search.", columns=()),
            )
        )

    async def test_no_objects_answers_a_catalog_not_the_column_detail(self) -> None:
        result = await _knowledge_base(FakeSqlGateway(schema=self._schema())).direct_call_tool("describe_schema", {})

        assert isinstance(result, str)
        assert "api.cards (view)" in result
        assert "Page id column." not in result  # column detail is a named-object ask, not the catalog's

    async def test_named_objects_answer_only_their_own_detail(self) -> None:
        result = await _knowledge_base(FakeSqlGateway(schema=self._schema())).direct_call_tool(
            "describe_schema", {"objects": ["api.cards"]}
        )

        assert isinstance(result, str)
        assert "Page id column." in result
        assert "api.search" not in result

    async def test_too_many_named_objects_fails_the_call(self) -> None:
        with pytest.raises(ToolError):
            await _knowledge_base(FakeSqlGateway(schema=self._schema())).direct_call_tool(
                "describe_schema", {"objects": [f"api.obj{i}" for i in range(9)]}
            )

    async def test_an_unknown_object_fails_the_call_naming_valid_ones(self) -> None:
        with pytest.raises(ToolError, match=r"api\.cards"):
            await _knowledge_base(FakeSqlGateway(schema=self._schema())).direct_call_tool(
                "describe_schema", {"objects": ["api.does_not_exist"]}
            )


class TestBoundedRunSqlResults:
    """`run_sql` through the in-process door: bounded cells, bounded rows, the same for the sandbox flavor.

    Drives the shared `_bound_sql_response` core through both envelopes (`make_run_sql`'s
    `{"rows": ..., "note": ...}` here, `make_sandbox_run_sql`'s bare row list in
    `test_mcp_server.py`'s code-mode tests), so the two doors are proven to answer identically.
    """

    def _ten_ordinary_rows(self) -> tuple[dict[str, object], ...]:
        return tuple({"id": f"row{i}", "value": i} for i in range(10))

    async def test_a_long_text_cell_is_shortened_with_an_ellipsis(self) -> None:
        rows = (*self._ten_ordinary_rows(), {"id": "long", "body": "a" * 5_000})
        gateway = FakeSqlGateway(result=SqlResult(rows=rows, columns=("id", "value", "body")))

        result = await _knowledge_base(gateway).direct_call_tool("run_sql", {"sql": "SELECT * FROM api.cards"})

        assert isinstance(result, dict)
        long_row = next(row for row in result["rows"] if row["id"] == "long")
        assert isinstance(long_row["body"], str)
        assert len(long_row["body"]) == sql_tools.SQL_CELL_CHARS
        assert long_row["body"].endswith("…")
        assert result["note"] is not None
        assert "Text cells were shortened" in result["note"]

    async def test_a_string_leaf_nested_inside_a_json_cell_is_also_shortened(self) -> None:
        rows = (*self._ten_ordinary_rows(), {"id": "nested", "payload": {"outer": {"deep": "b" * 3_000}}})
        gateway = FakeSqlGateway(result=SqlResult(rows=rows, columns=("id", "value", "payload")))

        result = await _knowledge_base(gateway).direct_call_tool("run_sql", {"sql": "SELECT * FROM api.cards"})

        assert isinstance(result, dict)
        nested_row = next(row for row in result["rows"] if row["id"] == "nested")
        deep = nested_row["payload"]["outer"]["deep"]
        assert len(deep) == sql_tools.SQL_CELL_CHARS
        assert deep.endswith("…")
        assert nested_row["payload"]["outer"]["deep"] is deep  # structure (dict nesting) preserved, only the leaf cut

    async def test_ten_ordinary_rows_all_come_back_whole_in_column_order(self) -> None:
        gateway = FakeSqlGateway(result=SqlResult(rows=self._ten_ordinary_rows(), columns=("id", "value")))

        result = await _knowledge_base(gateway).direct_call_tool("run_sql", {"sql": "SELECT * FROM api.cards"})

        assert isinstance(result, dict)
        assert len(result["rows"]) == 10
        assert [row["id"] for row in result["rows"]] == [f"row{i}" for i in range(10)]
        assert list(result["rows"][0]) == ["id", "value"]  # column order kept
        assert result["note"] is None  # nothing was shortened or omitted

    async def test_a_row_that_would_cross_the_budget_is_dropped_whole_and_noted(self) -> None:
        wide_row = {f"col{i}": "x" * 2_500 for i in range(10)}  # ~20k chars even after per-cell clipping
        rows = (*self._ten_ordinary_rows(), {"id": "wide", **wide_row})
        gateway = FakeSqlGateway(result=SqlResult(rows=rows, columns=("id", "value", *wide_row)))

        result = await _knowledge_base(gateway).direct_call_tool("run_sql", {"sql": "SELECT * FROM api.cards"})

        assert isinstance(result, dict)
        assert all(row["id"] != "wide" for row in result["rows"])  # dropped whole, never partial
        assert len(result["rows"]) == 10  # the ten ordinary rows still come back complete
        assert result["note"] is not None
        assert "More rows were omitted. Select fewer columns or narrow filters" in result["note"]
        assert len(json.dumps(result)) <= sql_tools.SQL_RESULT_CHARS

    async def test_driver_values_come_back_as_json_scalars(self) -> None:
        """A date, a Decimal and a UUID are ordinary cells of the `api` surface, not a corner case.

        The gateway hands back whatever the driver read, so the bounding step is the one place that
        makes a row JSON-safe. Measuring a row it had not converted made an ordinary `SELECT` fail.
        """
        rows = ({"id": "r1", "updated": date(2026, 9, 1), "weight": Decimal("1.50"), "run": UUID("00000000-0000-4000-8000-000000000001")},)
        gateway = FakeSqlGateway(result=SqlResult(rows=rows, columns=("id", "updated", "weight", "run")))

        result = await _knowledge_base(gateway).direct_call_tool("run_sql", {"sql": "SELECT * FROM api.cards"})

        assert isinstance(result, dict)
        (row,) = result["rows"]
        assert row["updated"] == "2026-09-01"
        assert row["weight"] == "1.50"
        assert row["run"] == "00000000-0000-4000-8000-000000000001"
        assert json.dumps(result)  # the delivered envelope is the measured one: plain JSON throughout

    async def test_scalars_and_arrays_pass_through_untouched(self) -> None:
        rows = ({"id": "r1", "count": 7, "active": True, "tags": ["a", "b", "c"], "note": None},)
        gateway = FakeSqlGateway(result=SqlResult(rows=rows, columns=("id", "count", "active", "tags", "note")))

        result = await _knowledge_base(gateway).direct_call_tool("run_sql", {"sql": "SELECT * FROM api.cards"})

        assert isinstance(result, dict)
        (row,) = result["rows"]
        assert (row["count"], row["active"], row["tags"], row["note"]) == (7, True, ["a", "b", "c"], None)


class TestModularInstructions:
    """Three voices, one order: what this knowledge base is, how to retrieve from it, how the door works."""

    def test_the_domain_leads_and_the_tooling_follows(self) -> None:
        text = _knowledge_base().get_instructions()(_ctx())
        assert text.startswith(DOMAIN), "the caller's sentence about the knowledge base comes first"
        assert "describe_schema" in text, "and the domain-agnostic mechanics follow it"

    def test_a_deployment_with_no_guidance_leaves_no_empty_slot(self) -> None:
        """`guidance` is `None` until somebody writes some, and nothing here pretends otherwise."""
        text = _knowledge_base(guidance=lambda: None).get_instructions()(_ctx())
        assert "\n\n\n" not in text
        assert text == _knowledge_base().get_instructions()(_ctx())

    def test_the_guidance_voice_sits_between_the_domain_and_the_mechanics(self) -> None:
        spoken = "Prefer api.search with k <= 8; expand with graph_expand only past two hops."
        text = _knowledge_base(guidance=lambda: spoken).get_instructions()(_ctx())
        assert text.index(DOMAIN) < text.index(spoken) < text.index("describe_schema")

    def test_guidance_is_read_per_request_not_at_construction(self) -> None:
        """Guidance that changes while this process runs takes effect on the next turn, not the next boot."""
        applied: list[str | None] = [None]
        capability = _knowledge_base(guidance=lambda: applied[0])
        assert "Prefer api.search" not in capability.get_instructions()(_ctx())
        applied[0] = "Prefer api.search with k <= 8."
        assert "Prefer api.search" in capability.get_instructions()(_ctx())

    def test_the_tooling_text_carries_no_domain_words(self) -> None:
        """The shipped mechanics are every knowledge base's; a knowledge base's subject arrives through `domain`."""
        lowered = sql_tools.TOOLING_INSTRUCTIONS_TEMPLATE.lower()
        for word in ("research", "paper", "literature", "recipe", "memory"):
            assert word not in lowered, f"{word!r} is a domain assumption, and domain is the caller's field"

    def test_the_tooling_text_states_the_rules_that_make_the_tools_usable(self) -> None:
        text = _knowledge_base().get_instructions()(_ctx())
        assert "before" in text.lower(), "read the schema before writing SQL"
        assert "read-only" in text.lower()
        assert "50" in text, "the row cap is a fact the caller plans around"
        assert "probe" in text.lower(), "probe before answering from general knowledge"
        assert "put the id beside each claim" in text.lower(), "and cite what the probe returned"

    def test_renaming_the_tools_renames_them_in_the_text_too(self) -> None:
        """The multiplex seam: instructions that named a tool the agent does not hold are a bug."""
        capability = _knowledge_base(tool_names=("query_pickles", "pickle_schema"))
        text = capability.get_instructions()(_ctx())
        assert "query_pickles" in text
        assert "pickle_schema" in text
        assert "run_sql" not in text
        assert sorted(capability.get_toolset().tools) == ["pickle_schema", "query_pickles"]


class TestKnowledgeBaseSurface:
    """The three members a workflow host's step world reads off a knowledge base, served natively."""

    async def test_list_tools_publishes_both_tools_with_their_schemas(self) -> None:
        tools = await _knowledge_base().list_tools()
        assert sorted(tool.name for tool in tools) == sorted(DEFAULT_TOOL_NAMES)
        run_sql = next(tool for tool in tools if tool.name == "run_sql")
        assert run_sql.description == sql_tools.RUN_SQL_DESCRIPTION
        assert "sql" in run_sql.input_schema["properties"]

    async def test_filtered_admits_only_what_the_grant_named(self) -> None:
        capability = _knowledge_base()
        only_reads = capability.filtered(lambda _ctx, tool: tool.name == "describe_schema")
        tools = await only_reads.get_tools(_ctx())
        assert sorted(tools) == ["describe_schema"]

    async def test_direct_call_returns_the_tools_own_payload(self) -> None:
        """A program dispatches through here and gets data, not a rendered transcript."""
        gateway = FakeSqlGateway(result=SqlResult(rows=({"id": "pickle.dill"},), columns=("id",)))
        result = await _knowledge_base(gateway).direct_call_tool("run_sql", {"sql": "SELECT id FROM api.cards"})
        assert result == {"rows": [{"id": "pickle.dill"}], "note": None}

    async def test_an_unknown_tool_name_is_refused_by_name(self) -> None:
        with pytest.raises(KeyError, match="drop_everything"):
            await _knowledge_base().direct_call_tool("drop_everything", {})

    async def test_the_embed_shim_rides_the_native_door_too(self) -> None:
        """`vector_query => '<text>'` is the server's hybrid shim, and it is the factory's, not the server's."""
        gateway = FakeSqlGateway()

        async def embed(text: str) -> Sequence[float]:
            del text
            return [0.5, 0.25]

        await _knowledge_base(gateway, embed_query=embed).direct_call_tool(
            "run_sql", {"sql": "SELECT * FROM api.search('brine', vector_query => 'salt ratios')"}
        )
        assert "query_vec => '[0.5,0.25]'::halfvec" in gateway.last.sql
        assert "vector_query" not in gateway.last.sql
