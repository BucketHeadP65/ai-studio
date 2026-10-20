"""Unit tests for the generic retrieval toolset builder (`core.ai.retrieval.toolset`).

The builder defines the four retrieval tools once and registers them into a pydantic-ai
`FunctionToolset` or an MCP server. These specs pin the generic contract with hand-written fake
handlers: the builder registers the expected tool names, applies the caller's description strings,
threads the tool args into the constructed search message, and honours `search_type` so a caller's
`Search` subclass (a facet extension) is the message the search handler receives, which is what makes
`SupportsSearch` and `RetrievalHandlers` generic in the first place.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING

import pytest

from mcp.client import Client
from mcp.server.mcpserver import MCPServer
from pydantic_ai import ModelRetry

from core.ai.retrieval.contracts import (
    Fetch,
    FetchResult,
    GraphExpand,
    GraphPath,
    GraphPaths,
    GraphPathsResult,
    GraphResult,
    KnowledgeBaseStamp,
    NextSteps,
    ResultWindow,
    RetrievalHandlers,
    Search,
    SearchHit,
    SqlResult,
    build_retrieval_toolset,
    register_mcp_tools,
)
from core.ai.retrieval.names import FETCH, FIND_PATHS, GRAPH_EXPAND, SEARCH, TOOL_MAX_RETRIES, TOOL_NAMES

if TYPE_CHECKING:
    from pydantic_ai.tools import RunContext
    from pydantic_ai.toolsets import AbstractToolset


_DESCRIPTIONS = {SEARCH: "search desc", FETCH: "fetch desc", GRAPH_EXPAND: "expand desc", FIND_PATHS: "paths desc"}


class FacetSearch(Search):
    """A caller's facet extension of the generic `Search` (the knowledge-base-specific narrowing fields)."""

    level: str | None = None
    collection: str | None = "default"


@dataclass
class FakeSearch:
    """Records the `Search` (or subclass) it was called with; returns the scripted `SqlResult`."""

    result: SqlResult = field(default_factory=SqlResult)
    calls: list[Search] = field(default_factory=list)

    async def __call__(self, query: Search) -> SqlResult:
        self.calls.append(query)
        return self.result


@dataclass
class FakeFetch:
    """Records the `Fetch` it was called with; returns the scripted `SqlResult`."""

    result: SqlResult = field(default_factory=SqlResult)
    calls: list[Fetch] = field(default_factory=list)

    async def __call__(self, query: Fetch) -> SqlResult:
        self.calls.append(query)
        return self.result


@dataclass
class FakeExpand:
    """Records the `GraphExpand` it was called with; returns the scripted `GraphResult`."""

    result: GraphResult = field(default_factory=GraphResult)
    calls: list[GraphExpand] = field(default_factory=list)
    served: list[frozenset[str]] = field(default_factory=list)

    async def __call__(self, query: GraphExpand, *, tools: frozenset[str] = TOOL_NAMES) -> GraphResult:
        self.calls.append(query)
        self.served.append(tools)
        return self.result


def _paths_result(*paths: GraphPath) -> GraphPathsResult:
    """A handler's own answer: the routes it found, plus the next move it composed for them."""
    summary = f"{len(paths)} route(s) between the two endpoints." if paths else "No route between the two endpoints."
    return GraphPathsResult(paths=paths, next_steps=NextSteps(summary=summary), window=ResultWindow(returned_items=len(paths)))


@dataclass
class FakePaths:
    """Records the `GraphPaths` it was called with; returns the scripted result."""

    result: GraphPathsResult = field(default_factory=_paths_result)
    calls: list[GraphPaths] = field(default_factory=list)
    served: list[frozenset[str]] = field(default_factory=list)

    async def __call__(self, query: GraphPaths, *, tools: frozenset[str] = TOOL_NAMES) -> GraphPathsResult:
        self.calls.append(query)
        self.served.append(tools)
        return self.result


def _handlers(
    *,
    search: FakeSearch | None = None,
    fetch: FakeFetch | None = None,
    graph_expand: FakeExpand | None = None,
    find_paths: FakePaths | None = None,
) -> RetrievalHandlers[Search]:
    return RetrievalHandlers(
        search=search or FakeSearch(),
        fetch=fetch or FakeFetch(),
        graph_expand=graph_expand or FakeExpand(),
        find_paths=find_paths or FakePaths(),
    )


def _hit_rows() -> SqlResult:
    return SqlResult(
        rows=({"id": "u.1", "hit_level": "section", "page_id": "d.1", "page_title": "One", "snippet": "s", "score": 0.9},),
        columns=("id", "hit_level", "page_id", "page_title", "snippet", "score"),
    )


def _unit_row() -> SqlResult:
    return SqlResult(rows=({"id": "page.x", "level": "page", "title": "X", "body": "Body."},), columns=("id", "level", "title", "body"))


def _ctx() -> RunContext[None]:
    from pydantic_ai.models.test import TestModel
    from pydantic_ai.tools import RunContext
    from pydantic_ai.usage import RunUsage

    return RunContext(deps=None, model=TestModel(), usage=RunUsage())


async def _call(toolset: AbstractToolset[None], name: str, args: dict[str, object]) -> object:
    ctx = _ctx()
    tools = await toolset.get_tools(ctx)
    return await toolset.call_tool(name, args, ctx, tools[name])


class TestRegistration:
    async def test_registers_the_four_tools_under_canonical_names(self):
        toolset = build_retrieval_toolset(_handlers(), descriptions=_DESCRIPTIONS)

        assert set(toolset.tools) == {SEARCH, FETCH, GRAPH_EXPAND, FIND_PATHS}

    async def test_only_registers_the_named_subset(self):
        toolset = build_retrieval_toolset(_handlers(), descriptions=_DESCRIPTIONS, only=frozenset({GRAPH_EXPAND, FIND_PATHS}))

        assert set(toolset.tools) == {GRAPH_EXPAND, FIND_PATHS}

    async def test_applies_the_callers_description_strings(self):
        toolset = build_retrieval_toolset(_handlers(), descriptions=_DESCRIPTIONS)

        assert {name: tool.description for name, tool in toolset.tools.items()} == _DESCRIPTIONS

    async def test_forced_hybrid_search_drops_the_bm25_arg(self):
        import inspect

        toolset = build_retrieval_toolset(_handlers(), descriptions=_DESCRIPTIONS, only=frozenset({SEARCH}), search_hybrid=True)

        assert "bm25_only" not in inspect.signature(toolset.tools[SEARCH].function).parameters


class TestTheGraphHandlersLearnWhatTheSurfaceServes:
    """A graph answer's next move may only name tools the caller holds, so the handler is told which those are."""

    async def test_a_graph_only_registration_hands_the_handler_its_own_two_names(self):
        expand = FakeExpand()
        toolset = build_retrieval_toolset(
            _handlers(graph_expand=expand), descriptions=_DESCRIPTIONS, only=frozenset({GRAPH_EXPAND, FIND_PATHS})
        )

        await _call(toolset, GRAPH_EXPAND, {"seed_ids": ["n.1"]})

        assert expand.served == [frozenset({GRAPH_EXPAND, FIND_PATHS})]

    async def test_the_open_registration_hands_the_handler_all_four(self):
        paths = FakePaths()
        toolset = build_retrieval_toolset(_handlers(find_paths=paths), descriptions=_DESCRIPTIONS)

        await _call(toolset, FIND_PATHS, {"from_id": "a", "to_id": "b"})

        assert paths.served == [frozenset({SEARCH, FETCH, GRAPH_EXPAND, FIND_PATHS})]

    async def test_available_names_the_finished_surface_when_the_caller_registers_the_rest_itself(self):
        expand = FakeExpand()
        toolset = build_retrieval_toolset(
            _handlers(graph_expand=expand),
            available=frozenset({SEARCH, FETCH, GRAPH_EXPAND}),
            descriptions=_DESCRIPTIONS,
            only=frozenset({GRAPH_EXPAND}),
        )

        await _call(toolset, GRAPH_EXPAND, {"seed_ids": ["n.1"]})

        assert expand.served == [frozenset({SEARCH, FETCH, GRAPH_EXPAND})]


class TestSearchThreading:
    async def test_threads_the_args_into_a_generic_search_message(self):
        search = FakeSearch(result=_hit_rows())
        toolset = build_retrieval_toolset(_handlers(search=search), descriptions=_DESCRIPTIONS)

        hits = await _call(toolset, SEARCH, {"query": "graph rag", "k": 5, "bm25_only": True})

        (message,) = search.calls
        assert type(message) is Search  # the default search_type is the generic Search
        assert (message.query, message.k, message.hybrid) == ("graph rag", 5, False)
        assert isinstance(hits, list)
        assert isinstance(hits[0], SearchHit)

    async def test_search_type_constructs_the_callers_facet_subclass(self):
        # The generic proof: a caller's Search subclass, passed as search_type, is the message the
        # search handler receives — so SupportsSearch / RetrievalHandlers are genuinely generic.
        search = FakeSearch(result=_hit_rows())
        bundle: RetrievalHandlers[FacetSearch] = RetrievalHandlers(search=search, fetch=FakeFetch(), graph_expand=FakeExpand(), find_paths=FakePaths())

        toolset = build_retrieval_toolset(bundle, descriptions=_DESCRIPTIONS, search_type=FacetSearch)
        await _call(toolset, SEARCH, {"query": "cell A", "k": 3})

        (message,) = search.calls
        assert isinstance(message, FacetSearch)
        assert message.collection == "default"  # a facet default the generic Search does not carry
        assert message.hybrid is True


class TestFetchTool:
    async def test_projects_the_first_row_into_a_fetch_result(self):
        rows = SqlResult(rows=({"id": "u.1", "level": "page", "title": "One", "body": "Body."},), columns=("id", "level", "title", "body"))
        toolset = build_retrieval_toolset(_handlers(fetch=FakeFetch(result=rows)), descriptions=_DESCRIPTIONS)

        unit = await _call(toolset, FETCH, {"unit_id": "u.1"})

        assert isinstance(unit, FetchResult)
        assert (unit.id, unit.level, unit.body) == ("u.1", "page", "Body.")

    async def test_unknown_id_raises_model_retry(self):
        toolset = build_retrieval_toolset(_handlers(fetch=FakeFetch(result=SqlResult())), descriptions=_DESCRIPTIONS)

        with pytest.raises(ModelRetry) as exc:
            await _call(toolset, FETCH, {"unit_id": "u.hallucinated"})
        assert "search" in str(exc.value).lower()


class TestFindPathsTool:
    async def test_answers_the_handlers_own_bounded_result(self):
        # The handler owns the window and the next move; the tool hands its result straight on rather
        # than assembling a second one that could disagree with it.
        path = GraphPath(nodes=(), edges=(), score=0.5)
        answer = _paths_result(path)
        toolset = build_retrieval_toolset(_handlers(find_paths=FakePaths(result=answer)), descriptions=_DESCRIPTIONS)

        out = await _call(toolset, FIND_PATHS, {"from_id": "a", "to_id": "b"})

        assert out is answer

    async def test_an_empty_result_still_carries_a_result_shaped_next_move(self):
        toolset = build_retrieval_toolset(_handlers(find_paths=FakePaths(result=_paths_result())), descriptions=_DESCRIPTIONS)

        out = await _call(toolset, FIND_PATHS, {"from_id": "a", "to_id": "b"})

        assert isinstance(out, GraphPathsResult)
        assert out.paths == ()
        assert out.next_steps.summary


class TestRegisterMcpTools:
    async def test_registers_the_four_tools_on_the_server(self):
        server = MCPServer(name="t")
        register_mcp_tools(server, _handlers(), descriptions=_DESCRIPTIONS)

        async with Client(server) as client:
            listing = await client.list_tools()

        assert {SEARCH, FETCH, GRAPH_EXPAND, FIND_PATHS} <= {tool.name for tool in listing.tools}

    async def test_find_paths_returns_the_paths_result_directly_not_an_untyped_dict(self):
        path = GraphPath(nodes=(), edges=(), score=0.5)
        server = MCPServer(name="t")
        register_mcp_tools(server, _handlers(find_paths=FakePaths(result=_paths_result(path))), descriptions=_DESCRIPTIONS)

        async with Client(server) as client:
            result = await client.call_tool(FIND_PATHS, {"from_id": "a", "to_id": "b"})

        assert result.structured_content is not None
        assert result.structured_content["paths"] == [path.model_dump(mode="json")]
        assert result.structured_content["window"] == ResultWindow(returned_items=1).model_dump(mode="json")


class TestTheRetryBudget:
    """The pydantic-ai toolset caps each tool at `TOOL_MAX_RETRIES`, and the MCP server registers the same tools with no cap."""

    async def test_every_toolset_tool_carries_the_retry_budget(self):
        toolset = build_retrieval_toolset(_handlers(), descriptions=_DESCRIPTIONS)

        assert {name: tool.max_retries for name, tool in toolset.tools.items()} == dict.fromkeys(TOOL_NAMES, TOOL_MAX_RETRIES)

    async def test_the_mcp_server_registers_the_same_four_names(self):
        server = MCPServer(name="t")
        register_mcp_tools(server, _handlers(), descriptions=_DESCRIPTIONS)

        async with Client(server) as client:
            listing = await client.list_tools()

        assert {tool.name for tool in listing.tools} == set(TOOL_NAMES)


class TestTheStampOnTheFlatFetch:
    async def test_the_flat_fetch_carries_the_stamp_the_host_hands_it(self):
        stamp = KnowledgeBaseStamp(slug="repo-docs", changed_since=2)

        async def stamp_source() -> KnowledgeBaseStamp:
            return stamp

        toolset = build_retrieval_toolset(_handlers(fetch=FakeFetch(result=_unit_row())), descriptions=_DESCRIPTIONS, stamp=stamp_source)

        result = await _call(toolset, FETCH, {"unit_id": "page.x"})

        assert isinstance(result, FetchResult)
        assert result.knowledge_base == stamp

    async def test_the_flat_fetch_carries_no_stamp_when_the_host_hands_none(self):
        toolset = build_retrieval_toolset(_handlers(fetch=FakeFetch(result=_unit_row())), descriptions=_DESCRIPTIONS)

        result = await _call(toolset, FETCH, {"unit_id": "page.x"})

        assert isinstance(result, FetchResult)
        assert result.knowledge_base is None
