"""The host-owned row cap: when `k_source` is set, `search` stops offering `k` and reads it per call.

Two claims. **The agent never sees the cap** — `k` leaves the tool schema and every sentence of the
description that taught it, so a model cannot spend a budget it was not given. **The cap is live** —
each call resolves `k_source()` afresh, so a cap that moves under a running host lands on the next
call rather than the next boot, and with several knowledge bases the resolved value bounds the merged list
too. Without a `k_source` the tool is exactly what it was: `k`, defaulted to 12, argued by the agent.

The fakes and surface builder are the read-tools suite's own (`test_retrieval_knowledge_base`), so a change
to what a surface is reaches both files.
"""

from __future__ import annotations

from dataclasses import replace
from typing import TYPE_CHECKING

import pytest

from engram.retrieval.capability import RetrievalKnowledgeBase
from engram.retrieval.domain import SearchResult
from tests.unit.engram.retrieval.test_retrieval_knowledge_base import FakeSearch, _hit, _surface

if TYPE_CHECKING:
    from collections.abc import Callable

    from mcp import types as mcp_types

WORKDOCS = "Working notes for a pickling kitchen: brine logs, ferment hubs, lifecycle docs."
RESEARCH = "A research wiki on fermentation science, indexed into claims with provenance."


def _one(*, search: FakeSearch | None = None, hybrid: bool = True, k_source: Callable[[], int] | None = None) -> RetrievalKnowledgeBase[None]:
    """One knowledge base, its search leg fixed, under an optional host-owned cap."""
    surface = replace(_surface("kitchen", "kitchen", WORKDOCS, search=search), hybrid=hybrid)
    return RetrievalKnowledgeBase[None](surfaces=(surface,), k_source=k_source)


def _several(
    *, kitchen: FakeSearch | None = None, wiki: FakeSearch | None = None, k_source: Callable[[], int] | None = None
) -> RetrievalKnowledgeBase[None]:
    """Two knowledge bases behind the same two tools, under an optional host-owned cap.

    `cross_search` is on so an omitted `knowledge_base` still fans out: the claim under test is the cap,
    and a merged result set is what the cap has to bound.
    """
    return RetrievalKnowledgeBase[None](
        surfaces=(_surface("kitchen", "kitchen", WORKDOCS, search=kitchen), _surface("wiki", "wiki", RESEARCH, search=wiki)),
        k_source=k_source,
        cross_search=True,
    )


async def _search(knowledge_base: RetrievalKnowledgeBase[None]) -> mcp_types.Tool:
    """The published `search` tool, as a host reading the knowledge base's tool list sees it."""
    [tool] = [published for published in await knowledge_base.list_tools() if published.name == "search"]
    return tool


class TestOneSurfaceUnderAHostCap:
    async def test_search_offers_the_query_and_no_k(self):
        properties = (await _search(_one(k_source=lambda: 3))).input_schema["properties"]
        assert "query" in properties
        assert "k" not in properties

    async def test_each_call_resolves_the_cap_afresh(self):
        recording = FakeSearch()
        cap = {"k": 5}
        knowledge_base = _one(search=recording, k_source=lambda: cap["k"])
        await knowledge_base.direct_call_tool("search", {"query": "brine"})
        cap["k"] = 2
        await knowledge_base.direct_call_tool("search", {"query": "brine"})
        assert [query.k for query in recording.seen] == [5, 2]

    @pytest.mark.parametrize("hybrid", [True, False])
    async def test_the_description_never_mentions_k(self, hybrid: bool):
        description = (await _search(_one(hybrid=hybrid, k_source=lambda: 3))).description or ""
        assert "`k`" not in description
        assert "caps the hits" not in description
        assert "ranked hits" in description, "everything else the description taught survives"


class TestSeveralSurfacesUnderAHostCap:
    async def test_search_keeps_knowledge_base_and_drops_k(self):
        properties = (await _search(_several(k_source=lambda: 3))).input_schema["properties"]
        assert "knowledge_base" in properties
        assert "k" not in properties

    async def test_the_resolved_cap_bounds_the_merged_hits(self):
        kitchen = FakeSearch(rows=(_hit("mu.brine", 0.9), _hit("mu.jar", 0.2)))
        wiki = FakeSearch(rows=(_hit("paper.lacto", 0.7),))
        knowledge_base = _several(kitchen=kitchen, wiki=wiki, k_source=lambda: 2)
        result = await knowledge_base.direct_call_tool("search", {"query": "brine"})
        assert isinstance(result, SearchResult)
        assert [hit.id for hit in result.hits] == ["mu.brine", "paper.lacto"]
        assert [query.k for query in kitchen.seen] == [2], "and each knowledge base is searched under the same resolved cap"

    async def test_one_named_knowledge_base_is_still_reachable(self):
        wiki = FakeSearch(rows=(_hit("paper.lacto", 0.7),))
        result = await _several(wiki=wiki, k_source=lambda: 4).direct_call_tool("search", {"query": "brine", "knowledge_base": "wiki"})
        assert isinstance(result, SearchResult)
        assert [hit.id for hit in result.hits] == ["paper.lacto"]
        assert [query.k for query in wiki.seen] == [4]

    async def test_the_description_never_mentions_k(self):
        description = (await _search(_several(k_source=lambda: 3))).description or ""
        assert "`k`" not in description
        assert "caps the hits" not in description
        assert "`knowledge_base` limits the search" in description


class TestWithoutAHostCap:
    async def test_one_surface_keeps_the_agent_argued_k(self):
        properties = (await _search(_one())).input_schema["properties"]
        assert properties["k"]["default"] == 12

    async def test_several_surfaces_keep_the_agent_argued_k(self):
        properties = (await _search(_several())).input_schema["properties"]
        assert properties["k"]["default"] == 12
        assert "knowledge_base" in properties

    @pytest.mark.parametrize("hybrid", [True, False])
    async def test_the_description_still_teaches_k(self, hybrid: bool):
        assert "`k` caps the hits (default 12)" in ((await _search(_one(hybrid=hybrid))).description or "")

    async def test_the_library_description_still_teaches_k(self):
        assert "`k` caps the hits (default 12)" in ((await _search(_several())).description or "")

    async def test_the_agents_k_still_reaches_the_surface(self):
        recording = FakeSearch()
        await _one(search=recording).direct_call_tool("search", {"query": "brine", "k": 3})
        assert [query.k for query in recording.seen] == [3]
