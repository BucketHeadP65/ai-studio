"""The auto-mode retrieval-switches seam (`build_retrieval_toolset` / `register_mcp_tools`).

Both toolset builders take an optional `switches`: the knowledge base's resolved `RetrievalSwitches` for an
auto-mode knowledge base at build time; `None` (the default) is every fixed-mode knowledge base, which never
consults it. `search_hybrid=None` over a knowledge base whose embeddings switch is off is forced
keyword-only. `graph_expand`/`find_paths` are withheld when the graph switch is off, whether `only`
is `None` or a profile that names them. An explicit `search_hybrid` is the caller's own ask and
always wins over the switch.

The handler fakes and canned rows are the toolset suite's own (`test_tools`), so a change to what a
handler is reaches both files.
"""

from __future__ import annotations

import inspect
import re

from typing import TYPE_CHECKING

import pytest

from core.ai.retrieval.strategy import EVAL_STRATEGY_NAMES, resolve_strategy
from mcp.client import Client
from mcp.server.mcpserver import MCPServer

from engram.indexing.contracts import RetrievalSwitches
from engram.retrieval.tools import FETCH, FIND_PATHS, GRAPH_EXPAND, SEARCH, build_retrieval_toolset, register_mcp_tools
from tests.unit.engram.retrieval.test_tools import FakeSearchHandler, _call, _handlers, _search_rows

if TYPE_CHECKING:
    from engram.retrieval.tools import RetrievalToolset


_COLLECTION = "workdocs"
"""The made-up collection every door in this module is built over: there is no default one."""


def _search_params(toolset: RetrievalToolset) -> dict[str, inspect.Parameter]:
    """The `search` tool's parameters as the agent's schema is derived from them."""
    return dict(inspect.signature(toolset.tools["search"].function).parameters)


class TestSwitchesGateTheSearchLeg:
    async def test_a_fixed_mode_knowledge_base_never_consults_switches(self):
        # `switches=None` (the default) is every fixed-mode knowledge_base: behavior is untouched.
        toolset = build_retrieval_toolset(_handlers(), collection=_COLLECTION)

        assert "bm25_only" in _search_params(toolset)

    async def test_an_auto_knowledge_base_with_the_switch_off_runs_lexical_with_no_bm25_arg(self):
        search = FakeSearchHandler(result=_search_rows())
        toolset = build_retrieval_toolset(_handlers(search=search), switches=RetrievalSwitches(embeddings=False), collection=_COLLECTION)

        assert "bm25_only" not in _search_params(toolset)
        await _call(toolset, "search", {"query": "graph rag"})

        (message,) = search.calls
        assert message.hybrid is False

    async def test_an_auto_knowledge_base_with_the_switch_on_keeps_agent_control(self):
        search = FakeSearchHandler(result=_search_rows())
        toolset = build_retrieval_toolset(_handlers(search=search), switches=RetrievalSwitches(embeddings=True), collection=_COLLECTION)

        assert "bm25_only" in _search_params(toolset)
        await _call(toolset, "search", {"query": "graph rag", "bm25_only": True})

        (message,) = search.calls
        assert message.hybrid is False  # the agent still chooses; the leg genuinely exists

    async def test_an_explicit_search_hybrid_true_wins_over_an_off_switch(self):
        search = FakeSearchHandler(result=_search_rows())
        toolset = build_retrieval_toolset(
            _handlers(search=search), search_hybrid=True, switches=RetrievalSwitches(embeddings=False), collection=_COLLECTION
        )

        await _call(toolset, "search", {"query": "graph rag"})

        (message,) = search.calls
        assert message.hybrid is True

    async def test_an_explicit_search_hybrid_false_is_unaffected_by_an_on_switch(self):
        search = FakeSearchHandler(result=_search_rows())
        toolset = build_retrieval_toolset(
            _handlers(search=search), search_hybrid=False, switches=RetrievalSwitches(embeddings=True), collection=_COLLECTION
        )

        await _call(toolset, "search", {"query": "graph rag"})

        (message,) = search.calls
        assert message.hybrid is False


class TestSwitchesGateTheGraphTools:
    async def test_a_fixed_mode_knowledge_base_keeps_them_regardless(self):
        toolset = build_retrieval_toolset(_handlers(), collection=_COLLECTION)

        assert set(toolset.tools) == {"search", "fetch", "graph_expand", "find_paths"}

    async def test_an_auto_knowledge_base_with_the_graph_switch_off_drops_the_graph_tools(self):
        toolset = build_retrieval_toolset(_handlers(), switches=RetrievalSwitches(graph=False), collection=_COLLECTION)

        assert set(toolset.tools) == {"search", "fetch"}

    async def test_an_auto_knowledge_base_with_the_graph_switch_on_keeps_them(self):
        toolset = build_retrieval_toolset(_handlers(), switches=RetrievalSwitches(graph=True), collection=_COLLECTION)

        assert set(toolset.tools) == {"search", "fetch", "graph_expand", "find_paths"}

    async def test_a_profile_naming_the_graph_tools_loses_them_to_an_off_switch(self):
        toolset = build_retrieval_toolset(
            _handlers(),
            only=frozenset({"search", "fetch", "graph_expand", "find_paths"}),
            switches=RetrievalSwitches(graph=False),
            collection=_COLLECTION,
        )

        assert set(toolset.tools) == {"search", "fetch"}

    async def test_a_search_and_fetch_only_profile_is_unaffected_either_way(self):
        toolset = build_retrieval_toolset(
            _handlers(), only=frozenset({"search", "fetch"}), switches=RetrievalSwitches(graph=True), collection=_COLLECTION
        )

        assert set(toolset.tools) == {"search", "fetch"}


class TestMcpSurfaceSwitches:
    """The MCP registration path (`register_mcp_tools`) follows the same two rules."""

    async def test_the_served_search_offers_no_bm25_arg_when_the_switch_is_off(self):
        server = MCPServer(name="t")
        register_mcp_tools(server, _handlers(), switches=RetrievalSwitches(embeddings=False), collection=_COLLECTION)

        async with Client(server) as client:
            listing = await client.list_tools()
        [search] = [tool for tool in listing.tools if tool.name == "search"]
        assert "bm25_only" not in search.input_schema["properties"]

    async def test_the_served_search_keeps_bm25_when_the_switch_is_on(self):
        server = MCPServer(name="t")
        register_mcp_tools(server, _handlers(), switches=RetrievalSwitches(embeddings=True), collection=_COLLECTION)

        async with Client(server) as client:
            listing = await client.list_tools()
        [search] = [tool for tool in listing.tools if tool.name == "search"]
        assert "bm25_only" in search.input_schema["properties"]

    async def test_the_graph_tools_are_withheld_when_off(self):
        server = MCPServer(name="t")
        register_mcp_tools(server, _handlers(), switches=RetrievalSwitches(graph=False), collection=_COLLECTION)

        async with Client(server) as client:
            listing = await client.list_tools()
        assert {tool.name for tool in listing.tools} == {"search", "fetch"}

    async def test_the_graph_tools_are_withheld_when_off_even_when_only_names_them(self):
        server = MCPServer(name="t")
        register_mcp_tools(
            server,
            _handlers(),
            only=frozenset({"search", "fetch", "graph_expand", "find_paths"}),
            switches=RetrievalSwitches(graph=False),
            collection=_COLLECTION,
        )

        async with Client(server) as client:
            listing = await client.list_tools()
        assert {tool.name for tool in listing.tools} == {"search", "fetch"}

    async def test_the_graph_tools_are_served_when_on(self):
        server = MCPServer(name="t")
        register_mcp_tools(server, _handlers(), switches=RetrievalSwitches(graph=True), collection=_COLLECTION)

        async with Client(server) as client:
            listing = await client.list_tools()
        assert {tool.name for tool in listing.tools} == {"search", "fetch", "graph_expand", "find_paths"}

    async def test_a_fixed_mode_server_keeps_all_four_tools(self):
        server = MCPServer(name="t")
        register_mcp_tools(server, _handlers(), collection=_COLLECTION)

        async with Client(server) as client:
            listing = await client.list_tools()
        assert {tool.name for tool in listing.tools} == {"search", "fetch", "graph_expand", "find_paths"}


_CANONICAL_TOOL_NAMES = (SEARCH, FETCH, GRAPH_EXPAND, FIND_PATHS)
_BACKTICKED_TOOL = re.compile(r"`(" + "|".join(_CANONICAL_TOOL_NAMES) + r")\b")


def _mentioned_tools(description: str) -> set[str]:
    """Every canonical tool name backtick-mentioned in `description`.

    An arg clause like `` `fetch(unit_id=...)` `` still counts as a mention of `fetch`: the pattern
    matches the tool name at the start of a backticked span, not the whole span.
    """
    return set(_BACKTICKED_TOOL.findall(description))


class TestRetrievalProfilesAreDescribedTruthfully:
    """Every `RetrievalStrategy`'s resolved profile registers a toolset described only for its own tools.

    A description that tells the agent to ground an id with `search`, or offers `fetch` as a next
    move, is a broken orientation when the resolved profile never registered that tool — this is the
    contract `core.ai.retrieval.strategy` and `build_retrieval_toolset` must never drift apart on.
    `code-mode` is excluded from `EVAL_STRATEGY_NAMES` (and so from here) because it is not a
    toolset-builder-shaped profile: the `run_code` meta-tool has no description of its own here.
    """

    @pytest.mark.parametrize("name", EVAL_STRATEGY_NAMES)
    async def test_every_strategy_registers_exactly_its_resolved_tools(self, name: str):
        profile = resolve_strategy(name)

        toolset = build_retrieval_toolset(_handlers(), only=profile.tools, search_hybrid=profile.search_hybrid, collection=_COLLECTION)

        assert set(toolset.tools) == profile.tools

    @pytest.mark.parametrize("name", EVAL_STRATEGY_NAMES)
    async def test_every_description_only_mentions_tools_the_strategy_holds(self, name: str):
        profile = resolve_strategy(name)

        toolset = build_retrieval_toolset(_handlers(), only=profile.tools, search_hybrid=profile.search_hybrid, collection=_COLLECTION)

        for tool_name, tool in toolset.tools.items():
            mentioned = _mentioned_tools(tool.description or "")
            assert mentioned <= profile.tools, f"{name}/{tool_name} names {mentioned - profile.tools}, outside its own profile"

    async def test_graph_only_registers_fetch_but_no_search(self):
        profile = resolve_strategy("graph-only")

        toolset = build_retrieval_toolset(_handlers(), only=profile.tools, search_hybrid=profile.search_hybrid, collection=_COLLECTION)

        assert "fetch" in toolset.tools
        assert "search" not in toolset.tools

    async def test_graph_only_descriptions_ground_ids_from_the_task_not_search(self):
        profile = resolve_strategy("graph-only")

        toolset = build_retrieval_toolset(_handlers(), only=profile.tools, search_hybrid=profile.search_hybrid, collection=_COLLECTION)

        for name in ("graph_expand", "find_paths"):
            description = toolset.tools[name].description or ""
            assert "Use endpoint ids supplied with the task" in description
            assert "search" not in description

    async def test_full_and_keyword_graph_teach_search_then_graph_then_fetch_provenance(self):
        for name in ("full", "keyword-graph"):
            profile = resolve_strategy(name)
            toolset = build_retrieval_toolset(_handlers(), only=profile.tools, search_hybrid=profile.search_hybrid, collection=_COLLECTION)

            assert "search" in toolset.tools
            assert "fetch" in toolset.tools
            assert "`search`" in (toolset.tools["graph_expand"].description or "")
            assert "`search`" in (toolset.tools["find_paths"].description or "")
            assert "provenance_claim" in (toolset.tools["graph_expand"].description or "")
            assert "provenance_claim" in (toolset.tools["find_paths"].description or "")
            assert "`fetch`" in (toolset.tools["search"].description or "")
