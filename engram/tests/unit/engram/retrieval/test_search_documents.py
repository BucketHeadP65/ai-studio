"""Every `search` shows a hit's folded units with the refs that fetch them, and says each hit is a distinct document.

`api.search` keeps the best hit of each document and lists the document's next matching units under it as
`folded_units`, each an id and a hit level. The projection gives each folded unit the ref a hit of its own level would
carry, qualified by the collection it came from, on the single-base search, the routed search and the capability's
search alike. Every search description tells the agent that each hit is a distinct document and names `folded_units`.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from pydantic_ai import FunctionToolset

from engram.retrieval.capability import RetrievalKnowledgeBase
from engram.retrieval.domain import FoldedUnit, SearchResult, SqlResult
from engram.retrieval.tools import (
    SEARCH_DESCRIPTION,
    SEARCH_DESCRIPTION_HYBRID,
    SEARCH_DESCRIPTION_KEYWORD,
    add_routed_read_tools,
    build_retrieval_toolset,
    search_description,
    search_hits,
)
from tests.unit.engram.retrieval.test_retrieval_knowledge_base import RESEARCH, WORKDOCS, _surface
from tests.unit.engram.retrieval.test_tools import FakeSearchHandler, _call, _handlers

if TYPE_CHECKING:
    from engram.retrieval.tools import RetrievalToolset

_DOCUMENT_CLAUSE = "Each hit is a distinct document"
_FOLDED_ROWS = (
    {
        "id": "note.alpha",
        "hit_level": "card",
        "page_id": "note.alpha",
        "page_title": "Alpha",
        "snippet": "the card",
        "score": 0.9,
        "folded_units": [
            {"id": "note.alpha#s1", "hit_level": "section"},
            {"id": "note.alpha#c1", "hit_level": "claim"},
            {"id": "src.alpha:0", "hit_level": "raw"},
        ],
    },
    {"id": "note.beta", "hit_level": "card", "page_id": "note.beta", "page_title": "Beta", "snippet": "a card", "score": 0.5, "folded_units": []},
)
_COLUMNS = ("id", "hit_level", "page_id", "page_title", "snippet", "score", "folded_units")


def _folding_search() -> FakeSearchHandler:
    """A search answering one hit that folds three units and one hit that folds none."""
    return FakeSearchHandler(result=SqlResult(rows=_FOLDED_ROWS, columns=_COLUMNS))


def _expected(collection: str) -> tuple[FoldedUnit, ...]:
    """The three folded units of the first row, each carrying the ref its level fetches by in `collection`."""
    return (
        FoldedUnit(id="note.alpha#s1", hit_level="section", ref=f"section:{collection}/note.alpha#s1"),
        FoldedUnit(id="note.alpha#c1", hit_level="claim", ref=f"unit:{collection}/note.alpha#c1"),
        FoldedUnit(id="src.alpha:0", hit_level="raw", ref=f"unit:{collection}/src.alpha:0"),
    )


class TestTheProjection:
    def test_each_folded_unit_carries_the_ref_a_hit_of_its_level_would_carry(self):
        hits = search_hits(_FOLDED_ROWS, collection="workdocs", knowledge_base="workdocs", hybrid=False)

        assert hits[0].folded_units == _expected("workdocs")
        assert hits[1].folded_units == ()

    def test_a_row_with_no_folded_units_column_folds_nothing(self):
        row = {key: value for key, value in _FOLDED_ROWS[0].items() if key != "folded_units"}

        (hit,) = search_hits((row,), collection="workdocs", knowledge_base="workdocs", hybrid=False)

        assert hit.folded_units == ()


class TestEverySearchShowsTheFoldedUnits:
    async def test_the_single_base_search(self):
        toolset = build_retrieval_toolset(_handlers(search=_folding_search()), collection="workdocs")

        result = await _call(toolset, "search", {"query": "alpha"})

        assert isinstance(result, SearchResult)
        assert result.hits[0].folded_units == _expected("workdocs")

    async def test_the_routed_search_qualifies_each_ref_by_the_base_it_came_from(self):
        toolset: RetrievalToolset = FunctionToolset()
        add_routed_read_tools(
            toolset,
            (_surface("workdocs", "workdocs", WORKDOCS), _surface("research", "research", RESEARCH, search=_folding_search())),
            default_knowledge_base="workdocs",
        )

        result = await _call(toolset, "search", {"query": "alpha", "collection": "research"})

        assert isinstance(result, SearchResult)
        assert result.hits[0].folded_units == _expected("research")

    async def test_the_capabilitys_search(self):
        knowledge_base = RetrievalKnowledgeBase[None](surfaces=(_surface("research", "research", RESEARCH, search=_folding_search()),))

        result = await knowledge_base.direct_call_tool("search", {"query": "alpha"})

        assert isinstance(result, SearchResult)
        assert result.hits[0].folded_units == _expected("research")


class TestEveryDescriptionSaysEachHitIsADistinctDocument:
    @pytest.mark.parametrize("description", [SEARCH_DESCRIPTION, SEARCH_DESCRIPTION_HYBRID, SEARCH_DESCRIPTION_KEYWORD], ids=["either_leg", "hybrid", "keyword"])
    def test_the_shipped_descriptions(self, description: str):
        assert _DOCUMENT_CLAUSE in description
        assert "`folded_units`" in description

    @pytest.mark.parametrize("hybrid", [True, False])
    def test_the_description_a_host_fixes_the_leg_of(self, hybrid: bool):
        description = search_description(hybrid=hybrid, can_sql=False, facet_keys=("shade",))

        assert _DOCUMENT_CLAUSE in description
        assert "`folded_units`" in description

    async def test_the_several_base_description(self):
        knowledge_base = RetrievalKnowledgeBase[None](
            surfaces=(_surface("workdocs", "workdocs", WORKDOCS), _surface("research", "research", RESEARCH))
        )

        [tool] = [published for published in await knowledge_base.list_tools() if published.name == "search"]

        assert _DOCUMENT_CLAUSE in (tool.description or "")
        assert "`folded_units`" in (tool.description or "")
