"""`ReadDials` on the read tools: modes resolved per call, descriptions following per request.

The dial source is read on every call, so one capability instance serves keyword and hybrid
searches, flat and leveled reads, as the host's dial turns — no rebuild, no re-mount. The
prepared toolset keeps the descriptions honest: a tool never promises another mode's behavior.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

import pytest

from core.ai.disclosure.contracts import DisclosureEnvelope
from pydantic_ai.exceptions import ModelRetry
from pydantic_ai.models.test import TestModel
from pydantic_ai.tools import RunContext
from pydantic_ai.usage import RunUsage

from engram.retrieval.capability import ReadSurface, RetrievalKnowledgeBase
from engram.retrieval.dials import ReadDials
from engram.retrieval.domain import FetchResult, SqlResult
from engram.retrieval.envelope import BadRefError
from engram.retrieval.tools import TOOL_NAMES, RetrievalHandlers

if TYPE_CHECKING:
    from core.ai.disclosure.contracts import DisclosureMode

    from engram.retrieval.domain import Search

SEARCH_COLUMNS = ("id", "hit_level", "page_title", "snippet", "score")


@dataclass
class FakeSearch:
    """Scripted ranked rows; records each query, so a test reads the leg off `Search.hybrid`."""

    rows: tuple[dict[str, object], ...] = ()
    seen: list[Search] = field(default_factory=list)

    async def __call__(self, query: Search) -> SqlResult:
        self.seen.append(query)
        return SqlResult(rows=self.rows, columns=SEARCH_COLUMNS)


@dataclass
class FakeFlatFetch:
    """The flat `api.fetch` read: scripted unit rows by id."""

    units: dict[str, dict[str, object]] = field(default_factory=dict)

    async def __call__(self, query: object) -> SqlResult:
        unit = self.units.get(getattr(query, "id", ""))
        return SqlResult(rows=(unit,) if unit else (), columns=("id", "level", "title", "body"))


class _NeverWalks:
    """Graph handlers the read tools never drive."""

    async def __call__(self, query: object, *, tools: frozenset[str] = TOOL_NAMES) -> Any:  # noqa: ANN401 (one fake satisfies both graph slices)
        del query, tools
        msg = "the read tools never walk the graph"
        raise AssertionError(msg)


@dataclass
class FakeEnvelope:
    """Scripted leveled reads: known refs answer, everything else is outside the surface."""

    known: dict[str, DisclosureEnvelope] = field(default_factory=dict)
    mode: DisclosureMode = "strict"

    async def __call__(self, ref: str) -> DisclosureEnvelope:
        if ref not in self.known:
            msg = f"ref {ref!r} is outside this surface"
            raise BadRefError(msg)
        return self.known[ref]


@dataclass
class DialBoard:
    """A host's live dial state: reassign `dials` between calls to turn a dial mid-session."""

    dials: ReadDials = field(default_factory=ReadDials)

    def source(self) -> ReadDials:
        return self.dials


def _unit(unit_id: str) -> dict[str, object]:
    return {"id": unit_id, "level": "page", "title": unit_id, "body": f"The whole text of {unit_id}."}


def _surface(*, search: FakeSearch | None = None, flat: FakeFlatFetch | None = None, hybrid: bool = True) -> ReadSurface:
    root = "collection:kitchen"
    return ReadSurface(
        name="kitchen",
        collection="kitchen",
        handlers=RetrievalHandlers(
            search=search or FakeSearch(),
            fetch=flat or FakeFlatFetch(),
            graph_expand=_NeverWalks(),
            find_paths=_NeverWalks(),
        ),
        envelope=FakeEnvelope(known={root: DisclosureEnvelope(ref=root, kind="collection", title="kitchen", mode="strict")}),
        domain="Working notes for a pickling kitchen.",
        hybrid=hybrid,
    )


def _knowledge_base(board: DialBoard, *, search: FakeSearch | None = None, flat: FakeFlatFetch | None = None, hybrid: bool = True) -> RetrievalKnowledgeBase[None]:
    return RetrievalKnowledgeBase[None](surfaces=(_surface(search=search, flat=flat, hybrid=hybrid),), dials_source=board.source)


def _ctx() -> RunContext[None]:
    return RunContext(deps=None, model=TestModel(), usage=RunUsage())


class TestFetchDial:
    async def test_flat_reads_a_bare_id_as_full_text(self):
        board = DialBoard(ReadDials(fetch="flat"))
        knowledge_base = _knowledge_base(board, flat=FakeFlatFetch(units={"mu.brine": _unit("mu.brine")}))

        result = await knowledge_base.direct_call_tool("fetch", {"unit_id": "mu.brine"})

        assert isinstance(result, FetchResult)
        assert result.body == "The whole text of mu.brine."

    async def test_flat_sheds_the_prefixes_a_leveled_listing_taught(self):
        board = DialBoard(ReadDials(fetch="flat"))
        knowledge_base = _knowledge_base(board, flat=FakeFlatFetch(units={"mu.brine#s1": _unit("mu.brine#s1")}))

        result = await knowledge_base.direct_call_tool("fetch", {"unit_id": "section:mu.brine#s1"})

        assert isinstance(result, FetchResult)
        assert result.id == "mu.brine#s1"

    @pytest.mark.parametrize("map_request", ["", "kitchen", "collection:kitchen"])
    async def test_flat_serves_no_map(self, map_request: str):
        board = DialBoard(ReadDials(fetch="flat"))
        knowledge_base = _knowledge_base(board)

        with pytest.raises(ModelRetry, match="no browsable map"):
            await knowledge_base.direct_call_tool("fetch", {"unit_id": map_request})

    async def test_flat_misses_teach_regrounding(self):
        board = DialBoard(ReadDials(fetch="flat"))
        knowledge_base = _knowledge_base(board)

        with pytest.raises(ModelRetry, match="Re-ground the id with `search`"):
            await knowledge_base.direct_call_tool("fetch", {"unit_id": "mu.gone"})

    async def test_the_dial_turns_between_calls_on_one_instance(self):
        board = DialBoard(ReadDials(fetch="flat"))
        knowledge_base = _knowledge_base(board, flat=FakeFlatFetch(units={"mu.brine": _unit("mu.brine")}))

        with pytest.raises(ModelRetry, match="no browsable map"):
            await knowledge_base.direct_call_tool("fetch", {})
        board.dials = ReadDials(fetch="leveled")
        leveled = await knowledge_base.direct_call_tool("fetch", {})

        assert isinstance(leveled, DisclosureEnvelope)
        assert leveled.ref == "collection:kitchen"


class TestSearchDial:
    async def test_keyword_forces_the_bm25_leg(self):
        recorder = FakeSearch()
        board = DialBoard(ReadDials(search="keyword"))
        knowledge_base = _knowledge_base(board, search=recorder)

        await knowledge_base.direct_call_tool("search", {"query": "brine", "k": 3})

        assert recorder.seen[0].hybrid is False

    async def test_hybrid_runs_the_surfaces_own_leg(self):
        recorder = FakeSearch()
        board = DialBoard(ReadDials(search="hybrid"))
        knowledge_base = _knowledge_base(board, search=recorder)

        await knowledge_base.direct_call_tool("search", {"query": "brine", "k": 3})

        assert recorder.seen[0].hybrid is True

    async def test_the_dial_never_conjures_a_missing_embedder(self):
        recorder = FakeSearch()
        board = DialBoard(ReadDials(search="hybrid"))
        knowledge_base = _knowledge_base(board, search=recorder, hybrid=False)

        await knowledge_base.direct_call_tool("search", {"query": "brine", "k": 3})

        assert recorder.seen[0].hybrid is False


class TestDialedDescriptions:
    async def _descriptions(self, board: DialBoard) -> dict[str, str]:
        tools = await _knowledge_base(board).get_toolset().get_tools(_ctx())
        return {name: tool.tool_def.description or "" for name, tool in tools.items()}

    async def test_flat_speaks_full_text_and_never_levels(self):
        described = await self._descriptions(DialBoard(ReadDials(fetch="flat")))
        assert "full text" in described["fetch"]
        assert "leveled" not in described["fetch"]

    async def test_leveled_speaks_levels(self):
        described = await self._descriptions(DialBoard(ReadDials(fetch="leveled")))
        assert "leveled" in described["fetch"]

    async def test_keyword_renders_the_keyword_tuning(self):
        keyword = await self._descriptions(DialBoard(ReadDials(search="keyword")))
        hybrid = await self._descriptions(DialBoard(ReadDials(search="hybrid")))
        assert "no semantic vector leg" in keyword["search"]
        assert keyword["search"] != hybrid["search"]

    async def test_an_undialed_door_keeps_the_plain_toolset(self):
        knowledge_base = RetrievalKnowledgeBase[None](surfaces=(_surface(),))
        tools = await knowledge_base.get_toolset().get_tools(_ctx())
        assert "leveled" in (tools["fetch"].tool_def.description or "")
