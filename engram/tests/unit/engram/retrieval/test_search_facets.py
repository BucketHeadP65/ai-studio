"""`search` narrows by the facets the knowledge base's schema declares, on every variant of the single-base tool.

`facets` maps a facet key to a list of values: values for one key match any, several keys must all match. The keys
come from the caller, read from the knowledge base's schema. A key the schema does not declare is refused before
any handler runs, naming the declared keys, and with no declared keys every key is refused. The description names
the declared keys. A pinned search drops `collection` but keeps `since`, `until` and `facets`.
"""

from __future__ import annotations

import json

from datetime import date
from typing import TYPE_CHECKING

import pytest

from mcp.client import Client
from mcp.server.mcpserver import MCPServer
from pydantic_ai import ModelRetry

from engram.retrieval.domain import SearchResult, SqlResult
from engram.retrieval.infrastructure.mcp_server import create_mcp_server
from engram.retrieval.queries import FetchHandler, GraphExpandHandler, GraphPathsHandler, SearchHandler
from engram.retrieval.tools import build_retrieval_toolset, register_mcp_tools, search_next_steps
from engram.testing.fakes.embedding import FakeEmbedding
from engram.testing.fakes.gateway import FakeSqlGateway
from tests.unit.engram.retrieval.test_mcp_server import _describe_handler, _no_reach, _schema
from tests.unit.engram.retrieval.test_tools import FakeSearchHandler, _call, _ctx, _handlers, _search_rows, _text

if TYPE_CHECKING:
    from collections.abc import Callable

_COLLECTION = "workdocs"
_KEYS = ("shade", "texture")
_FACETS = {"shade": ["teal", "amber"], "texture": ["matte"]}


class TestToolset:
    async def test_the_facets_reach_the_handler_on_the_search_message(self):
        search = FakeSearchHandler(result=_search_rows())
        toolset = build_retrieval_toolset(_handlers(search=search), collection=_COLLECTION, facet_keys=_KEYS)

        await _call(toolset, "search", {"query": "brine", "facets": _FACETS})

        (message,) = search.calls
        assert message.facets == _FACETS

    @pytest.mark.parametrize("search_hybrid", [None, True, False])
    @pytest.mark.parametrize("k_source", [None, lambda: 4])
    async def test_every_variant_passes_the_facets(self, search_hybrid: bool | None, k_source: Callable[[], int] | None):
        search = FakeSearchHandler(result=_search_rows())
        toolset = build_retrieval_toolset(
            _handlers(search=search), search_hybrid=search_hybrid, k_source=k_source, collection=_COLLECTION, facet_keys=_KEYS
        )

        await _call(toolset, "search", {"query": "brine", "facets": {"shade": ["teal"]}})

        (message,) = search.calls
        assert message.facets == {"shade": ["teal"]}

    async def test_a_search_without_facets_sends_none(self):
        search = FakeSearchHandler(result=_search_rows())
        toolset = build_retrieval_toolset(_handlers(search=search), collection=_COLLECTION, facet_keys=_KEYS)

        await _call(toolset, "search", {"query": "brine"})

        (message,) = search.calls
        assert message.facets == {}

    async def test_a_pinned_search_honours_the_facets_and_the_range_and_still_ignores_collection(self):
        search = FakeSearchHandler(result=_search_rows())
        toolset = build_retrieval_toolset(_handlers(search=search), collection=_COLLECTION, pinned=True, facet_keys=_KEYS)

        await _call(toolset, "search", {"query": "brine", "facets": _FACETS, "collection": "elsewhere", "since": "2026"})

        (message,) = search.calls
        assert message.facets == _FACETS
        assert (message.collection, message.since, message.until) == (_COLLECTION, date(2026, 1, 1), None)

    async def test_a_pinned_search_offers_facets_and_dates_and_not_collection(self):
        toolset = build_retrieval_toolset(_handlers(), collection=_COLLECTION, pinned=True, facet_keys=_KEYS)
        tools = await toolset.get_tools(_ctx())

        offered = set(tools["search"].tool_def.parameters_json_schema["properties"])
        assert {"facets", "since", "until"} <= offered
        assert "collection" not in offered

    async def test_an_undeclared_key_is_refused_naming_the_declared_keys(self):
        search = FakeSearchHandler(result=_search_rows())
        toolset = build_retrieval_toolset(_handlers(search=search), collection=_COLLECTION, facet_keys=_KEYS)

        with pytest.raises(ModelRetry) as refused:
            await _call(toolset, "search", {"query": "brine", "facets": {"weight": ["heavy"], "shade": ["teal"]}})

        assert "'weight'" in str(refused.value)
        assert "`shade`, `texture`" in str(refused.value)
        assert search.calls == []

    async def test_a_pinned_search_refuses_an_undeclared_key_too(self):
        toolset = build_retrieval_toolset(_handlers(), collection=_COLLECTION, pinned=True, facet_keys=_KEYS)

        with pytest.raises(ModelRetry, match="`shade`, `texture`"):
            await _call(toolset, "search", {"query": "brine", "facets": {"weight": ["heavy"]}})

    async def test_with_no_declared_facets_every_key_is_refused_saying_there_are_none(self):
        search = FakeSearchHandler(result=_search_rows())
        toolset = build_retrieval_toolset(_handlers(search=search), collection=_COLLECTION)

        with pytest.raises(ModelRetry, match="declares no facets"):
            await _call(toolset, "search", {"query": "brine", "facets": {"shade": ["teal"]}})

        assert search.calls == []

    @pytest.mark.parametrize("search_hybrid", [None, True, False])
    @pytest.mark.parametrize("pinned", [False, True])
    def test_every_description_names_the_declared_keys_and_asks_for_stored_values(self, search_hybrid: bool | None, pinned: bool):
        toolset = build_retrieval_toolset(_handlers(), search_hybrid=search_hybrid, collection=_COLLECTION, pinned=pinned, facet_keys=_KEYS)

        description = toolset.tools["search"].description or ""
        assert "`facets`" in description
        assert "`shade`, `texture`" in description
        assert "exactly as the knowledge base stores it" in description

    def test_a_description_override_replaces_the_facets_sentences_with_the_rest(self):
        toolset = build_retrieval_toolset(_handlers(), collection=_COLLECTION, descriptions={"search": "Find things."}, facet_keys=_KEYS)

        assert toolset.tools["search"].description == "Find things."

    def test_with_no_declared_facets_the_description_says_to_leave_facets_out(self):
        toolset = build_retrieval_toolset(_handlers(), collection=_COLLECTION)

        assert "declares no facets, so leave `facets` out" in (toolset.tools["search"].description or "")


class TestNoHits:
    def test_a_faceted_search_that_finds_nothing_names_the_facets_as_a_lever(self):
        steps = search_next_steps((), query="brine", knowledge_base="kitchen", hybrid=True, status="no_hits", map_collection=None, filtered=True)

        assert steps.options == ("Try broader or different terms, or loosen or drop `facets`.",)

    def test_a_faceted_dated_search_that_finds_nothing_names_both_levers(self):
        steps = search_next_steps(
            (), query="brine", knowledge_base="kitchen", hybrid=True, status="no_hits", map_collection=None, dated=True, filtered=True
        )

        assert steps.options == (
            (
                "Try broader or different terms, widen or drop `since` and `until`, or loosen or drop `facets`: "
                "a page with no page date never matches a range."
            ),
        )

    async def test_a_pinned_search_that_passed_no_facets_and_finds_nothing_names_only_the_query(self):
        toolset = build_retrieval_toolset(_handlers(), collection=_COLLECTION, pinned=True, facet_keys=_KEYS)

        result = await _call(toolset, "search", {"query": "brine"})

        assert isinstance(result, SearchResult)
        assert result.next_steps.options[0] == "Try broader or different terms."

    async def test_the_facets_lever_appears_only_when_the_call_passed_facets(self):
        declared = build_retrieval_toolset(_handlers(), collection=_COLLECTION, facet_keys=_KEYS)
        undeclared = build_retrieval_toolset(_handlers(), collection=_COLLECTION)

        filtered = await _call(declared, "search", {"query": "brine", "facets": {"shade": ["teal"]}})
        unfiltered = await _call(declared, "search", {"query": "brine"})
        without_keys = await _call(undeclared, "search", {"query": "brine"})

        assert isinstance(filtered, SearchResult)
        assert isinstance(unfiltered, SearchResult)
        assert isinstance(without_keys, SearchResult)
        assert "loosen or drop `facets`" in filtered.next_steps.options[0]
        assert unfiltered.next_steps.options[0] == without_keys.next_steps.options[0] == "Try broader or different terms."


class TestMcpServer:
    async def test_the_served_search_takes_facets_and_refuses_an_undeclared_key(self):
        search = FakeSearchHandler(result=_search_rows())
        server = MCPServer(name="t")
        register_mcp_tools(server, _handlers(search=search), collection=_COLLECTION, facet_keys=_KEYS)

        async with Client(server) as client:
            listing = await client.list_tools()
            answered = await client.call_tool("search", {"query": "brine", "facets": {"texture": ["matte"]}})
            refused = await client.call_tool("search", {"query": "brine", "facets": {"weight": ["heavy"]}})

        [tool] = [published for published in listing.tools if published.name == "search"]
        assert "facets" in tool.input_schema["properties"]
        assert "`shade`, `texture`" in (tool.description or "")
        assert answered.is_error is False
        assert refused.is_error is True
        assert "`shade`, `texture`" in _text(refused)
        assert [message.facets for message in search.calls] == [{"texture": ["matte"]}]


def _server(gateway: FakeSqlGateway, *, only: frozenset[str] | None = None, facet_keys: tuple[str, ...] = ()) -> MCPServer:
    """A server over the whole single-base retrieval surface, every handler reading through `gateway`."""
    return create_mcp_server(
        gateway,
        _describe_handler(_schema()),
        search_handler=SearchHandler(gateway, FakeEmbedding()),
        fetch_handler=FetchHandler(gateway, _COLLECTION),
        graph_expand_handler=GraphExpandHandler(_no_reach(gateway), _COLLECTION),
        find_paths_handler=GraphPathsHandler(gateway, _COLLECTION),
        only=only,
        collection=_COLLECTION,
        facet_keys=facet_keys,
    )


class TestCreateMcpServer:
    async def test_the_declared_keys_reach_the_single_base_search(self):
        gateway = FakeSqlGateway(result=SqlResult(rows=(), columns=()))
        server = _server(gateway, facet_keys=_KEYS)

        async with Client(server) as client:
            listing = await client.list_tools()
            answered = await client.call_tool("search", {"query": "brine", "facets": {"shade": ["teal"]}})

        [tool] = [published for published in listing.tools if published.name == "search"]
        assert "`shade`, `texture`" in (tool.description or "")
        assert answered.is_error is False
        assert json.loads(str(gateway.rows_calls[-1].params["facets"])) == {"shade": ["teal"]}

    async def test_the_sandboxed_search_takes_the_declared_keys_too(self):
        gateway = FakeSqlGateway(result=SqlResult(rows=(), columns=()))
        server = _server(gateway, only=frozenset({"search", "fetch", "graph_expand", "find_paths", "run_code"}), facet_keys=_KEYS)

        code = "result = await search(query='brine', facets={'texture': ['matte']})\nresult['status']\n"
        async with Client(server) as client:
            result = await client.call_tool("run_code", {"code": code})

        assert result.is_error is False
        assert json.loads(str(gateway.rows_calls[-1].params["facets"])) == {"texture": ["matte"]}
