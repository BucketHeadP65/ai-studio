"""`search` narrows by each page's page date: `since` and `until` on every door that serves it.

A period is a year, a month or a day. `since` opens on the period's first day and `until` closes on
its last, so `until="2026-09"` keeps all of September. A value that names no real period is refused
while the arguments are read, before any handler runs. The single-base toolset, the MCP server, the
routed tools and the capability all hand the handler the same two dates, and a dated search that
finds nothing names the range as the lever to widen.
"""

from __future__ import annotations

import json

from datetime import date
from typing import TYPE_CHECKING

import pytest

from mcp.server.fastmcp import FastMCP
from mcp.shared.memory import create_connected_server_and_client_session as connect_in_memory
from pydantic import ValidationError
from pydantic_ai import FunctionToolset

from engram.retrieval.capability import RetrievalKnowledgeBase
from engram.retrieval.domain import SearchResult, period_end, period_start
from engram.retrieval.tools import DATE_RANGE_CLAUSE, add_routed_read_tools, build_retrieval_toolset, register_mcp_tools, search_next_steps
from tests.unit.engram.retrieval.test_retrieval_knowledge_base import WORKDOCS, FakeSearch, _surface
from tests.unit.engram.retrieval.test_tools import FakeSearchHandler, _call, _ctx, _handlers, _search_rows, _text

if TYPE_CHECKING:
    from engram.retrieval.tools import RetrievalToolset

_COLLECTION = "workdocs"


class TestPeriods:
    @pytest.mark.parametrize(
        ("period", "first", "last"),
        [
            ("2026", date(2026, 1, 1), date(2026, 12, 31)),
            ("2026-09", date(2026, 9, 1), date(2026, 9, 30)),
            ("2024-02", date(2024, 2, 1), date(2024, 2, 29)),
            ("2026-09-14", date(2026, 9, 14), date(2026, 9, 14)),
        ],
    )
    def test_a_period_opens_on_its_first_day_and_closes_on_its_last(self, period: str, first: date, last: date):
        assert (period_start(period), period_end(period)) == (first, last)

    @pytest.mark.parametrize("period", ["26", "2026-9", "2026-13", "2026-02-30", "last month", "2026-09-14T10:00"])
    def test_anything_that_is_not_a_real_year_month_or_day_is_refused_with_the_three_shapes(self, period: str):
        with pytest.raises(ValueError, match=r"Write a year \(2026\), a month \(2026-09\) or a day \(2026-09-14\)"):
            period_start(period)


class TestToolset:
    async def test_the_range_reaches_the_handler_as_the_first_and_last_day(self):
        search = FakeSearchHandler(result=_search_rows())
        toolset = build_retrieval_toolset(_handlers(search=search), collection=_COLLECTION)

        await _call(toolset, "search", {"query": "brine", "since": "2026-09", "until": "2026"})

        (message,) = search.calls
        assert (message.since, message.until) == (date(2026, 9, 1), date(2026, 12, 31))

    async def test_an_undated_search_sends_no_range(self):
        search = FakeSearchHandler(result=_search_rows())
        toolset = build_retrieval_toolset(_handlers(search=search), collection=_COLLECTION)

        await _call(toolset, "search", {"query": "brine"})

        (message,) = search.calls
        assert (message.since, message.until) == (None, None)

    async def test_a_pinned_search_hands_the_range_to_the_handler_and_still_reads_its_own_collection(self):
        search = FakeSearchHandler(result=_search_rows())
        toolset = build_retrieval_toolset(_handlers(search=search), collection=_COLLECTION, pinned=True)

        await _call(toolset, "search", {"query": "brine", "collection": "elsewhere", "since": "2026-09", "until": "2026"})

        (message,) = search.calls
        assert (message.collection, message.since, message.until) == (_COLLECTION, date(2026, 9, 1), date(2026, 12, 31))

    async def test_an_impossible_day_is_refused_while_the_arguments_are_read(self):
        toolset = build_retrieval_toolset(_handlers(), collection=_COLLECTION)
        tools = await toolset.get_tools(_ctx())

        with pytest.raises(ValidationError, match="names no real year, month or day"):
            tools["search"].args_validator.validate_python({"query": "brine", "until": "2026-02-30"})

    @pytest.mark.parametrize("pinned", [False, True])
    async def test_every_description_teaches_the_range(self, pinned: bool):
        for search_hybrid in (None, True, False):
            toolset = build_retrieval_toolset(_handlers(), search_hybrid=search_hybrid, collection=_COLLECTION, pinned=pinned)

            assert DATE_RANGE_CLAUSE in (toolset.tools["search"].description or "")


class TestMcpServer:
    async def test_the_served_range_carries_its_shape_and_reaches_the_handler(self):
        search = FakeSearchHandler(result=_search_rows())
        server = FastMCP(name="t")
        register_mcp_tools(server, _handlers(search=search), collection=_COLLECTION)

        async with connect_in_memory(server) as client:
            await client.initialize()
            listing = await client.list_tools()
            answered = await client.call_tool("search", {"query": "brine", "since": "2026-09-14"})
            refused = await client.call_tool("search", {"query": "brine", "since": "2026-13"})

        [tool] = [published for published in listing.tools if published.name == "search"]
        assert {"since", "until"} <= set(tool.inputSchema["properties"])
        assert answered.isError is False
        assert refused.isError is True
        assert "names no real year, month or day" in _text(refused)
        assert [message.since for message in search.calls] == [date(2026, 9, 14)]


class TestRoutedTools:
    async def test_the_routed_search_hands_the_range_to_the_base_it_routes_to(self):
        kitchen = FakeSearch()
        toolset: RetrievalToolset = FunctionToolset()
        add_routed_read_tools(toolset, (_surface("kitchen", "kitchen", WORKDOCS, search=kitchen),), default_knowledge_base="kitchen")

        await _call(toolset, "search", {"query": "brine", "until": "2026-09"})

        assert [(message.since, message.until) for message in kitchen.seen] == [(None, date(2026, 9, 30))]


class TestCapability:
    async def test_one_base_hands_the_range_to_its_handler(self):
        kitchen = FakeSearch()
        knowledge_base = RetrievalKnowledgeBase[None](surfaces=(_surface("kitchen", "kitchen", WORKDOCS, search=kitchen),))

        await knowledge_base.direct_call_tool("search", {"query": "brine", "since": "2026"})

        assert [(message.since, message.until) for message in kitchen.seen] == [(date(2026, 1, 1), None)]

    async def test_several_bases_each_get_the_same_range(self):
        kitchen, wiki = FakeSearch(), FakeSearch()
        knowledge_base = RetrievalKnowledgeBase[None](
            surfaces=(_surface("kitchen", "kitchen", WORKDOCS, search=kitchen), _surface("wiki", "wiki", WORKDOCS, search=wiki)),
            cross_search=True,
        )

        result = await knowledge_base.direct_call_tool("search", {"query": "brine", "since": "2026-09", "until": "2026-09"})

        assert isinstance(result, SearchResult)
        september = (date(2026, 9, 1), date(2026, 9, 30))
        assert [(message.since, message.until) for message in (*kitchen.seen, *wiki.seen)] == [september, september]
        assert "`since` and `until`" in result.next_steps.options[0]

    def test_the_description_over_several_bases_teaches_the_range(self):
        knowledge_base = RetrievalKnowledgeBase[None](
            surfaces=(_surface("kitchen", "kitchen", WORKDOCS), _surface("wiki", "wiki", WORKDOCS)), cross_search=True
        )

        assert "`since` and `until`" in knowledge_base._search_description()


class TestNoHits:
    def test_a_dated_search_that_finds_nothing_names_the_range_as_the_lever(self):
        steps = search_next_steps((), query="brine", knowledge_base="kitchen", hybrid=True, status="no_hits", map_collection=None, dated=True)

        assert steps.options == (
            "Try broader or different terms, or widen or drop `since` and `until`: a page with no page date never matches a range.",
        )

    def test_an_undated_search_that_finds_nothing_names_only_the_query(self):
        steps = search_next_steps((), query="brine", knowledge_base="kitchen", hybrid=True, status="no_hits", map_collection=None)

        assert steps.options[0] == "Try broader or different terms."

    async def test_a_pinned_dated_result_that_finds_nothing_names_the_range_as_the_lever(self):
        toolset = build_retrieval_toolset(_handlers(), collection=_COLLECTION, pinned=True)

        result = await _call(toolset, "search", {"query": "brine", "since": "2026"})

        assert isinstance(result, SearchResult)
        assert result.next_steps.options[0] == (
            "Try broader or different terms, or widen or drop `since` and `until`: a page with no page date never matches a range."
        )

    async def test_a_pinned_undated_result_that_finds_nothing_names_only_the_query(self):
        toolset = build_retrieval_toolset(_handlers(), collection=_COLLECTION, pinned=True)

        result = await _call(toolset, "search", {"query": "brine"})

        assert isinstance(result, SearchResult)
        assert result.next_steps.options[0] == "Try broader or different terms."

    async def test_a_dated_result_serializes_its_next_move(self):
        toolset = build_retrieval_toolset(_handlers(), collection=_COLLECTION)

        result = await _call(toolset, "search", {"query": "brine", "since": "2026"})

        assert isinstance(result, SearchResult)
        assert "widen or drop `since` and `until`" in json.dumps(result.model_dump(mode="json"))
