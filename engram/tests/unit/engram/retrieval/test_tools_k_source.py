"""The host-owned row cap on the shared retrieval tools: `k` hidden from the agent, resolved live.

`RetrievalKnowledgeBase` is one door onto the cap; these are the other two — the pydantic-ai
`FunctionToolset` an eval agent binds, and the FastMCP surface `studio mcp` serves. One contract on
both: with a `k_source` the registered `search` takes no `k`, every call reads the cap afresh, and
the description drops every sentence that taught the argument. Without one the tools are exactly
what they were, `k` and its default included.

The handler fakes and canned rows are the toolset suite's own (`test_tools`), so a change to what a
handler is reaches both files.
"""

from __future__ import annotations

import inspect
import json

from typing import TYPE_CHECKING

import pytest

from mcp.server.fastmcp import FastMCP
from mcp.shared.memory import create_connected_server_and_client_session as connect_in_memory

from engram.retrieval.domain import SearchResult
from engram.retrieval.tools import TOOL_MAX_RETRIES, build_retrieval_toolset, register_mcp_tools
from tests.unit.engram.retrieval.test_tools import FakeSearchHandler, _call, _handlers, _search_rows, _text

if TYPE_CHECKING:
    from mcp.types import Tool
    from pydantic_ai.toolsets import AbstractToolset

    from engram.retrieval.tools import RetrievalToolset


_COLLECTION = "workdocs"
"""The made-up collection every door in this module is built over: there is no default one."""


def _params(toolset: RetrievalToolset) -> dict[str, inspect.Parameter]:
    """The `search` tool's parameters as the agent's schema is derived from them."""
    return dict(inspect.signature(toolset.tools["search"].function).parameters)


async def _served_search(server: FastMCP) -> Tool:
    """The published `search` tool, as an MCP client listing the server's surface sees it."""
    async with connect_in_memory(server) as client:
        await client.initialize()
        listing = await client.list_tools()
    [tool] = [published for published in listing.tools if published.name == "search"]
    return tool


class TestToolsetUnderAHostCap:
    async def test_search_offers_the_query_and_no_k(self):
        toolset = build_retrieval_toolset(_handlers(), k_source=lambda: 3, collection=_COLLECTION)

        assert "query" in _params(toolset)
        assert "k" not in _params(toolset)

    async def test_each_call_resolves_the_cap_afresh(self):
        search = FakeSearchHandler(result=_search_rows())
        cap = {"k": 5}
        toolset = build_retrieval_toolset(_handlers(search=search), k_source=lambda: cap["k"], collection=_COLLECTION)

        await _call(toolset, "search", {"query": "graph rag"})
        cap["k"] = 2
        await _call(toolset, "search", {"query": "graph rag"})

        assert [message.k for message in search.calls] == [5, 2]

    async def test_the_agent_still_chooses_the_leg_when_the_mode_is_not_forced(self):
        # The cap is the only thing the host takes: an unforced leg stays the agent's call.
        search = FakeSearchHandler(result=_search_rows())
        toolset = build_retrieval_toolset(_handlers(search=search), k_source=lambda: 3, collection=_COLLECTION)

        await _call(toolset, "search", {"query": "graph rag", "bm25_only": True})

        (message,) = search.calls
        assert message.hybrid is False
        assert message.k == 3

    @pytest.mark.parametrize("search_hybrid", [True, False])
    async def test_a_forced_leg_leaves_the_query_collection_date_range_and_facets_as_the_only_arguments(self, search_hybrid: bool):
        search = FakeSearchHandler(result=_search_rows())
        toolset = build_retrieval_toolset(_handlers(search=search), search_hybrid=search_hybrid, k_source=lambda: 4, collection=_COLLECTION)

        assert set(_params(toolset)) == {"query", "collection", "since", "until", "facets"}
        await _call(toolset, "search", {"query": "graph rag"})
        (message,) = search.calls
        assert message.hybrid is search_hybrid
        assert message.k == 4

    async def test_the_hits_are_projected_exactly_as_the_uncapped_tool_projects_them(self):
        toolset = build_retrieval_toolset(
            _handlers(search=FakeSearchHandler(result=_search_rows())), k_source=lambda: 3, collection=_COLLECTION
        )

        result = await _call(toolset, "search", {"query": "graph rag"})

        assert isinstance(result, SearchResult)
        assert [hit.id for hit in result.hits] == ["paper.lightrag-2025#s2", "paper.graphrag-2024"]

    async def test_a_host_owned_cap_still_carries_next_steps(self):
        toolset = build_retrieval_toolset(
            _handlers(search=FakeSearchHandler(result=_search_rows())), k_source=lambda: 3, collection=_COLLECTION
        )

        result = await _call(toolset, "search", {"query": "graph rag"})

        assert isinstance(result, SearchResult)
        assert "fetch" in " ".join(result.next_steps.options)

    async def test_the_other_three_tools_are_untouched(self):
        toolset = build_retrieval_toolset(_handlers(), k_source=lambda: 3, collection=_COLLECTION)

        assert set(toolset.tools) == {"search", "fetch", "graph_expand", "find_paths"}

    async def test_the_capped_search_keeps_the_shared_retry_budget(self):
        # The capped search is registered by its own helper — it must not be the one tool left at
        # pydantic-ai's fragile default of 1.
        toolset = build_retrieval_toolset(_handlers(), k_source=lambda: 3, collection=_COLLECTION)

        assert toolset.tools["search"].max_retries == TOOL_MAX_RETRIES

    async def test_a_search_only_profile_still_gets_the_capped_search(self):
        toolset = build_retrieval_toolset(_handlers(), only=frozenset({"search"}), k_source=lambda: 3, collection=_COLLECTION)

        assert set(toolset.tools) == {"search"}
        assert "k" not in _params(toolset)

    async def test_a_profile_without_search_registers_no_search_at_all(self):
        toolset = build_retrieval_toolset(_handlers(), only=frozenset({"fetch"}), k_source=lambda: 3, collection=_COLLECTION)

        assert set(toolset.tools) == {"fetch"}

    @pytest.mark.parametrize("search_hybrid", [None, True, False])
    async def test_the_description_never_mentions_k(self, search_hybrid: bool | None):
        toolset = build_retrieval_toolset(_handlers(), search_hybrid=search_hybrid, k_source=lambda: 3, collection=_COLLECTION)

        description = toolset.tools["search"].description or ""
        assert "`k`" not in description
        assert "caps the hits" not in description
        assert "ranked hits" in description, "everything else the description taught survives"

    async def test_a_tuning_override_is_elided_too(self):
        # A promoted policy's text is still a description the agent reads; it cannot offer `k` either.
        toolset = build_retrieval_toolset(
            _handlers(),
            descriptions={"search": "Search: returns the top-`k` ranked hits. `k` caps the hits (default 12)."},
            k_source=lambda: 3,
            collection=_COLLECTION,
        )

        assert toolset.tools["search"].description == "Search: returns the top ranked hits."


class TestMcpSurfaceUnderAHostCap:
    async def test_the_served_search_takes_no_k(self):
        server = FastMCP(name="t")
        register_mcp_tools(server, _handlers(), k_source=lambda: 3, collection=_COLLECTION)

        properties = (await _served_search(server)).inputSchema["properties"]
        assert "query" in properties
        assert "k" not in properties

    async def test_each_call_resolves_the_cap_afresh_and_returns_the_hits(self):
        search = FakeSearchHandler(result=_search_rows())
        cap = {"k": 6}
        server = FastMCP(name="t")
        register_mcp_tools(server, _handlers(search=search), k_source=lambda: cap["k"], collection=_COLLECTION)

        async with connect_in_memory(server) as client:
            await client.initialize()
            first = await client.call_tool("search", {"query": "graph rag"})
            cap["k"] = 1
            await client.call_tool("search", {"query": "graph rag"})

        assert first.isError is False
        payload = json.loads(_text(first))
        assert [hit["id"] for hit in payload["hits"]] == ["paper.lightrag-2025#s2", "paper.graphrag-2024"]
        assert [message.k for message in search.calls] == [6, 1]

    async def test_a_forced_leg_leaves_the_query_collection_date_range_and_facets_as_the_only_arguments(self):
        server = FastMCP(name="t")
        register_mcp_tools(server, _handlers(), search_hybrid=True, k_source=lambda: 4, collection=_COLLECTION)

        assert set((await _served_search(server)).inputSchema["properties"]) == {"query", "collection", "since", "until", "facets"}

    async def test_the_served_description_never_mentions_k(self):
        server = FastMCP(name="t")
        register_mcp_tools(server, _handlers(), k_source=lambda: 3, collection=_COLLECTION)

        description = (await _served_search(server)).description or ""
        assert "`k`" not in description
        assert "caps the hits" not in description

    async def test_the_other_three_tools_are_still_served(self):
        server = FastMCP(name="t")
        register_mcp_tools(server, _handlers(), k_source=lambda: 3, collection=_COLLECTION)

        async with connect_in_memory(server) as client:
            await client.initialize()
            listing = await client.list_tools()
        assert {"search", "fetch", "graph_expand", "find_paths"} <= {tool.name for tool in listing.tools}


class TestWithoutAHostCap:
    """The default is byte-identical to what every existing host already serves."""

    async def test_the_toolset_search_keeps_the_agent_argued_k(self):
        toolset = build_retrieval_toolset(_handlers(), collection=_COLLECTION)

        assert _params(toolset)["k"].default == 12

    async def test_the_served_search_keeps_the_agent_argued_k(self):
        server = FastMCP(name="t")
        register_mcp_tools(server, _handlers(), collection=_COLLECTION)

        assert (await _served_search(server)).inputSchema["properties"]["k"]["default"] == 12

    async def test_the_description_still_teaches_k(self):
        toolset = build_retrieval_toolset(_handlers(), collection=_COLLECTION)

        assert "`k` caps the hits (default 12)" in (toolset.tools["search"].description or "")

    async def test_the_agents_k_still_reaches_the_handler(self):
        search = FakeSearchHandler(result=_search_rows())
        toolset: AbstractToolset[None] = build_retrieval_toolset(_handlers(search=search), collection=_COLLECTION)

        await _call(toolset, "search", {"query": "graph rag", "k": 3})

        assert [message.k for message in search.calls] == [3]
