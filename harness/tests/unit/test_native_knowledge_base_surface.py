"""Engram's native knowledge base IS a `KnowledgeBaseSurface`, so the step environment takes it unchanged.

The stand-in in `tests/support/environment.py` proves the environment's own seams. This proves the other half,
and it is the half a stand-in cannot: that the REAL thing engram now ships fits the three-member
slice this app reads a knowledge base through. That contract used to be `MCPToolset`'s by accident of it
being what was plugged in; making it engram's on purpose is what let the subprocess go, and a
rename on either side must fail here rather than at boot.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import pytest

from core.ai.disclosure.contracts import DisclosureEnvelope
from engram import RetrievalKnowledgeBase, SqlKnowledgeBase
from engram.retrieval.capability import ReadSurface
from engram.retrieval.envelope import BadRefError
from engram.retrieval.tools import TOOL_NAMES, RetrievalHandlers

if TYPE_CHECKING:
    from pathlib import Path

    from core.ai.disclosure.contracts import DisclosureMode

from core.ai.retrieval.contracts import ColumnInfo, DescribeSchema, ObjectInfo, SchemaDescription, SqlResult
from engram.retrieval.queries import DescribeSchemaHandler
from pydantic_ai.models.test import TestModel
from pydantic_ai.tools import RunContext
from pydantic_ai.usage import RunUsage

from harness.adapters.websearch import WEB_SEARCH_TOOL
from harness.composition import build_environment
from harness.domain.ports import NOT_FOR_STEPS, SCHEMA_TOOL, SHELL_TOOL, SQL_TOOL, KnowledgeBaseSurface
from harness.session import NoSessionLog
from harness.settings import WebSearchSettings
from proof.instrument import ToolTrace
from proof.knowledge_base import GuardedKnowledgeBase
from tests.support.state import records_under
from tests.support.tiers import tiers_under

if TYPE_CHECKING:
    from collections.abc import Mapping


class FakeGateway:
    """Engram's `SqlGateway` silhouette, answering nothing by default -- the SQL-plugin shape tests need no rows."""

    def __init__(self, schema: SchemaDescription | None = None) -> None:
        self._schema = schema if schema is not None else SchemaDescription(objects=())

    async def rows(self, sql: str, params: Mapping[str, object] | None = None, *, cap: int = 50) -> SqlResult:
        del sql, params, cap
        return SqlResult(rows=(), columns=())

    async def api_objects(self, names: tuple[str, ...] = ()) -> SchemaDescription:
        if not names:
            return self._schema
        wanted = set(names)
        return SchemaDescription(objects=tuple(obj for obj in self._schema.objects if obj.name.removeprefix("api.") in wanted))


class _NeverAnswers:
    """One object satisfying every handler slice; the shape tests never drive a call through it."""

    async def __call__(self, query: object, *, tools: frozenset[str] = TOOL_NAMES) -> Any:  # noqa: ANN401 (one fake satisfies four differently-returning slices)
        del query, tools
        msg = "the shape tests never call a handler"
        raise AssertionError(msg)


class _RootOnly:
    """An envelope reader that serves exactly its collection map."""

    mode: DisclosureMode = "strict"

    async def __call__(self, ref: str) -> DisclosureEnvelope:
        if ref == "collection:notes":
            return DisclosureEnvelope(ref="collection:notes", kind="collection", title="notes", summary="the map", mode="strict")
        msg = f"ref {ref!r} is outside this surface"
        raise BadRefError(msg)


@pytest.fixture
def knowledge_base() -> SqlKnowledgeBase[object]:
    """Engram's capability over a gateway that answers nothing -- the shape is what is under test."""
    gateway = FakeGateway()
    return SqlKnowledgeBase[object](gateway=gateway, describe_handler=DescribeSchemaHandler(gateway), domain="A knowledge base of something.")


def test_it_satisfies_the_surface_the_world_takes(knowledge_base: SqlKnowledgeBase[object]) -> None:
    """Structural, with no cast at the call site -- the same way `MCPToolset` satisfied it."""
    surface: KnowledgeBaseSurface = knowledge_base
    assert surface is knowledge_base


async def test_the_world_derives_its_grants_from_the_live_surface(knowledge_base: SqlKnowledgeBase[object], tmp_path: Path) -> None:
    """Read off the connected knowledge base, never typed out here: that is what "the host supplies it" means."""
    registry = await build_environment(TestModel(), engram=knowledge_base, external_agents=dict, session_log=NoSessionLog(), custom=tmp_path / "custom", records=records_under(tmp_path), web_search=WebSearchSettings(), tiers=tiers_under(tmp_path))
    assert sorted(registry.grants) == sorted((SCHEMA_TOOL, SQL_TOOL, SHELL_TOOL, WEB_SEARCH_TOOL))


async def test_nothing_a_step_must_not_see_is_on_the_native_surface(knowledge_base: SqlKnowledgeBase[object]) -> None:
    """`run_code` collided with `CodeMode`'s tool and `submit_feedback` was noise; neither is here now."""
    offered = {tool.name for tool in await knowledge_base.list_tools()}
    assert offered.isdisjoint(NOT_FOR_STEPS)


async def test_the_proof_harness_guards_it_unchanged(knowledge_base: SqlKnowledgeBase[object]) -> None:
    """`GuardedKnowledgeBase` wraps any `KnowledgeBaseSurface`; swapping the plugin underneath must not touch it."""
    trace = ToolTrace()
    async with GuardedKnowledgeBase(inner=knowledge_base, trace=trace, withheld=frozenset({"paper.withheld"})) as guarded:
        assert {tool.name for tool in await guarded.list_tools()} == {SQL_TOOL, SCHEMA_TOOL}
        refused = await guarded.direct_call_tool(SQL_TOOL, {"sql": "SELECT * FROM api.cards WHERE id = 'paper.withheld'"})
    assert trace.calls[-1].refused, "a call targeting a withheld page never reaches the knowledge base"
    assert isinstance(refused, str)


async def test_a_worker_grant_becomes_a_filtered_view(knowledge_base: SqlKnowledgeBase[object]) -> None:
    """The other plugin the environment uses: a step's grant is a filter over the same toolset."""
    granted = knowledge_base.filtered(lambda _ctx, tool: tool.name == SQL_TOOL)
    tools = await granted.get_tools(RunContext(deps=None, model=TestModel(), usage=RunUsage()))
    assert sorted(tools) == [SQL_TOOL]


async def test_describe_schema_through_the_fake_gateway_answers_only_the_named_object() -> None:
    """Drift guard: `FakeGateway.api_objects` must keep honouring `names`, or `describe_schema(objects=[...])` silently answers everything."""
    schema = SchemaDescription(
        objects=(
            ObjectInfo(
                name="api.cards", kind="view", comment="Compact page rows.", columns=(ColumnInfo(name="id", data_type="text", comment="Page id."),)
            ),
            ObjectInfo(name="api.search", kind="function", comment="Hybrid search.", columns=()),
        )
    )
    handler = DescribeSchemaHandler(FakeGateway(schema))

    rendered = await handler(DescribeSchema(objects=("api.cards",)))

    assert "api.cards" in rendered
    assert "Page id." in rendered
    assert "api.search" not in rendered


@pytest.fixture
def reader() -> RetrievalKnowledgeBase[object]:
    """Engram's read-tools capability over fakes -- the surface the studio boots on today."""
    silent = _NeverAnswers()
    return RetrievalKnowledgeBase[object](
        surfaces=(
            ReadSurface(
                name="notes",
                collection="notes",
                handlers=RetrievalHandlers(search=silent, fetch=silent, graph_expand=silent, find_paths=silent),
                envelope=_RootOnly(),
                domain="A knowledge base of something.",
            ),
        )
    )


def test_the_reader_satisfies_the_surface_the_world_takes(reader: RetrievalKnowledgeBase[object]) -> None:
    """The studio's live surface fits the same three-member slice the SQL plugin did."""
    surface: KnowledgeBaseSurface = reader
    assert surface is reader


async def test_the_world_derives_search_and_fetch_grants_from_the_reader(reader: RetrievalKnowledgeBase[object], tmp_path: Path) -> None:
    """A step may now be granted the two read tools, exactly as it could the SQL pair."""
    registry = await build_environment(TestModel(), engram=reader, external_agents=dict, session_log=NoSessionLog(), custom=tmp_path / "custom", records=records_under(tmp_path), web_search=WebSearchSettings(), tiers=tiers_under(tmp_path))
    assert sorted(registry.grants) == sorted(("search", "fetch", SHELL_TOOL, WEB_SEARCH_TOOL))
