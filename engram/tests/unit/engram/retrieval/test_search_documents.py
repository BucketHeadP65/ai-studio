"""Every `search` shows a hit's folded units with the refs that fetch them, and states the cap on hits per document.

`api.search` keeps the best hits of each document and lists the document's next matching units under its best hit as
`folded_units`, each an id, a hit level and a snippet. The projection gives each folded unit the ref a hit of its own
level would carry, qualified by the collection it came from, on the single-base search, the routed search and the
capability's search alike, and bounds its snippet as a hit's. Every search description states the cap the database
applies to the knowledge base, and that folded units carry a snippet: a cap of 1 makes each hit a distinct document, a
cap above 1 allows that many hits of one document, and no cap says nothing about documents. The response budget cuts a
folded unit's snippet before it drops the unit.
"""

from __future__ import annotations

import json

from dataclasses import dataclass, field, replace
from typing import TYPE_CHECKING

import pytest

from core.ai.disclosure.contracts import DisclosureEnvelope
from mcp.client import Client
from mcp.server.mcpserver import MCPServer
from pydantic_ai import FunctionToolset
from sqlalchemy.exc import OperationalError

from engram.retrieval.capability import RetrievalKnowledgeBase
from engram.retrieval.contracts import read_hits_per_document
from engram.retrieval.dials import ReadDials
from engram.retrieval.domain import FetchResult, FoldedUnit, NextSteps, SearchHit, SearchResult, SqlResult
from engram.retrieval.queries import SearchHandler
from engram.retrieval.tools import (
    SEARCH_RESULT_CHARS,
    SEARCH_SNIPPET_CHARS,
    RetrievalHandlers,
    add_routed_read_tools,
    bound_search_result,
    build_retrieval_toolset,
    document_clause,
    register_mcp_tools,
    register_routed_mcp_tools,
    search_description,
    search_hits,
    search_next_steps,
    search_result_status,
)
from engram.testing.fakes.embedding import FakeEmbedding
from engram.testing.fakes.gateway import FakeSqlGateway
from tests.unit.engram.retrieval.test_retrieval_knowledge_base import RESEARCH, WORKDOCS, FakeFlatFetch, _NeverWalks, _surface
from tests.unit.engram.retrieval.test_tools import FakeSearchHandler, _call, _ctx, _envelope, _handlers, _text

if TYPE_CHECKING:
    from collections.abc import Mapping

    from core.ai.retrieval.contracts import SearchResultStatus
    from mcp.types import Tool as MCPTool

    from engram.retrieval.capability import ReadSurface
    from engram.retrieval.dials import FetchDial
    from engram.retrieval.tools import RetrievalToolset

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

    def test_each_folded_unit_keeps_the_snippet_its_row_carries(self):
        row = {**_FOLDED_ROWS[0], "folded_units": [{"id": "note.alpha#s1", "hit_level": "section", "snippet": "The section matched\u2026"}]}

        (hit,) = search_hits((row,), collection="workdocs", knowledge_base="workdocs", hybrid=False)

        assert [unit.snippet for unit in hit.folded_units] == ["The section matched\u2026"]

    def test_a_folded_snippet_longer_than_a_hit_snippet_is_cut_to_it(self):
        row = {**_FOLDED_ROWS[0], "folded_units": [{"id": "note.alpha#s1", "hit_level": "section", "snippet": "y" * 600}]}

        (hit,) = search_hits((row,), collection="workdocs", knowledge_base="workdocs", hybrid=False)

        assert hit.folded_units[0].snippet == "y" * (SEARCH_SNIPPET_CHARS - 1) + "\u2026"


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


_DISTINCT = "Each hit is a distinct document"
_AT_MOST_TWO = "At most 2 hits come from one document"
_SNIPPETED = "each with a short `snippet` of the text it matched"


def _says(description: str, cap: int) -> None:
    """Assert `description` states exactly what a search capped at `cap` hits per document does."""
    if cap == 0:
        assert "document" not in description
        assert "folded_units" not in description
        return
    assert (_DISTINCT in description) is (cap == 1)
    assert (_AT_MOST_TWO in description) is (cap == 2)
    assert "`folded_units`" in description
    assert _SNIPPETED in description


def _description(tools: list[MCPTool]) -> str:
    """The description of the `search` tool among `tools`."""
    [tool] = [published for published in tools if published.name == "search"]
    return tool.description or ""


_CAPS = pytest.mark.parametrize("cap", [1, 2, 0], ids=["one_per_document", "two_per_document", "no_cap"])


class TestTheDocumentClause:
    @_CAPS
    def test_it_states_the_cap(self, cap: int):
        clause = document_clause(cap)

        if cap == 0:
            assert clause == ""
        else:
            _says(clause, cap)


class TestEveryDescriptionStatesTheConfiguredCap:
    @_CAPS
    async def test_the_single_base_toolset(self, cap: int):
        toolset = build_retrieval_toolset(_handlers(), collection="workdocs", hits_per_document=cap)

        tools = await toolset.get_tools(_ctx())

        _says(tools["search"].tool_def.description or "", cap)

    @_CAPS
    async def test_the_single_base_mcp_server(self, cap: int):
        server = MCPServer("documents")
        register_mcp_tools(server, _handlers(), collection="workdocs", hits_per_document=cap)

        _says(_description(await server.list_tools()), cap)

    @_CAPS
    @pytest.mark.parametrize("hybrid", [True, False], ids=["hybrid", "keyword"])
    def test_the_description_a_host_fixes_the_leg_of(self, cap: int, hybrid: bool):
        _says(search_description(hybrid=hybrid, can_sql=False, facet_keys=("shade",), hits_per_document=cap), cap)

    @_CAPS
    async def test_the_routed_search(self, cap: int):
        toolset: RetrievalToolset = FunctionToolset()
        surfaces = (_capped("workdocs", WORKDOCS, cap), _capped("research", RESEARCH, cap))
        add_routed_read_tools(toolset, surfaces, default_knowledge_base="workdocs")

        tools = await toolset.get_tools(_ctx())

        _says(tools["search"].tool_def.description or "", cap)

    @_CAPS
    async def test_the_capability_over_one_base(self, cap: int):
        knowledge_base = RetrievalKnowledgeBase[None](surfaces=(_capped("research", RESEARCH, cap),))

        _says(_description(await knowledge_base.list_tools()), cap)

    @_CAPS
    async def test_the_capability_over_several_bases(self, cap: int):
        knowledge_base = RetrievalKnowledgeBase[None](surfaces=(_capped("workdocs", WORKDOCS, cap), _capped("research", RESEARCH, cap)))

        _says(_description(await knowledge_base.list_tools()), cap)

    async def test_bases_that_disagree_on_the_cap_state_none(self):
        knowledge_base = RetrievalKnowledgeBase[None](surfaces=(_capped("workdocs", WORKDOCS, 1), _capped("research", RESEARCH, 2)))

        _says(_description(await knowledge_base.list_tools()), 0)


def _capped(name: str, domain: str, cap: int) -> ReadSurface:
    """A read surface over `name` whose database caps hits per document at `cap`."""
    return replace(_surface(name, name, domain), hits_per_document=cap)


class TestReadingTheCap:
    async def test_the_search_handler_reads_the_number_the_database_answers_for_its_knowledge_base(self):
        gateway = FakeSqlGateway(routed={"api.search_hits_per_document": SqlResult(rows=({"hits_per_document": 2},), columns=("hits_per_document",))})

        cap = await SearchHandler(gateway, FakeEmbedding()).hits_per_document("workdocs")

        assert cap == 2
        assert gateway.rows_calls[-1].params == {"collection": "workdocs"}

    async def test_a_host_reads_the_cap_of_the_knowledge_base_it_names(self):
        gateway = FakeSqlGateway(routed={"api.search_hits_per_document": SqlResult(rows=({"hits_per_document": 3},), columns=("hits_per_document",))})

        cap = await read_hits_per_document(SearchHandler(gateway, FakeEmbedding()), knowledge_base="the work documents", collection="workdocs")

        assert cap == 3
        assert gateway.rows_calls[-1].params == {"collection": "workdocs"}

    async def test_no_answer_reads_as_no_cap(self):
        assert await SearchHandler(FakeSqlGateway(), FakeEmbedding()).hits_per_document("workdocs") == 0

    @pytest.mark.parametrize("error", [OperationalError("SELECT 1", {}, Exception("gone")), ConnectionRefusedError("refused")], ids=["database", "network"])
    async def test_a_failed_read_states_no_cap(self, error: Exception):
        search = SearchHandler(_FailingGateway(error=error), FakeEmbedding())

        assert await read_hits_per_document(search, knowledge_base="workdocs", collection="workdocs") == 0

    async def test_a_failed_read_warns_naming_the_knowledge_base_and_the_error(self, caplog: pytest.LogCaptureFixture):
        search = SearchHandler(_FailingGateway(error=ConnectionRefusedError("refused by the host")), FakeEmbedding())

        with caplog.at_level("WARNING"):
            await read_hits_per_document(search, knowledge_base="workdocs", collection="workdocs")

        assert "workdocs" in caplog.text
        assert "ConnectionRefusedError" in caplog.text
        assert "refused by the host" in caplog.text


@dataclass
class _FailingGateway(FakeSqlGateway):
    """A gateway whose every read raises `error`."""

    error: Exception = field(default_factory=lambda: OSError("down"))

    async def rows(self, sql: str, params: Mapping[str, object] | None = None, *, cap: int = 50) -> SqlResult:
        del sql, params, cap
        raise self.error


def _full_rows(count: int = 12) -> tuple[dict[str, object], ...]:
    """`count` card rows with 480-character snippets, each folding two sections and one raw chunk."""
    return tuple(
        {
            "id": f"note.example-document-{number:02d}",
            "hit_level": "card",
            "page_id": f"note.example-document-{number:02d}",
            "page_title": f"An example document title number {number}",
            "snippet": "x" * 480,
            "score": 0.5 - number / 100,
            "lexical_rank": number + 1,
            "vector_rank": number + 1,
            "vector_distance": 0.2,
            "folded_units": [
                {"id": f"note.example-document-{number:02d}#s0", "hit_level": "section"},
                {"id": f"note.example-document-{number:02d}#s1", "hit_level": "section"},
                {"id": f"src.example-document-{number:02d}:{number}", "hit_level": "raw"},
            ],
        }
        for number in range(count)
    )


def _bounded_answer(max_chars: int = SEARCH_RESULT_CHARS) -> tuple[tuple[SearchHit, ...], SearchResult]:
    """The projected hits of `_full_rows` and the answer the response budget bounds them to."""
    hits = search_hits(_full_rows(), collection="knowledge", knowledge_base="knowledge", hybrid=True)
    status = search_result_status(hits)
    steps = search_next_steps(hits, query="example", knowledge_base="knowledge", hybrid=True, status=status, map_collection="knowledge")
    return hits, bound_search_result(hits, status=status, next_steps=steps, knowledge_base=None, max_chars=max_chars)


class TestTheResponseBudgetTrimsFoldedUnitsFirst:
    def test_twelve_full_hits_with_three_folded_units_each_all_come_back_with_whole_snippets(self):
        _hits, answer = _bounded_answer()

        assert len(answer.hits) == 12
        assert answer.window.omitted_items == 0
        assert all(hit.snippet == "x" * 480 for hit in answer.hits)

    def test_the_lowest_ranked_hits_lose_their_folded_units_first(self):
        hits, answer = _bounded_answer()
        kept = [len(hit.folded_units) for hit in answer.hits]

        assert answer.hits[0].folded_units == hits[0].folded_units
        assert kept[-1] < 3
        assert kept == sorted(kept, reverse=True)

    def test_a_snippet_is_cut_only_once_every_folded_unit_is_gone(self):
        _hits, answer = _bounded_answer(max_chars=6_000)

        assert all(hit.folded_units == () for hit in answer.hits)


_FOLDED_SNIPPET = "The folded section says the vote count moved to the end of the month, after the second recount. " * 2


def _one_folding_hit() -> tuple[tuple[SearchHit, ...], SearchResultStatus, NextSteps]:
    """One card hit folding one section whose snippet is long, with the status and next steps its answer carries."""
    row = {
        **_FOLDED_ROWS[0],
        "snippet": "z" * 300,
        "folded_units": [{"id": "note.alpha#s1", "hit_level": "section", "snippet": _FOLDED_SNIPPET}],
    }
    hits = search_hits((row,), collection="knowledge", knowledge_base="knowledge", hybrid=True)
    status = search_result_status(hits)
    return hits, status, search_next_steps(hits, query="example", knowledge_base="knowledge", hybrid=True, status=status, map_collection="knowledge")


def _size(hits: tuple[SearchHit, ...], status: SearchResultStatus, steps: NextSteps) -> int:
    """How many characters the whole answer over `hits` serializes to."""
    return len(bound_search_result(hits, status=status, next_steps=steps, knowledge_base=None, max_chars=1_000_000).model_dump_json())


class TestTheResponseBudgetCutsAFoldedSnippetBeforeDroppingItsUnit:
    def test_a_folded_unit_that_fits_with_a_shorter_snippet_keeps_a_cut_one(self):
        hits, status, steps = _one_folding_hit()
        unit = hits[0].folded_units[0]
        bare = _size((hits[0].model_copy(update={"folded_units": (unit.model_copy(update={"snippet": ""}),)}),), status, steps)

        answer = bound_search_result(hits, status=status, next_steps=steps, knowledge_base=None, max_chars=bare + 60)

        [kept] = answer.hits[0].folded_units
        assert kept.id == unit.id
        assert kept.snippet is not None
        assert 0 < len(kept.snippet) < len(_FOLDED_SNIPPET)
        assert kept.snippet.endswith("\u2026")
        assert answer.hits[0].snippet == "z" * 300

    def test_a_folded_snippet_is_never_cut_to_its_ellipsis_alone(self):
        hits, status, steps = _one_folding_hit()
        unit = hits[0].folded_units[0]
        ellipsis_only = _size((hits[0].model_copy(update={"folded_units": (unit.model_copy(update={"snippet": "\u2026"}),)}),), status, steps)

        answer = bound_search_result(hits, status=status, next_steps=steps, knowledge_base=None, max_chars=ellipsis_only)

        assert all(folded.snippet != "\u2026" for folded in answer.hits[0].folded_units)
        assert answer.hits[0].snippet == "z" * 300

    def test_a_folded_unit_that_fits_with_no_snippet_at_all_is_dropped_before_any_hit_snippet_is_cut(self):
        hits, status, steps = _one_folding_hit()
        alone = _size((hits[0].model_copy(update={"folded_units": ()}),), status, steps)

        answer = bound_search_result(hits, status=status, next_steps=steps, knowledge_base=None, max_chars=alone + 5)

        assert answer.hits[0].folded_units == ()
        assert answer.hits[0].snippet == "z" * 300


# -- every fetch opens a folded unit by its ref ------------------------------------

_FETCHED_ROWS = (
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
    {
        "id": "src.beta:0",
        "hit_level": "raw",
        "page_id": None,
        "page_title": "Beta source",
        "snippet": "a chunk",
        "score": 0.5,
        "folded_units": [{"id": "note.beta", "hit_level": "card"}],
    },
)
_UNITS: dict[str, dict[str, object]] = {
    "note.alpha#s1": {"id": "note.alpha#s1", "level": "section", "title": "Findings", "body": "The section."},
    "note.alpha#c1": {"id": "note.alpha#c1", "level": "claim", "title": "support", "body": "The claim."},
    "src.alpha:0": {"id": "src.alpha:0", "level": "raw", "title": "Body", "body": "The chunk."},
    "note.beta": {"id": "note.beta", "level": "page", "title": "Beta", "body": "The card."},
}


def _fetched_search() -> FakeSearchHandler:
    """A search answering one card folding a section, a claim and a chunk, and one chunk folding a card."""
    return FakeSearchHandler(result=SqlResult(rows=_FETCHED_ROWS, columns=_COLUMNS))


def _folded(result: object) -> list[FoldedUnit]:
    """Every folded unit a search answer carries, in rank order."""
    assert isinstance(result, SearchResult)
    return [unit for hit in result.hits for unit in hit.folded_units]


def _opens(result: object, unit: FoldedUnit) -> bool:
    """Whether a `fetch` answer is the read of `unit`: a leveled answer by its ref, a flat one by its id."""
    if isinstance(result, DisclosureEnvelope):
        return result.ref == unit.ref
    return isinstance(result, FetchResult) and result.id == unit.id


def _opens_payload(payload: Mapping[str, object], unit: FoldedUnit) -> bool:
    """Whether a `fetch` answer read off the wire is the read of `unit`."""
    return payload.get("ref") == unit.ref if "children" in payload else payload.get("id") == unit.id


def _surface_over_units(name: str, domain: str) -> ReadSurface:
    """A read surface whose search folds units under its hits and whose flat read knows each of them by id."""
    return _surface(name, name, domain, search=_fetched_search(), flat=FakeFlatFetch(units=_UNITS))


class TestEveryFetchOpensAFoldedUnitByItsRef:
    @pytest.mark.parametrize("leveled", [False, True], ids=["flat", "leveled"])
    async def test_the_single_base_toolset(self, leveled: bool):
        handlers = RetrievalHandlers(search=_fetched_search(), fetch=FakeFlatFetch(units=_UNITS), graph_expand=_NeverWalks(), find_paths=_NeverWalks())
        toolset = build_retrieval_toolset(handlers, collection="workdocs", envelope=_envelope("workdocs") if leveled else None)

        units = _folded(await _call(toolset, "search", {"query": "alpha"}))

        assert len(units) == 4
        for unit in units:
            assert _opens(await _call(toolset, "fetch", {"unit_id": unit.ref}), unit), unit

    @pytest.mark.parametrize("leveled", [False, True], ids=["flat", "leveled"])
    async def test_the_single_base_mcp_server(self, leveled: bool):
        handlers = RetrievalHandlers(search=_fetched_search(), fetch=FakeFlatFetch(units=_UNITS), graph_expand=_NeverWalks(), find_paths=_NeverWalks())
        server = MCPServer("documents")
        register_mcp_tools(server, handlers, collection="workdocs", envelope=_envelope("workdocs") if leveled else None)

        async with Client(server) as client:
            searched = await client.call_tool("search", {"query": "alpha"})
            units = _folded(SearchResult.model_validate(json.loads(_text(searched))))
            fetched = [(unit, await client.call_tool("fetch", {"unit_id": unit.ref})) for unit in units]

        assert len(fetched) == 4
        for unit, answer in fetched:
            assert not answer.is_error, _text(answer)
            assert _opens_payload(json.loads(_text(answer)), unit), unit

    async def test_the_routed_search_and_fetch(self):
        toolset: RetrievalToolset = FunctionToolset()
        add_routed_read_tools(
            toolset, (_surface("workdocs", "workdocs", WORKDOCS), _surface_over_units("research", RESEARCH)), default_knowledge_base="workdocs"
        )

        units = _folded(await _call(toolset, "search", {"query": "alpha", "collection": "research"}))

        assert len(units) == 4
        for unit in units:
            assert _opens(await _call(toolset, "fetch", {"unit_id": unit.ref}), unit), unit

    async def test_the_routed_mcp_server(self):
        server = MCPServer("documents")
        register_routed_mcp_tools(
            server, (_surface("workdocs", "workdocs", WORKDOCS), _surface_over_units("research", RESEARCH)), default_knowledge_base="workdocs"
        )

        async with Client(server) as client:
            searched = await client.call_tool("search", {"query": "alpha", "collection": "research"})
            units = _folded(SearchResult.model_validate(json.loads(_text(searched))))
            fetched = [(unit, await client.call_tool("fetch", {"unit_id": unit.ref})) for unit in units]

        assert len(fetched) == 4
        for unit, answer in fetched:
            assert not answer.is_error, _text(answer)
            assert _opens_payload(json.loads(_text(answer)), unit), unit

    @pytest.mark.parametrize("fetch_dial", ["leveled", "flat"])
    async def test_the_capability(self, fetch_dial: FetchDial):
        knowledge_base = RetrievalKnowledgeBase[None](
            surfaces=(_surface_over_units("research", RESEARCH), _surface("workdocs", "workdocs", WORKDOCS)),
            dials_source=lambda: ReadDials(fetch=fetch_dial),
        )

        units = _folded(await knowledge_base.direct_call_tool("search", {"query": "alpha", "knowledge_base": "research"}))

        assert len(units) == 4
        for unit in units:
            assert _opens(await knowledge_base.direct_call_tool("fetch", {"knowledge_base": "research", "unit_id": unit.ref}), unit), unit


class TestASurfaceWithoutFetchIsNotSentToFetch:
    async def test_the_single_base_toolset(self):
        toolset = build_retrieval_toolset(_handlers(), collection="workdocs", only=frozenset({"search"}), hits_per_document=1)

        description = (await toolset.get_tools(_ctx()))["search"].tool_def.description or ""

        assert "`folded_units`" in description
        assert "`fetch` one directly" not in description

    async def test_the_routed_mcp_server(self):
        server = MCPServer("documents")
        register_routed_mcp_tools(server, (_capped("workdocs", WORKDOCS, 1),), default_knowledge_base="workdocs", only=frozenset({"search"}))

        description = _description(await server.list_tools())

        assert "`folded_units`" in description
        assert "`fetch` one directly" not in description

    def test_the_clause_itself(self):
        clause = document_clause(2, can_fetch=False)

        assert "`fetch`" not in clause
        assert _SNIPPETED in clause


_LEGS = pytest.mark.parametrize("search_hybrid", [None, True, False], ids=["either_leg", "hybrid", "keyword"])


class TestASearchOnlySurfaceNeverTellsTheModelToFetch:
    @_LEGS
    async def test_the_single_base_toolset(self, search_hybrid: bool | None):
        toolset = build_retrieval_toolset(
            _handlers(), collection="workdocs", only=frozenset({"search"}), search_hybrid=search_hybrid, hits_per_document=1
        )

        description = (await toolset.get_tools(_ctx()))["search"].tool_def.description or ""

        assert "fetch" not in description.lower()
        assert "Start here to find candidate sources." in description

    @_LEGS
    async def test_the_single_base_mcp_server(self, search_hybrid: bool | None):
        server = MCPServer("documents")
        register_mcp_tools(server, _handlers(), collection="workdocs", only=frozenset({"search"}), search_hybrid=search_hybrid, hits_per_document=1)

        assert "fetch" not in _description(await server.list_tools()).lower()

    async def test_the_routed_mcp_server(self):
        server = MCPServer("documents")
        surfaces = (_capped("workdocs", WORKDOCS, 1), _capped("research", RESEARCH, 1))
        register_routed_mcp_tools(server, surfaces, default_knowledge_base="workdocs", only=frozenset({"search"}))

        description = _description(await server.list_tools())

        assert "fetch" not in description.lower()
        assert "Every hit's `ref` already names the knowledge base it came from." in description

    async def test_a_surface_with_fetch_still_sends_the_model_to_it(self):
        toolset = build_retrieval_toolset(_handlers(), collection="workdocs", search_hybrid=True)

        description = (await toolset.get_tools(_ctx()))["search"].tool_def.description or ""

        assert "then `fetch` a hit's full text by `id`." in description
        assert "Every hit stays a candidate until you fetch and read it." in description


# -- a search answer's next steps name only the tools the surface serves ------------

_ANSWERS = {
    "lexical_hits": (
        {"id": "note.alpha", "hit_level": "card", "page_id": "note.alpha", "page_title": "Alpha", "snippet": "a card", "score": 0.9, "lexical_rank": 1},
    ),
    "semantic_hits": (
        {"id": "note.alpha", "hit_level": "card", "page_id": "note.alpha", "page_title": "Alpha", "snippet": "a card", "score": 0.9, "vector_rank": 1},
    ),
    "no_hits": (),
}
_ANSWER_COLUMNS = ("id", "hit_level", "page_id", "page_title", "snippet", "score", "lexical_rank", "vector_rank")


def _answering(answer: str) -> FakeSearchHandler:
    """A search whose rows are the scripted `answer`: keyword-supported hits, semantic-only hits or none."""
    return FakeSearchHandler(result=SqlResult(rows=_ANSWERS[answer], columns=_ANSWER_COLUMNS))


def _steps(result: object) -> str:
    """A search answer's next-move block as the model reads it: its summary and every option."""
    assert isinstance(result, SearchResult)
    return " ".join((result.next_steps.summary, *result.next_steps.options))


_ANSWER_KINDS = pytest.mark.parametrize("answer", list(_ANSWERS))


class TestASearchAnswerOffersFetchOnlyWhereItIsServed:
    @_ANSWER_KINDS
    @pytest.mark.parametrize("served", [False, True], ids=["search_only", "with_fetch"])
    async def test_the_single_base_toolset(self, answer: str, served: bool):
        only = frozenset({"search", "fetch"}) if served else frozenset({"search"})
        toolset = build_retrieval_toolset(_handlers(search=_answering(answer)), collection="workdocs", only=only, envelope=_envelope("workdocs"))

        steps = _steps(await _call(toolset, "search", {"query": "alpha"}))

        assert ("fetch" in steps) is served

    @_ANSWER_KINDS
    @pytest.mark.parametrize("served", [False, True], ids=["search_only", "with_fetch"])
    async def test_the_single_base_mcp_server(self, answer: str, served: bool):
        only = frozenset({"search", "fetch"}) if served else frozenset({"search"})
        server = MCPServer("documents")
        register_mcp_tools(server, _handlers(search=_answering(answer)), collection="workdocs", only=only, envelope=_envelope("workdocs"))

        async with Client(server) as client:
            searched = await client.call_tool("search", {"query": "alpha"})

        assert ("fetch" in _steps(SearchResult.model_validate(json.loads(_text(searched))))) is served

    @_ANSWER_KINDS
    @pytest.mark.parametrize("served", [False, True], ids=["search_only", "with_fetch"])
    async def test_the_routed_mcp_server(self, answer: str, served: bool):
        only = frozenset({"search", "fetch"}) if served else frozenset({"search"})
        server = MCPServer("documents")
        surfaces = (_surface("workdocs", "workdocs", WORKDOCS, search=_answering(answer)), _surface("research", "research", RESEARCH))
        register_routed_mcp_tools(server, surfaces, default_knowledge_base="workdocs", only=only)

        async with Client(server) as client:
            searched = await client.call_tool("search", {"query": "alpha"})

        assert ("fetch" in _steps(SearchResult.model_validate(json.loads(_text(searched))))) is served
