r"""Auto-retrieval routing: search and the graph tools consult the knowledge base's earned switches, and a hit's ref routes its own read.

`RetrievalKnowledgeBase` routes a surface's vector leg through `_effective_hybrid`: the dial can force
keyword, a surface with no embedder never runs it, and an auto-mode surface (`switch_source` set)
additionally reads its stored embeddings switch fresh on every call. A fixed-mode surface
(`switch_source=None`) never consults a switch: the vector leg runs whenever `hybrid` and the dial
allow it. Every hit either surface returns is stamped `answered_by` with the leg that actually
ran, the universal honesty line (fixed knowledge bases carry it too). Several surfaces route and stamp
independently, each consulting only its own switch.

`graph_expand`/`find_paths` follow the SAME switch, read off the record's `.graph` field: they
exist on the toolset only when some surface currently earns them, and a call routes to the surface
that earned it — direct when only one ever could, by name when several simultaneously do.

The other routing this file pins is by HANDLE: every `search` hit comes back with the
collection-qualified `ref` of the surface that answered, and passing that ref back to `fetch` reads
it from that same surface — even when another knowledge base in the same reader holds the same bare
id — and answers with that surface's own freshness stamp.
"""

from __future__ import annotations

import re

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

import pytest

from core.ai.disclosure.contracts import DisclosureEnvelope
from core.ai.retrieval.contracts import GraphPathsResult, KnowledgeBaseStamp, NextSteps, ResultWindow
from pydantic_ai.exceptions import ModelRetry
from pydantic_ai.models.test import TestModel
from pydantic_ai.tools import RunContext
from pydantic_ai.usage import RunUsage

from engram.indexing.contracts import RetrievalSwitches
from engram.retrieval.capability import ReadSurface, RetrievalKnowledgeBase
from engram.retrieval.dials import ReadDials
from engram.retrieval.domain import (
    FetchResult,
    GraphEdge,
    GraphExpand,
    GraphNode,
    GraphPath,
    GraphPaths,
    GraphResult,
    SearchHit,
    SearchResult,
    SqlResult,
)
from engram.retrieval.envelope import BadRefError
from engram.retrieval.queries import bound_graph_result
from engram.retrieval.tools import TOOL_NAMES, FetchEnvelope, RetrievalHandlers

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

    from core.ai.disclosure.contracts import DisclosureMode
    from core.ai.retrieval.contracts import StampSource

    from engram.retrieval.domain import Fetch, Search
    from engram.retrieval.tools import SupportsFindPaths, SupportsGraphExpand

SEARCH_COLUMNS = ("id", "hit_level", "page_title", "snippet", "score")


@dataclass
class FakeSearch:
    """Scripted ranked rows; records each query, so a test reads the leg off `Search.hybrid`."""

    rows: tuple[dict[str, object], ...] = ()
    seen: list[Search] = field(default_factory=list)

    async def __call__(self, query: Search) -> SqlResult:
        self.seen.append(query)
        return SqlResult(rows=self.rows, columns=SEARCH_COLUMNS)


class _NeverDriven:
    """The fetch / graph handler slices the search tool never drives (one fake satisfies all three)."""

    async def __call__(self, query: object, *, tools: frozenset[str] = TOOL_NAMES) -> Any:  # noqa: ANN401 (never returns — satisfies three differing result types)
        del query, tools
        msg = "the search tool never drives this handler"
        raise AssertionError(msg)


@dataclass
class _NeverOpened:
    """The envelope reader the search tool never drives."""

    mode: DisclosureMode = "strict"

    async def __call__(self, ref: str) -> DisclosureEnvelope:
        msg = f"the search tool never opens {ref!r}"
        raise AssertionError(msg)


@dataclass
class SwitchBoard:
    """A host's stored retrieval switches: reassign `on`/`graph` between calls to flip them mid-session."""

    on: bool
    graph: bool = False
    calls: int = 0

    async def source(self) -> RetrievalSwitches:
        self.calls += 1
        return RetrievalSwitches(embeddings=self.on, graph=self.graph)


@dataclass
class FakeGraphExpand:
    """Scripted `graph_expand` handler; records every `GraphExpand` message it was driven with."""

    result: GraphResult = field(default_factory=GraphResult)
    seen: list[GraphExpand] = field(default_factory=list)

    async def __call__(self, query: GraphExpand, *, tools: frozenset[str] = TOOL_NAMES) -> GraphResult:
        del tools
        self.seen.append(query)
        return self.result


@dataclass
class FakeFindPaths:
    """Scripted `find_paths` handler; records every `GraphPaths` message it was driven with."""

    result: tuple[GraphPath, ...] = ()
    seen: list[GraphPaths] = field(default_factory=list)

    async def __call__(self, query: GraphPaths, *, tools: frozenset[str] = TOOL_NAMES) -> GraphPathsResult:
        del tools
        self.seen.append(query)
        return GraphPathsResult(
            paths=self.result,
            next_steps=NextSteps(summary=f"{len(self.result)} route(s)."),
            window=ResultWindow(returned_items=len(self.result)),
        )


@dataclass
class FakePages:
    """Scripted leveled reads for one knowledge base; records every ref it was handed."""

    bodies: dict[str, str] = field(default_factory=dict)
    calls: list[str] = field(default_factory=list)
    mode: DisclosureMode = "strict"

    async def __call__(self, ref: str) -> DisclosureEnvelope:
        self.calls.append(ref)
        body = self.bodies.get(ref)
        if body is None:
            msg = f"ref {ref!r} is outside this knowledge base"
            raise BadRefError(msg)
        return DisclosureEnvelope(ref=ref, kind="reference", title=ref, content=body, mode=self.mode)


@dataclass
class FakeUnits:
    """The flat `api.fetch` read for one knowledge base; records every id it was handed."""

    units: dict[str, dict[str, object]] = field(default_factory=dict)
    calls: list[str] = field(default_factory=list)

    async def __call__(self, query: Fetch) -> SqlResult:
        self.calls.append(query.id)
        unit = self.units.get(query.id)
        return SqlResult(rows=(unit,) if unit else (), columns=("id", "level", "title", "body"))


def _stamp(slug: str, changed_since: int) -> StampSource:
    """One knowledge base's freshness stamp, the shape a host resolves fresh on every answer."""

    async def read() -> KnowledgeBaseStamp:
        return KnowledgeBaseStamp(slug=slug, changed_since=changed_since)

    return read


def _hit(unit_id: str, score: float, *, hit_level: str = "card") -> dict[str, object]:
    return {"id": unit_id, "hit_level": hit_level, "page_title": unit_id, "snippet": "…", "score": score}


def _surface(
    *,
    name: str = "kitchen",
    collection: str = "kitchen",
    search: FakeSearch,
    hybrid: bool = True,
    switch_source: Callable[[], Awaitable[RetrievalSwitches]] | None = None,
    graph_expand: SupportsGraphExpand | None = None,
    find_paths: SupportsFindPaths | None = None,
    pages: FakePages | None = None,
    units: FakeUnits | None = None,
    stamp: StampSource | None = None,
) -> ReadSurface:
    return ReadSurface(
        name=name,
        collection=collection,
        handlers=RetrievalHandlers(
            search=search,
            fetch=units if units is not None else _NeverDriven(),
            graph_expand=graph_expand if graph_expand is not None else _NeverDriven(),
            find_paths=find_paths if find_paths is not None else _NeverDriven(),
        ),
        envelope=pages if pages is not None else _NeverOpened(),
        domain="Working notes for a pickling kitchen.",
        stamp=stamp,
        hybrid=hybrid,
        switch_source=switch_source,
    )


def _knowledge_base(surface: ReadSurface, *, dials: ReadDials | None = None) -> RetrievalKnowledgeBase[None]:
    resolved = dials or ReadDials()
    return RetrievalKnowledgeBase[None](surfaces=(surface,), dials_source=lambda: resolved)


def _ctx() -> RunContext[None]:
    return RunContext(deps=None, model=TestModel(), usage=RunUsage())


async def _tool_names(knowledge_base: RetrievalKnowledgeBase[None]) -> set[str]:
    tools = await knowledge_base.get_toolset().get_tools(_ctx())
    return set(tools)


async def _search(knowledge_base: RetrievalKnowledgeBase[None]) -> tuple[SearchHit, ...]:
    result = await knowledge_base.direct_call_tool("search", {"query": "brine", "k": 3})
    assert isinstance(result, SearchResult)
    return result.hits


class TestAutoModeConsultsTheStoredSwitch:
    async def test_switch_off_runs_lexical_and_the_hits_say_so(self):
        search = FakeSearch(rows=(_hit("mu.brine", 0.9),))
        board = SwitchBoard(on=False)
        surface = _surface(search=search, switch_source=board.source)

        hits = await _search(_knowledge_base(surface))

        assert search.seen[0].hybrid is False
        assert [hit.answered_by for hit in hits] == ["lexical"]
        assert board.calls == 1

    async def test_switch_on_runs_hybrid_and_the_hits_say_so(self):
        search = FakeSearch(rows=(_hit("mu.brine", 0.9),))
        board = SwitchBoard(on=True)
        surface = _surface(search=search, switch_source=board.source)

        hits = await _search(_knowledge_base(surface))

        assert search.seen[0].hybrid is True
        assert [hit.answered_by for hit in hits] == ["hybrid"]
        assert board.calls == 1


class TestFixedModeInvariance:
    async def test_no_switch_source_keeps_todays_routing(self):
        # `switch_source=None` is every fixed-mode knowledge_base: the switch is never consulted, and the
        # leg is `surface.hybrid` gated only by the dial — exactly as before auto mode existed.
        search = FakeSearch(rows=(_hit("mu.brine", 0.9),))
        surface = _surface(search=search, switch_source=None)

        hits = await _search(_knowledge_base(surface))

        assert search.seen[0].hybrid is True
        assert [hit.answered_by for hit in hits] == ["hybrid"]

    async def test_fixed_mode_hits_still_carry_the_honesty_stamp(self):
        # The stamp is universal: a fixed knowledge base running its ordinary keyword-only leg says so too.
        search = FakeSearch(rows=(_hit("mu.brine", 0.9),))
        surface = _surface(search=search, hybrid=False, switch_source=None)

        hits = await _search(_knowledge_base(surface))

        assert search.seen[0].hybrid is False
        assert [hit.answered_by for hit in hits] == ["lexical"]


class TestTheDialAndTheEmbedderStillWin:
    async def test_the_keyword_dial_beats_an_on_switch(self):
        search = FakeSearch(rows=(_hit("mu.brine", 0.9),))
        board = SwitchBoard(on=True)
        surface = _surface(search=search, switch_source=board.source)

        hits = await _search(_knowledge_base(surface, dials=ReadDials(search="keyword")))

        assert search.seen[0].hybrid is False
        assert [hit.answered_by for hit in hits] == ["lexical"]
        assert board.calls == 0  # the dial already forces lexical — the switch is never worth reading

    async def test_a_surface_without_an_embedder_never_runs_the_vector_leg(self):
        # An on switch cannot conjure an embedder the surface never had.
        search = FakeSearch(rows=(_hit("mu.brine", 0.9),))
        board = SwitchBoard(on=True)
        surface = _surface(search=search, hybrid=False, switch_source=board.source)

        hits = await _search(_knowledge_base(surface))

        assert search.seen[0].hybrid is False
        assert [hit.answered_by for hit in hits] == ["lexical"]
        assert board.calls == 0  # no embedder already settles it — the switch is never worth reading


class TestMultiSurfaceRouting:
    async def test_each_surface_routes_by_its_own_switch_read_fresh_per_call(self):
        kitchen_search = FakeSearch(rows=(_hit("mu.brine", 0.9),))
        wiki_search = FakeSearch(rows=(_hit("paper.lacto", 0.7),))
        kitchen_board = SwitchBoard(on=True)
        wiki_board = SwitchBoard(on=False)
        kitchen = _surface(name="kitchen", collection="kitchen", search=kitchen_search, switch_source=kitchen_board.source)
        wiki = _surface(name="wiki", collection="wiki", search=wiki_search, switch_source=wiki_board.source)
        knowledge_base = RetrievalKnowledgeBase[None](surfaces=(kitchen, wiki), dials_source=ReadDials, cross_search=True)

        result = await knowledge_base.direct_call_tool("search", {"query": "brine", "k": 5})

        assert isinstance(result, SearchResult)
        assert {hit.id: hit.answered_by for hit in result.hits} == {"mu.brine": "hybrid", "paper.lacto": "lexical"}
        assert kitchen_search.seen[0].hybrid is True
        assert wiki_search.seen[0].hybrid is False
        assert kitchen_board.calls == 1  # each surface's own switch, read exactly once for this call
        assert wiki_board.calls == 1


class TestGraphToolsAppearWhenEarned:
    async def test_switch_off_keeps_the_plain_two_tools(self):
        board = SwitchBoard(on=True, graph=False)
        surface = _surface(search=FakeSearch(), switch_source=board.source)

        assert await _tool_names(_knowledge_base(surface)) == {"search", "fetch"}

    async def test_switch_on_adds_the_graph_tools(self):
        board = SwitchBoard(on=True, graph=True)
        surface = _surface(search=FakeSearch(), switch_source=board.source)

        assert await _tool_names(_knowledge_base(surface)) == {"search", "fetch", "graph_expand", "find_paths"}

    async def test_a_fixed_mode_surface_never_shows_them(self):
        surface = _surface(search=FakeSearch(), switch_source=None)

        assert await _tool_names(_knowledge_base(surface)) == {"search", "fetch"}

    async def test_a_fixed_mode_surface_never_shows_them_via_list_tools_either(self):
        surface = _surface(search=FakeSearch(), switch_source=None)
        knowledge_base = _knowledge_base(surface)

        names = {tool.name for tool in await knowledge_base.list_tools()}

        assert names == {"search", "fetch"}

    async def test_list_tools_also_follows_the_earned_state(self):
        board = SwitchBoard(on=True, graph=True)
        surface = _surface(search=FakeSearch(), switch_source=board.source)
        knowledge_base = _knowledge_base(surface)

        names = {tool.name for tool in await knowledge_base.list_tools()}

        assert names == {"search", "fetch", "graph_expand", "find_paths"}


class TestGraphToolDescriptionsTeachTheKnowledgeBaseArgument:
    async def test_a_single_surface_keeps_the_flat_description(self):
        board = SwitchBoard(on=True, graph=True)
        surface = _surface(search=FakeSearch(), switch_source=board.source)
        knowledge_base = _knowledge_base(surface)

        tools = await knowledge_base.get_toolset().get_tools(_ctx())

        assert "`knowledge_base`" not in (tools["graph_expand"].tool_def.description or "")
        assert "`knowledge_base`" not in (tools["find_paths"].tool_def.description or "")

    async def test_several_surfaces_name_the_argument_and_list_the_knowledge_bases(self):
        kitchen = _surface(name="kitchen", collection="kitchen", search=FakeSearch(), switch_source=SwitchBoard(on=True, graph=True).source)
        wiki = _surface(name="wiki", collection="wiki", search=FakeSearch(), switch_source=SwitchBoard(on=True, graph=True).source)
        knowledge_base = RetrievalKnowledgeBase[None](surfaces=(kitchen, wiki), dials_source=ReadDials)

        tools = await knowledge_base.get_toolset().get_tools(_ctx())

        graph_expand_description = tools["graph_expand"].tool_def.description or ""
        find_paths_description = tools["find_paths"].tool_def.description or ""
        for description in (graph_expand_description, find_paths_description):
            assert "`knowledge_base`" in description
            assert "'kitchen'" in description
            assert "'wiki'" in description

    async def test_with_no_default_source_the_description_says_the_sole_earner_rule(self):
        kitchen = _surface(name="kitchen", collection="kitchen", search=FakeSearch(), switch_source=SwitchBoard(on=True, graph=True).source)
        wiki = _surface(name="wiki", collection="wiki", search=FakeSearch(), switch_source=SwitchBoard(on=True, graph=True).source)
        knowledge_base = RetrievalKnowledgeBase[None](surfaces=(kitchen, wiki), dials_source=ReadDials)

        tools = await knowledge_base.get_toolset().get_tools(_ctx())

        for name in ("graph_expand", "find_paths"):
            description = tools[name].tool_def.description or ""
            assert 'knowledge_base="all"' in description
            assert "not accepted" in description

    async def test_a_default_source_is_named_in_the_description(self):
        kitchen = _surface(name="kitchen", collection="kitchen", search=FakeSearch(), switch_source=SwitchBoard(on=True, graph=True).source)
        wiki = _surface(name="wiki", collection="wiki", search=FakeSearch(), switch_source=SwitchBoard(on=True, graph=True).source)
        knowledge_base = RetrievalKnowledgeBase[None](surfaces=(kitchen, wiki), dials_source=ReadDials, cross_search=True, default_knowledge_base_source=lambda: "wiki")

        tools = await knowledge_base.get_toolset().get_tools(_ctx())

        for name in ("graph_expand", "find_paths"):
            description = tools[name].tool_def.description or ""
            assert "chosen for you" in description
            assert 'knowledge_base="all"' in description
            assert "not accepted" in description


class TestGraphToolCallsReachTheHandler:
    async def test_graph_expand_through_the_door_reaches_the_fake_handler(self):
        board = SwitchBoard(on=True, graph=True)
        fake = FakeGraphExpand()
        surface = _surface(search=FakeSearch(), switch_source=board.source, graph_expand=fake)
        knowledge_base = _knowledge_base(surface)

        result = await knowledge_base.direct_call_tool("graph_expand", {"seed_ids": ["mu.brine"], "hops": 2})

        assert isinstance(result, GraphResult)
        assert fake.seen == [GraphExpand(seed_ids=("mu.brine",), hops=2)]

    async def test_find_paths_through_the_door_reaches_the_fake_handler(self):
        board = SwitchBoard(on=True, graph=True)
        fake = FakeFindPaths()
        surface = _surface(search=FakeSearch(), switch_source=board.source, find_paths=fake)
        knowledge_base = _knowledge_base(surface)

        result = await knowledge_base.direct_call_tool("find_paths", {"from_id": "a", "to_id": "b"})

        assert isinstance(result, GraphPathsResult)
        assert result.paths == ()
        assert fake.seen == [GraphPaths(from_id="a", to_id="b")]

    async def test_a_call_is_refused_while_the_switch_is_off(self):
        board = SwitchBoard(on=True, graph=False)
        fake = FakeGraphExpand()
        surface = _surface(search=FakeSearch(), switch_source=board.source, graph_expand=fake)
        knowledge_base = _knowledge_base(surface)

        with pytest.raises(ModelRetry, match="No knowledge graph"):
            await knowledge_base.direct_call_tool("graph_expand", {"seed_ids": ["mu.brine"]})

        assert fake.seen == []

    async def test_a_fixed_mode_surface_has_no_such_tool_to_call_at_all(self):
        surface = _surface(search=FakeSearch(), switch_source=None)
        knowledge_base = _knowledge_base(surface)

        with pytest.raises(KeyError, match="graph_expand"):
            await knowledge_base.direct_call_tool("graph_expand", {"seed_ids": ["mu.brine"]})


class TestMultiSurfaceGraphRouting:
    async def test_a_single_earner_routes_direct_with_no_knowledge_base_argument(self):
        kitchen_graph = FakeGraphExpand()
        wiki_graph = FakeGraphExpand()
        kitchen = _surface(
            name="kitchen", collection="kitchen", search=FakeSearch(), switch_source=SwitchBoard(on=True, graph=True).source,
            graph_expand=kitchen_graph,
        )
        wiki = _surface(
            name="wiki", collection="wiki", search=FakeSearch(), switch_source=SwitchBoard(on=True, graph=False).source,
            graph_expand=wiki_graph,
        )
        knowledge_base = RetrievalKnowledgeBase[None](surfaces=(kitchen, wiki), dials_source=ReadDials)

        result = await knowledge_base.direct_call_tool("graph_expand", {"seed_ids": ["mu.brine"]})

        assert isinstance(result, GraphResult)
        assert kitchen_graph.seen
        assert wiki_graph.seen == []

    async def test_naming_the_earning_knowledge_base_explicitly_also_works(self):
        kitchen_graph = FakeGraphExpand()
        kitchen = _surface(
            name="kitchen", collection="kitchen", search=FakeSearch(), switch_source=SwitchBoard(on=True, graph=True).source,
            graph_expand=kitchen_graph,
        )
        wiki = _surface(name="wiki", collection="wiki", search=FakeSearch(), switch_source=SwitchBoard(on=True, graph=False).source)
        knowledge_base = RetrievalKnowledgeBase[None](surfaces=(kitchen, wiki), dials_source=ReadDials)

        await knowledge_base.direct_call_tool("graph_expand", {"seed_ids": ["mu.brine"], "knowledge_base": "kitchen"})

        assert kitchen_graph.seen

    async def test_naming_a_non_earning_knowledge_base_is_refused_and_lists_only_the_earner(self):
        kitchen = _surface(name="kitchen", collection="kitchen", search=FakeSearch(), switch_source=SwitchBoard(on=True, graph=True).source)
        wiki = _surface(name="wiki", collection="wiki", search=FakeSearch(), switch_source=SwitchBoard(on=True, graph=False).source)
        knowledge_base = RetrievalKnowledgeBase[None](surfaces=(kitchen, wiki), dials_source=ReadDials)

        with pytest.raises(ModelRetry, match="No knowledge base named 'wiki'") as exc:
            await knowledge_base.direct_call_tool("graph_expand", {"seed_ids": ["mu.brine"], "knowledge_base": "wiki"})

        # The refusal names only the knowledge base that HAS an available graph — 'wiki' (the one it just
        # refused) appears once, in the opening clause, never offered back as an option.
        message = str(exc.value)
        assert "kitchen" in message
        assert message.count("wiki") == 1

    async def test_naming_a_knowledge_base_when_none_earns_points_at_search(self):
        kitchen = _surface(name="kitchen", collection="kitchen", search=FakeSearch(), switch_source=SwitchBoard(on=True, graph=False).source)
        wiki = _surface(name="wiki", collection="wiki", search=FakeSearch(), switch_source=SwitchBoard(on=True, graph=False).source)
        knowledge_base = RetrievalKnowledgeBase[None](surfaces=(kitchen, wiki), dials_source=ReadDials)

        with pytest.raises(ModelRetry, match="Use `search` instead"):
            await knowledge_base.direct_call_tool("graph_expand", {"seed_ids": ["mu.brine"], "knowledge_base": "wiki"})

    async def test_two_simultaneous_earners_require_a_knowledge_base_name(self):
        kitchen = _surface(name="kitchen", collection="kitchen", search=FakeSearch(), switch_source=SwitchBoard(on=True, graph=True).source)
        wiki = _surface(name="wiki", collection="wiki", search=FakeSearch(), switch_source=SwitchBoard(on=True, graph=True).source)
        knowledge_base = RetrievalKnowledgeBase[None](surfaces=(kitchen, wiki), dials_source=ReadDials)

        with pytest.raises(ModelRetry, match="Pass `knowledge_base`"):
            await knowledge_base.direct_call_tool("graph_expand", {"seed_ids": ["mu.brine"]})


class TestGraphDefaultKnowledgeBaseSourceRouting:
    """An omitted `knowledge_base` consults `default_knowledge_base_source` first, exactly as `search` does."""

    async def test_an_omitted_knowledge_base_routes_to_the_default_when_it_currently_earns(self):
        kitchen_graph = FakeGraphExpand()
        wiki_graph = FakeGraphExpand()
        kitchen = _surface(
            name="kitchen",
            collection="kitchen",
            search=FakeSearch(),
            switch_source=SwitchBoard(on=True, graph=True).source,
            graph_expand=kitchen_graph,
        )
        wiki = _surface(
            name="wiki",
            collection="wiki",
            search=FakeSearch(),
            switch_source=SwitchBoard(on=True, graph=True).source,
            graph_expand=wiki_graph,
        )
        knowledge_base = RetrievalKnowledgeBase[None](surfaces=(kitchen, wiki), dials_source=ReadDials, cross_search=True, default_knowledge_base_source=lambda: "wiki")

        result = await knowledge_base.direct_call_tool("graph_expand", {"seed_ids": ["mu.brine"]})

        assert isinstance(result, GraphResult)
        assert wiki_graph.seen
        assert kitchen_graph.seen == []

    async def test_a_default_naming_a_surface_that_does_not_currently_earn_falls_through(self):
        # `wiki` is the host's default, but only `kitchen` currently earns a graph: today's
        # sole-earner rule takes over rather than refusing.
        kitchen_graph = FakeGraphExpand()
        kitchen = _surface(
            name="kitchen",
            collection="kitchen",
            search=FakeSearch(),
            switch_source=SwitchBoard(on=True, graph=True).source,
            graph_expand=kitchen_graph,
        )
        wiki = _surface(name="wiki", collection="wiki", search=FakeSearch(), switch_source=SwitchBoard(on=True, graph=False).source)
        knowledge_base = RetrievalKnowledgeBase[None](surfaces=(kitchen, wiki), dials_source=ReadDials, cross_search=True, default_knowledge_base_source=lambda: "wiki")

        await knowledge_base.direct_call_tool("graph_expand", {"seed_ids": ["mu.brine"]})

        assert kitchen_graph.seen

    async def test_a_named_knowledge_base_still_overrides_the_default(self):
        kitchen_graph = FakeGraphExpand()
        wiki_graph = FakeGraphExpand()
        kitchen = _surface(
            name="kitchen",
            collection="kitchen",
            search=FakeSearch(),
            switch_source=SwitchBoard(on=True, graph=True).source,
            graph_expand=kitchen_graph,
        )
        wiki = _surface(
            name="wiki",
            collection="wiki",
            search=FakeSearch(),
            switch_source=SwitchBoard(on=True, graph=True).source,
            graph_expand=wiki_graph,
        )
        knowledge_base = RetrievalKnowledgeBase[None](surfaces=(kitchen, wiki), dials_source=ReadDials, cross_search=True, default_knowledge_base_source=lambda: "wiki")

        await knowledge_base.direct_call_tool("graph_expand", {"seed_ids": ["mu.brine"], "knowledge_base": "kitchen"})

        assert kitchen_graph.seen
        assert wiki_graph.seen == []

    async def test_the_all_literal_is_refused_even_with_a_default_source(self):
        kitchen = _surface(name="kitchen", collection="kitchen", search=FakeSearch(), switch_source=SwitchBoard(on=True, graph=True).source)
        wiki = _surface(name="wiki", collection="wiki", search=FakeSearch(), switch_source=SwitchBoard(on=True, graph=False).source)
        knowledge_base = RetrievalKnowledgeBase[None](surfaces=(kitchen, wiki), dials_source=ReadDials, cross_search=True, default_knowledge_base_source=lambda: "kitchen")

        with pytest.raises(ModelRetry, match="one knowledge base"):
            await knowledge_base.direct_call_tool("graph_expand", {"seed_ids": ["mu.brine"], "knowledge_base": "all"})

    async def test_the_all_refusal_never_says_no_knowledge_base_named(self):
        kitchen = _surface(name="kitchen", collection="kitchen", search=FakeSearch(), switch_source=SwitchBoard(on=True, graph=True).source)
        wiki = _surface(name="wiki", collection="wiki", search=FakeSearch(), switch_source=SwitchBoard(on=True, graph=False).source)
        knowledge_base = RetrievalKnowledgeBase[None](surfaces=(kitchen, wiki), dials_source=ReadDials)

        with pytest.raises(ModelRetry) as exc:
            await knowledge_base.direct_call_tool("graph_expand", {"seed_ids": ["mu.brine"], "knowledge_base": "all"})

        assert "No knowledge base named" not in str(exc.value)

    async def test_the_all_refusal_lists_only_the_current_earners(self):
        kitchen = _surface(name="kitchen", collection="kitchen", search=FakeSearch(), switch_source=SwitchBoard(on=True, graph=True).source)
        wiki = _surface(name="wiki", collection="wiki", search=FakeSearch(), switch_source=SwitchBoard(on=True, graph=False).source)
        knowledge_base = RetrievalKnowledgeBase[None](surfaces=(kitchen, wiki), dials_source=ReadDials)

        with pytest.raises(ModelRetry, match="kitchen") as exc:
            await knowledge_base.direct_call_tool("graph_expand", {"seed_ids": ["mu.brine"], "knowledge_base": "all"})

        assert "wiki" not in str(exc.value)

    async def test_find_paths_honors_the_same_default(self):
        kitchen_paths = FakeFindPaths()
        wiki_paths = FakeFindPaths()
        kitchen = _surface(
            name="kitchen",
            collection="kitchen",
            search=FakeSearch(),
            switch_source=SwitchBoard(on=True, graph=True).source,
            find_paths=kitchen_paths,
        )
        wiki = _surface(
            name="wiki",
            collection="wiki",
            search=FakeSearch(),
            switch_source=SwitchBoard(on=True, graph=True).source,
            find_paths=wiki_paths,
        )
        knowledge_base = RetrievalKnowledgeBase[None](surfaces=(kitchen, wiki), dials_source=ReadDials, cross_search=True, default_knowledge_base_source=lambda: "wiki")

        await knowledge_base.direct_call_tool("find_paths", {"from_id": "a", "to_id": "b"})

        assert wiki_paths.seen
        assert kitchen_paths.seen == []


class TestGraphSwitchReadCounts:
    async def test_listing_tools_reads_the_switch_once(self):
        board = SwitchBoard(on=True, graph=True)
        surface = _surface(search=FakeSearch(), switch_source=board.source)

        await _tool_names(_knowledge_base(surface))

        assert board.calls == 1

    async def test_a_call_reads_again_independently_of_the_listing_read(self):
        board = SwitchBoard(on=True, graph=True)
        surface = _surface(search=FakeSearch(), switch_source=board.source, graph_expand=FakeGraphExpand())
        knowledge_base = _knowledge_base(surface)

        await _tool_names(knowledge_base)
        await knowledge_base.direct_call_tool("graph_expand", {"seed_ids": ["mu.brine"]})

        assert board.calls == 2

    async def test_the_first_earner_short_circuits_the_listing_check(self):
        kitchen_board = SwitchBoard(on=True, graph=True)
        wiki_board = SwitchBoard(on=True, graph=True)
        kitchen = _surface(name="kitchen", collection="kitchen", search=FakeSearch(), switch_source=kitchen_board.source)
        wiki = _surface(name="wiki", collection="wiki", search=FakeSearch(), switch_source=wiki_board.source)
        knowledge_base = RetrievalKnowledgeBase[None](surfaces=(kitchen, wiki), dials_source=ReadDials)

        await _tool_names(knowledge_base)

        assert kitchen_board.calls == 1
        assert wiki_board.calls == 0  # the listing check stops at the first surface that already earned it


class TestGraphSwitchFlipsMidSession:
    async def test_flipping_off_hides_the_tools_and_the_next_direct_call_refuses(self):
        board = SwitchBoard(on=True, graph=True)
        fake = FakeGraphExpand()
        surface = _surface(search=FakeSearch(), switch_source=board.source, graph_expand=fake)
        knowledge_base = _knowledge_base(surface)

        assert await _tool_names(knowledge_base) == {"search", "fetch", "graph_expand", "find_paths"}
        await knowledge_base.direct_call_tool("graph_expand", {"seed_ids": ["mu.brine"]})
        assert fake.seen  # earned and callable while the switch is on

        board.graph = False

        assert await _tool_names(knowledge_base) == {"search", "fetch"}
        with pytest.raises(ModelRetry, match="Use `search` instead"):
            await knowledge_base.direct_call_tool("graph_expand", {"seed_ids": ["mu.brine"]})


REPO_PAGE = "page:repo-docs/paper.graph"
RESEARCH_PAGE = "page:research/paper.graph"
RESEARCH_CLAIM = "unit:research/paper.graph#c1"


def _two_bases(
    *,
    repo_pages: FakePages,
    repo_units: FakeUnits,
    research_pages: FakePages,
    research_units: FakeUnits,
    hits: tuple[dict[str, object], ...],
    cross_search: bool = False,
) -> RetrievalKnowledgeBase[None]:
    """One reader over two knowledge bases that share a bare id, each with its own body and stamp.

    `cross_search` on gives the id-only `fetch` that tries each base in turn; off keeps the
    `knowledge_base`-naming one.
    """
    return RetrievalKnowledgeBase[None](
        cross_search=cross_search,
        surfaces=(
            _surface(
                name="repo-docs",
                collection="repo-docs",
                search=FakeSearch(rows=hits),
                hybrid=False,
                pages=repo_pages,
                units=repo_units,
                stamp=_stamp("repo-docs", 7),
            ),
            _surface(
                name="research",
                collection="research",
                search=FakeSearch(rows=hits),
                hybrid=False,
                pages=research_pages,
                units=research_units,
                stamp=_stamp("research", 0),
            ),
        )
    )


class TestAHitsRefReadsTheBaseThatAnsweredIt:
    """Two knowledge bases in one reader holding the SAME bare id: the returned ref decides who answers."""

    async def test_a_page_hits_ref_reads_its_own_base_and_stamp(self):
        repo_pages = FakePages(bodies={REPO_PAGE: "repo body"})
        repo_fetch = FakeUnits(units={"paper.graph": {"id": "paper.graph", "level": "card", "title": "Graph", "body": "repo body"}})
        research_pages = FakePages(bodies={RESEARCH_PAGE: "research body"})
        knowledge_base = _two_bases(
            repo_pages=repo_pages,
            repo_units=repo_fetch,
            research_pages=research_pages,
            research_units=FakeUnits(),
            hits=(_hit("paper.graph", 1.0),),
        )

        result = await knowledge_base.direct_call_tool("search", {"query": "graph", "knowledge_base": "research"})

        assert isinstance(result, SearchResult)
        assert result.hits[0].id == "paper.graph"
        assert result.hits[0].ref == RESEARCH_PAGE
        fetched = await knowledge_base.direct_call_tool("fetch", {"unit_id": result.hits[0].ref})
        assert isinstance(fetched, FetchEnvelope)
        assert fetched.content == "research body"
        assert fetched.ref == RESEARCH_PAGE
        assert fetched.knowledge_base == KnowledgeBaseStamp(slug="research", changed_since=0)
        assert repo_fetch.calls == []
        assert repo_pages.calls == []

    async def test_a_flat_units_ref_reads_its_own_base_and_stamp(self):
        repo_fetch = FakeUnits(units={"paper.graph#c1": {"id": "paper.graph#c1", "level": "claim", "title": "C", "body": "repo claim"}})
        research_fetch = FakeUnits(units={"paper.graph#c1": {"id": "paper.graph#c1", "level": "claim", "title": "C", "body": "research claim"}})
        knowledge_base = _two_bases(
            repo_pages=FakePages(),
            repo_units=repo_fetch,
            research_pages=FakePages(),
            research_units=research_fetch,
            hits=(_hit("paper.graph#c1", 1.0, hit_level="claim"),),
        )

        result = await knowledge_base.direct_call_tool("search", {"query": "graph", "knowledge_base": "research"})

        assert isinstance(result, SearchResult)
        assert result.hits[0].ref == RESEARCH_CLAIM
        fetched = await knowledge_base.direct_call_tool("fetch", {"unit_id": result.hits[0].ref})
        assert isinstance(fetched, FetchResult)
        assert fetched.body == "research claim"
        assert fetched.ref == RESEARCH_CLAIM
        assert fetched.knowledge_base == KnowledgeBaseStamp(slug="research", changed_since=0)
        assert repo_fetch.calls == []

    async def test_a_legacy_page_form_comes_back_canonical_under_the_base_it_named(self):
        research_pages = FakePages(bodies={RESEARCH_PAGE: "research body"})
        knowledge_base = _two_bases(
            repo_pages=FakePages(bodies={REPO_PAGE: "repo body"}),
            repo_units=FakeUnits(),
            research_pages=research_pages,
            research_units=FakeUnits(),
            hits=(),
        )

        fetched = await knowledge_base.direct_call_tool("fetch", {"knowledge_base": "research", "unit_id": "page:paper.graph"})

        assert isinstance(fetched, FetchEnvelope)
        assert fetched.ref == RESEARCH_PAGE
        assert research_pages.calls == [RESEARCH_PAGE]

    async def test_a_legacy_section_form_comes_back_canonical_under_the_base_it_named(self):
        section = "section:research/paper.graph#findings"
        research_pages = FakePages(bodies={section: "research findings"})
        knowledge_base = _two_bases(
            repo_pages=FakePages(),
            repo_units=FakeUnits(),
            research_pages=research_pages,
            research_units=FakeUnits(),
            hits=(),
        )

        fetched = await knowledge_base.direct_call_tool(
            "fetch", {"knowledge_base": "research", "unit_id": "section:paper.graph#findings"}
        )

        assert isinstance(fetched, FetchEnvelope)
        assert fetched.ref == section
        assert research_pages.calls == [section]

    async def test_an_unqualified_id_alone_still_asks_which_base_to_read(self):
        knowledge_base = _two_bases(
            repo_pages=FakePages(bodies={REPO_PAGE: "repo body"}),
            repo_units=FakeUnits(),
            research_pages=FakePages(bodies={RESEARCH_PAGE: "research body"}),
            research_units=FakeUnits(),
            hits=(),
        )

        with pytest.raises(ModelRetry, match="repo-docs, research"):
            await knowledge_base.direct_call_tool("fetch", {"unit_id": "paper.graph"})

    async def test_a_ref_from_an_unregistered_collection_is_refused_and_drives_no_handler(self):
        repo_pages = FakePages(bodies={REPO_PAGE: "repo body"})
        research_pages = FakePages(bodies={RESEARCH_PAGE: "research body"})
        knowledge_base = _two_bases(
            repo_pages=repo_pages,
            repo_units=FakeUnits(),
            research_pages=research_pages,
            research_units=FakeUnits(),
            hits=(),
        )

        with pytest.raises(ModelRetry, match="repo-docs, research"):
            await knowledge_base.direct_call_tool("fetch", {"unit_id": "page:unregistered/paper.graph"})

        assert repo_pages.calls == []
        assert research_pages.calls == []

    async def test_a_named_base_never_answers_another_bases_ref(self):
        repo_pages = FakePages(bodies={REPO_PAGE: "repo body"})
        research_pages = FakePages(bodies={RESEARCH_PAGE: "research body"})
        knowledge_base = _two_bases(
            repo_pages=repo_pages,
            repo_units=FakeUnits(),
            research_pages=research_pages,
            research_units=FakeUnits(),
            hits=(),
        )

        with pytest.raises(ModelRetry, match="different knowledge base"):
            await knowledge_base.direct_call_tool("fetch", {"knowledge_base": "repo-docs", "unit_id": RESEARCH_PAGE})

        assert repo_pages.calls == []
        assert research_pages.calls == []


class TestAMalformedRefIsAnInstruction:
    """A handle the ref grammar cannot read is a retryable refusal, never a raw error out of the tool.

    Both leveled read paths are covered: the one that names its base, and the id-only one that tries
    each base in turn. The fan-out keeps the grammar's own message, because "no unit found" would
    send an agent re-grounding an id that was never the problem.
    """

    def _bases(self, *, cross_search: bool = False) -> RetrievalKnowledgeBase[None]:
        return _two_bases(
            repo_pages=FakePages(bodies={REPO_PAGE: "repo body"}),
            repo_units=FakeUnits(),
            research_pages=FakePages(bodies={RESEARCH_PAGE: "research body"}),
            research_units=FakeUnits(),
            hits=(),
            cross_search=cross_search,
        )

    @pytest.mark.parametrize("unit_id", ["group:research", "collection:repo-docs/paper.graph"])
    async def test_a_named_read_refuses_a_malformed_ref(self, unit_id: str):
        with pytest.raises(ModelRetry, match="Could not read ref"):
            await self._bases().direct_call_tool("fetch", {"knowledge_base": "research", "unit_id": unit_id})

    @pytest.mark.parametrize("unit_id", ["group:research", "collection:repo-docs/paper.graph"])
    async def test_the_fan_out_refuses_a_malformed_ref_with_the_grammars_own_message(self, unit_id: str):
        with pytest.raises(ModelRetry, match="Could not read ref"):
            await self._bases(cross_search=True).direct_call_tool("fetch", {"unit_id": unit_id})

    async def test_an_id_no_base_holds_still_reports_the_plain_not_found(self):
        """The generic refusal stays for the case it was written for: a well-formed id nobody has."""
        with pytest.raises(ModelRetry, match=re.escape("No unit found with id 'paper.absent'")):
            await self._bases(cross_search=True).direct_call_tool("fetch", {"unit_id": "paper.absent"})

    async def test_the_flat_dial_fan_out_also_keeps_the_grammars_message(self):
        knowledge_base = RetrievalKnowledgeBase[None](
            cross_search=True,
            dials_source=lambda: ReadDials(fetch="flat"),
            surfaces=(
                _surface(name="repo-docs", collection="repo-docs", search=FakeSearch(), units=FakeUnits()),
                _surface(name="research", collection="research", search=FakeSearch(), units=FakeUnits()),
            ),
        )

        with pytest.raises(ModelRetry, match="Could not read ref"):
            await knowledge_base.direct_call_tool("fetch", {"unit_id": "group:research"})


def _reached(node_id: str, *, title: str = "Brine basics") -> GraphNode:
    """One reached node as a handler projects it: fetchable id, qualified ref, lifecycle."""
    return GraphNode(id=node_id, ref=f"page:kitchen/{node_id}", type="note", title=title, status="verified", superseded=False)


def _asserted(src_id: str, dst_id: str) -> GraphEdge:
    """One authored relation with the claim that asserts it, both handles qualified."""
    return GraphEdge(src_id=src_id, dst_id=dst_id, rel="cites", provenance_claim=f"{src_id}#c1", provenance_ref=f"unit:kitchen/{src_id}#c1")


class TestGraphAnswersCarryTheSurfaceThatServedThem:
    async def test_an_expansion_carries_the_answering_surfaces_stamp(self):
        board = SwitchBoard(on=True, graph=True)
        nodes = (_reached("mu.brine"), _reached("mu.salt"))
        expand = FakeGraphExpand(result=GraphResult(nodes=nodes, edges=(_asserted("mu.brine", "mu.salt"),)))
        surface = _surface(search=FakeSearch(), switch_source=board.source, graph_expand=expand, stamp=_stamp("kitchen", 0))

        result = await _knowledge_base(surface).direct_call_tool("graph_expand", {"seed_ids": ["mu.brine"]})

        assert isinstance(result, GraphResult)
        assert result.knowledge_base == KnowledgeBaseStamp(slug="kitchen", changed_since=0)
        assert {node.ref for node in result.nodes} == {"page:kitchen/mu.brine", "page:kitchen/mu.salt"}
        assert result.next_steps is not None
        assert "unit:kitchen/mu.brine#c1" in " ".join(result.next_steps.options)

    async def test_routes_carry_the_answering_surfaces_stamp(self):
        board = SwitchBoard(on=True, graph=True)
        route = GraphPath(nodes=(_reached("mu.brine"), _reached("mu.salt")), edges=(_asserted("mu.brine", "mu.salt"),), score=0.5)
        paths = FakeFindPaths(result=(route,))
        surface = _surface(search=FakeSearch(), switch_source=board.source, find_paths=paths, stamp=_stamp("kitchen", 0))

        result = await _knowledge_base(surface).direct_call_tool("find_paths", {"from_id": "mu.brine", "to_id": "mu.salt"})

        assert isinstance(result, GraphPathsResult)
        assert result.knowledge_base == KnowledgeBaseStamp(slug="kitchen", changed_since=0)
        assert result.paths == (route,)
        assert result.window == ResultWindow(returned_items=1, omitted_items=0, truncated=False)

    async def test_the_currency_opt_in_reaches_the_traversal(self):
        board = SwitchBoard(on=True, graph=True)
        expand = FakeGraphExpand()
        surface = _surface(search=FakeSearch(), switch_source=board.source, graph_expand=expand)

        await _knowledge_base(surface).direct_call_tool("graph_expand", {"seed_ids": ["mu.brine"], "include_superseded": True})

        assert expand.seen == [GraphExpand(seed_ids=("mu.brine",), include_superseded=True)]

    async def test_an_empty_route_set_still_offers_the_next_two_calls(self):
        board = SwitchBoard(on=True, graph=True)
        surface = _surface(search=FakeSearch(), switch_source=board.source, find_paths=FakeFindPaths(), stamp=_stamp("kitchen", 0))

        result = await _knowledge_base(surface).direct_call_tool("find_paths", {"from_id": "mu.brine", "to_id": "mu.salt"})

        assert isinstance(result, GraphPathsResult)
        options = " ".join(result.next_steps.options)
        assert "graph_expand(" in options
        assert "include_superseded" in options


    async def test_an_empty_route_set_says_how_far_it_looked_and_offers_a_longer_search(self):
        board = SwitchBoard(on=True, graph=True)
        paths = FakeFindPaths()
        surface = _surface(search=FakeSearch(), switch_source=board.source, find_paths=paths, stamp=_stamp("kitchen", 0))

        result = await _knowledge_base(surface).direct_call_tool("find_paths", {"from_id": "mu.brine", "to_id": "mu.salt"})

        assert isinstance(result, GraphPathsResult)
        assert result.next_steps.summary == "No route from 'mu.brine' to 'mu.salt' within 4 hops."
        assert "find_paths(from_id='mu.brine', to_id='mu.salt', max_hops=8)" in result.next_steps.options[0]

    async def test_a_route_search_at_its_limit_says_a_longer_route_is_not_ruled_out(self):
        board = SwitchBoard(on=True, graph=True)
        paths = FakeFindPaths()
        surface = _surface(search=FakeSearch(), switch_source=board.source, find_paths=paths, stamp=_stamp("kitchen", 0))

        result = await _knowledge_base(surface).direct_call_tool("find_paths", {"from_id": "mu.brine", "to_id": "mu.salt", "max_hops": 20})

        assert isinstance(result, GraphPathsResult)
        assert paths.seen[-1].max_hops == 20
        assert result.next_steps.summary == (
            "No route from 'mu.brine' to 'mu.salt' within 8 hops. Routes are searched up to 8 hops, so a longer one is not ruled out."
        )
        assert not any("max_hops=8" in option for option in result.next_steps.options)


class TestAGraphRefusalSurvivesTheSurface:
    """The in-process door stamps a refusal; it never re-reads it as an empty neighbourhood."""

    async def test_a_refused_expansion_keeps_its_refusal_and_gains_the_stamp(self):
        giant = "z" * 20_000
        refused = bound_graph_result(
            (_reached(giant),),
            (),
            knowledge_base=None,
            next_steps=NextSteps(summary="A neighbourhood."),
        )
        board = SwitchBoard(on=True, graph=True)
        surface = _surface(
            search=FakeSearch(), switch_source=board.source, graph_expand=FakeGraphExpand(result=refused), stamp=_stamp("kitchen", 0)
        )

        result = await _knowledge_base(surface).direct_call_tool("graph_expand", {"seed_ids": [giant]})

        assert isinstance(result, GraphResult)
        assert result.nodes == ()
        assert result.window.refused is True
        assert result.knowledge_base == KnowledgeBaseStamp(slug="kitchen", changed_since=0)
        assert result.next_steps is not None
        assert "No current node was reached" not in result.next_steps.summary
        assert giant not in result.model_dump_json()
