"""The read-tools capability: however many knowledge bases ride it, the agent holds two tools.

Three claims under test. **One surface keeps the plain ergonomics** — `search(query, k)`, a
no-argument `fetch` opening the knowledge base map. **Several surfaces share the SAME two tools** —
`search` grows a `knowledge_base` argument, a no-argument `fetch` lists the knowledge bases themselves,
and a ref is answered by the surface that recognizes it. **The voice is per knowledge base** — each
surface speaks its own domain and guidance; the mechanics stay in the tool descriptions.
"""

from __future__ import annotations

import re

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

import pytest

from core.ai.decision_models import DecisionModelQuestions
from core.ai.disclosure.contracts import DisclosureEnvelope
from core.ai.retrieval.contracts import KnowledgeBaseStamp, ResultWindow
from core.testing.fakes import FakeDecisionModel
from pydantic_ai.exceptions import ModelRetry
from pydantic_ai.models import override_allow_model_requests
from pydantic_ai.models.test import TestModel
from pydantic_ai.tools import RunContext
from pydantic_ai.usage import RunUsage

from engram.retrieval.capability import ReadSurface, RetrievalKnowledgeBase
from engram.retrieval.dials import ReadDials
from engram.retrieval.domain import Search, SearchResult, SqlResult
from engram.retrieval.envelope import BadRefError, parse_ref
from engram.retrieval.rerank import RerankedSearch, Reranking
from engram.retrieval.tools import TOOL_NAMES, RetrievalHandlers

if TYPE_CHECKING:
    from collections.abc import Callable

    from core.ai.disclosure.contracts import DisclosureMode
    from core.ai.retrieval.contracts import SupportsSearch
    from mcp import types as mcp_types
    from pydantic_ai.models.decision import DecisionRequest, NoulQuestion

SEARCH_COLUMNS = ("id", "hit_level", "page_title", "snippet", "score")


def _hit(unit_id: str, score: float) -> dict[str, object]:
    return {"id": unit_id, "hit_level": "card", "page_title": unit_id, "snippet": "…", "score": score}


@dataclass
class FakeSearch:
    """Scripted ranked rows; records the collection each query was scoped to."""

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


def _envelope(ref: str) -> DisclosureEnvelope:
    return DisclosureEnvelope(ref=ref, kind="collection", title=ref, summary="a level", mode="strict")


def _surface(
    name: str,
    collection: str,
    domain: str,
    *,
    search: SupportsSearch[Search] | None = None,
    flat: FakeFlatFetch | None = None,
    envelope: FakeEnvelope | None = None,
    guidance: str | None = None,
) -> ReadSurface:
    root = f"collection:{collection}"
    return ReadSurface(
        name=name,
        collection=collection,
        handlers=RetrievalHandlers(
            search=search or FakeSearch(),
            fetch=flat or FakeFlatFetch(),
            graph_expand=_NeverWalks(),
            find_paths=_NeverWalks(),
        ),
        envelope=envelope or FakeEnvelope(known={root: _envelope(root)}),
        domain=domain,
        guidance=None if guidance is None else lambda: guidance,
    )


def _ctx() -> RunContext[None]:
    return RunContext(deps=None, model=TestModel(), usage=RunUsage())


WORKDOCS = "Working notes for a pickling kitchen: brine logs, ferment hubs, lifecycle docs."
RESEARCH = "A research wiki on fermentation science, indexed into claims with provenance."


class TestOneSurface:
    async def test_the_tools_are_search_and_fetch(self):
        knowledge_base = RetrievalKnowledgeBase[None](surfaces=(_surface("kitchen", "kitchen", WORKDOCS),))
        tools = await knowledge_base.get_toolset().get_tools(_ctx())
        assert sorted(tools) == ["fetch", "search"]

    async def test_a_bare_fetch_opens_the_knowledge_base_map(self):
        knowledge_base = RetrievalKnowledgeBase[None](surfaces=(_surface("kitchen", "kitchen", WORKDOCS),))
        result = await knowledge_base.direct_call_tool("fetch", {})
        assert isinstance(result, DisclosureEnvelope)
        assert result.ref == "collection:kitchen"

    def test_the_voice_is_domain_plus_guidance_and_function_shaped(self):
        knowledge_base = RetrievalKnowledgeBase[None](surfaces=(_surface("kitchen", "kitchen", WORKDOCS, guidance="Open one unit at a time."),))
        compose = knowledge_base.get_instructions()
        assert callable(compose)  # a literal here would render ahead of the agent's identity
        text = compose(_ctx())
        assert text.startswith(WORKDOCS)
        assert "Open one unit at a time." in text

    async def test_a_hit_carries_its_own_surfaces_name(self):
        search = FakeSearch(rows=(_hit("mu.brine", 0.9),))
        knowledge_base = RetrievalKnowledgeBase[None](surfaces=(_surface("kitchen", "kitchen", WORKDOCS, search=search),))
        result = await knowledge_base.direct_call_tool("search", {"query": "brine"})
        assert isinstance(result, SearchResult)
        hits = result.hits
        assert [hit.knowledge_base for hit in hits] == ["kitchen"]


class TestTheDescriptionNamesOnlyThisDoorsOwnTools:
    """This door serves the structured tools and no SQL, so its `search` text offers no SQL read."""

    async def test_the_search_text_never_sends_the_caller_to_run_sql(self):
        knowledge_base = RetrievalKnowledgeBase[None](surfaces=(_surface("kitchen", "kitchen", WORKDOCS),))

        tools = await knowledge_base.get_toolset().get_tools(_ctx())
        description = tools["search"].tool_def.description or ""

        assert "run_sql" not in description
        assert "api.about" not in description
        assert "`collection`" not in description, "this search takes no `collection`, so its text never offers one"


class TestSeveralSurfaces:
    """Several knowledge bases with `cross_search` on: one call may read past a single base."""

    def _knowledge_base(
        self, *, kitchen_search: FakeSearch | None = None, wiki_search: FakeSearch | None = None, wiki_flat: FakeFlatFetch | None = None
    ) -> RetrievalKnowledgeBase[None]:
        return RetrievalKnowledgeBase[None](
            surfaces=(
                _surface("kitchen", "kitchen", WORKDOCS, search=kitchen_search, guidance="Prefer the map."),
                _surface("wiki", "wiki", RESEARCH, search=wiki_search, flat=wiki_flat),
            ),
            cross_search=True,
        )

    async def test_a_bare_fetch_lists_the_knowledge_bases(self):
        result = await self._knowledge_base().direct_call_tool("fetch", {})
        assert isinstance(result, DisclosureEnvelope)
        assert result.kind == "library"
        assert [child.title for child in result.children] == ["kitchen", "wiki"]
        assert [child.ref for child in result.children] == ["collection:kitchen", "collection:wiki"]

    async def test_a_knowledge_base_name_opens_its_own_map(self):
        result = await self._knowledge_base().direct_call_tool("fetch", {"unit_id": "wiki"})
        assert isinstance(result, DisclosureEnvelope)
        assert result.ref == "collection:wiki"

    async def test_an_unknown_collection_ref_names_the_knowledge_bases(self):
        with pytest.raises(ModelRetry, match="kitchen, wiki"):
            await self._knowledge_base().direct_call_tool("fetch", {"unit_id": "collection:nope"})

    async def test_a_page_ref_is_answered_by_the_surface_that_recognizes_it(self):
        wiki_page = _envelope("page:wiki/ferment.kimchi")
        knowledge_base = RetrievalKnowledgeBase[None](
            surfaces=(
                _surface("kitchen", "kitchen", WORKDOCS),
                _surface("wiki", "wiki", RESEARCH, envelope=FakeEnvelope(known={"page:wiki/ferment.kimchi": wiki_page})),
            ),
            cross_search=True,
        )
        result = await knowledge_base.direct_call_tool("fetch", {"unit_id": "ferment.kimchi"})
        assert isinstance(result, DisclosureEnvelope)
        assert result.ref == wiki_page.ref

    async def test_an_id_outside_every_leveled_surface_falls_back_to_the_flat_read(self):
        flat = FakeFlatFetch(units={"claim.acidity": {"id": "claim.acidity", "level": "claim", "title": "Acidity", "body": "pH drops."}})
        result = await self._knowledge_base(wiki_flat=flat).direct_call_tool("fetch", {"unit_id": "claim.acidity"})
        assert getattr(result, "id", None) == "claim.acidity"

    async def test_an_unknown_id_asks_for_regrounding(self):
        with pytest.raises(ModelRetry, match="Re-ground"):
            await self._knowledge_base().direct_call_tool("fetch", {"unit_id": "nothing.here"})

    async def test_a_base_whose_fused_scores_run_lower_still_places_its_best_hit_beside_the_others(self):
        # Fused scores are comparable only within one base: a base that ran both legs scores about twice one
        # that ran its keyword leg alone, so a merge by score would bury the second base's best hit.
        kitchen = FakeSearch(rows=(_hit("mu.brine", 0.032), _hit("mu.jar", 0.031), _hit("mu.crock", 0.030)))
        wiki = FakeSearch(rows=(_hit("paper.lacto", 0.016),))
        result = await self._knowledge_base(kitchen_search=kitchen, wiki_search=wiki).direct_call_tool("search", {"query": "brine", "k": 3})
        assert isinstance(result, SearchResult)
        assert [hit.id for hit in result.hits] == ["mu.brine", "paper.lacto", "mu.jar"]

    async def test_search_merges_every_knowledge_base_by_rank_and_caps_k(self):
        kitchen = FakeSearch(rows=(_hit("mu.brine", 0.9), _hit("mu.jar", 0.2)))
        wiki = FakeSearch(rows=(_hit("paper.lacto", 0.7),))
        result = await self._knowledge_base(kitchen_search=kitchen, wiki_search=wiki).direct_call_tool("search", {"query": "brine", "k": 2})
        assert isinstance(result, SearchResult)
        hits = result.hits
        assert [hit.id for hit in hits] == ["mu.brine", "paper.lacto"]

    async def test_a_re_ranked_one_hit_base_merges_by_probability_with_a_three_hit_base(self):
        probabilities = {"brine": 0.9, "jar": 0.2, "crock": 0.1, "lacto": 0.6}

        def by_snippet(request: DecisionRequest, _name: str, _question: NoulQuestion) -> float:
            return next(value for word, value in probabilities.items() if word in str(request.state))

        def reranked(rows: tuple[dict[str, object], ...]) -> RerankedSearch[Search]:
            rerank = Reranking(questions=DecisionModelQuestions(FakeDecisionModel(yes=by_snippet)))
            return rerank.over(FakeSearch(rows=rows), FakeFlatFetch())

        def row(unit_id: str, word: str) -> dict[str, object]:
            return {**_hit(unit_id, 0.5), "snippet": word}

        knowledge_base = RetrievalKnowledgeBase[None](
            surfaces=(
                _surface("kitchen", "kitchen", WORKDOCS, search=reranked((row("mu.jar", "jar"), row("mu.brine", "brine"), row("mu.crock", "crock")))),
                _surface("wiki", "wiki", RESEARCH, search=reranked((row("paper.lacto", "lacto"),))),
            ),
            cross_search=True,
        )
        with override_allow_model_requests(allow_model_requests=True):
            result = await knowledge_base.direct_call_tool("search", {"query": "pickling", "k": 3})

        assert isinstance(result, SearchResult)
        assert [(hit.id, hit.rerank_score, hit.score) for hit in result.hits] == [
            ("mu.brine", 0.9, 0.5),
            ("paper.lacto", 0.6, 0.5),
            ("mu.jar", 0.2, 0.5),
        ]

    async def test_search_scopes_to_one_knowledge_base_by_name(self):
        kitchen = FakeSearch(rows=(_hit("mu.brine", 0.9),))
        wiki = FakeSearch(rows=(_hit("paper.lacto", 0.7),))
        knowledge_base = self._knowledge_base(kitchen_search=kitchen, wiki_search=wiki)
        result = await knowledge_base.direct_call_tool("search", {"query": "brine", "knowledge_base": "wiki"})
        assert isinstance(result, SearchResult)
        hits = result.hits
        assert [hit.id for hit in hits] == ["paper.lacto"]
        assert kitchen.seen == []
        assert wiki.seen[0].collection == "wiki"

    async def test_an_unknown_knowledge_base_name_teaches_the_valid_ones(self):
        with pytest.raises(ModelRetry, match="kitchen, wiki"):
            await self._knowledge_base().direct_call_tool("search", {"query": "brine", "knowledge_base": "nope"})

    async def test_an_unknown_knowledge_base_name_also_teaches_the_all_literal(self):
        with pytest.raises(ModelRetry, match='knowledge_base="all"'):
            await self._knowledge_base().direct_call_tool("search", {"query": "brine", "knowledge_base": "nope"})

    async def test_search_merges_every_knowledge_base_and_stamps_each_hits_own_surface(self):
        kitchen = FakeSearch(rows=(_hit("mu.brine", 0.9),))
        wiki = FakeSearch(rows=(_hit("paper.lacto", 0.7),))
        result = await self._knowledge_base(kitchen_search=kitchen, wiki_search=wiki).direct_call_tool("search", {"query": "brine"})
        assert isinstance(result, SearchResult)
        hits = result.hits
        assert {hit.id: hit.knowledge_base for hit in hits} == {"mu.brine": "kitchen", "paper.lacto": "wiki"}

    def test_the_voice_names_each_knowledge_base(self):
        text = self._knowledge_base().get_instructions()(_ctx())
        assert text.startswith("## Your knowledge bases")
        assert "`kitchen`" in text
        assert WORKDOCS in text
        assert RESEARCH in text
        assert "Prefer the map." in text


class TestDefaultKnowledgeBaseSource:
    """A host-owned default scopes an omitted `knowledge_base`; `all` always fans out regardless."""

    def _knowledge_base(
        self,
        *,
        kitchen_search: FakeSearch | None = None,
        wiki_search: FakeSearch | None = None,
        default_knowledge_base_source: Callable[[], str | None] | None = None,
    ) -> RetrievalKnowledgeBase[None]:
        return RetrievalKnowledgeBase[None](
            surfaces=(
                _surface("kitchen", "kitchen", WORKDOCS, search=kitchen_search),
                _surface("wiki", "wiki", RESEARCH, search=wiki_search),
            ),
            cross_search=True,
            default_knowledge_base_source=default_knowledge_base_source,
        )

    async def test_an_omitted_knowledge_base_scopes_to_the_surface_the_source_names(self):
        kitchen = FakeSearch(rows=(_hit("mu.brine", 0.9),))
        wiki = FakeSearch(rows=(_hit("paper.lacto", 0.7),))
        knowledge_base = self._knowledge_base(kitchen_search=kitchen, wiki_search=wiki, default_knowledge_base_source=lambda: "wiki")
        result = await knowledge_base.direct_call_tool("search", {"query": "brine"})
        assert isinstance(result, SearchResult)
        hits = result.hits
        assert [hit.id for hit in hits] == ["paper.lacto"]
        assert kitchen.seen == []

    async def test_the_all_literal_always_fans_out_even_with_a_default_source(self):
        kitchen = FakeSearch(rows=(_hit("mu.brine", 0.9),))
        wiki = FakeSearch(rows=(_hit("paper.lacto", 0.7),))
        knowledge_base = self._knowledge_base(kitchen_search=kitchen, wiki_search=wiki, default_knowledge_base_source=lambda: "wiki")
        result = await knowledge_base.direct_call_tool("search", {"query": "brine", "knowledge_base": "all"})
        assert isinstance(result, SearchResult)
        hits = result.hits
        assert {hit.id for hit in hits} == {"mu.brine", "paper.lacto"}

    async def test_a_source_naming_a_non_surface_falls_back_to_every_surface(self):
        kitchen = FakeSearch(rows=(_hit("mu.brine", 0.9),))
        wiki = FakeSearch(rows=(_hit("paper.lacto", 0.7),))
        knowledge_base = self._knowledge_base(kitchen_search=kitchen, wiki_search=wiki, default_knowledge_base_source=lambda: "nope")
        result = await knowledge_base.direct_call_tool("search", {"query": "brine"})
        assert isinstance(result, SearchResult)
        hits = result.hits
        assert {hit.id for hit in hits} == {"mu.brine", "paper.lacto"}

    async def test_a_source_naming_none_falls_back_to_every_surface(self):
        kitchen = FakeSearch(rows=(_hit("mu.brine", 0.9),))
        wiki = FakeSearch(rows=(_hit("paper.lacto", 0.7),))
        knowledge_base = self._knowledge_base(kitchen_search=kitchen, wiki_search=wiki, default_knowledge_base_source=lambda: None)
        result = await knowledge_base.direct_call_tool("search", {"query": "brine"})
        assert isinstance(result, SearchResult)
        hits = result.hits
        assert {hit.id for hit in hits} == {"mu.brine", "paper.lacto"}

    async def test_a_named_knowledge_base_still_overrides_the_default(self):
        kitchen = FakeSearch(rows=(_hit("mu.brine", 0.9),))
        wiki = FakeSearch(rows=(_hit("paper.lacto", 0.7),))
        knowledge_base = self._knowledge_base(kitchen_search=kitchen, wiki_search=wiki, default_knowledge_base_source=lambda: "wiki")
        result = await knowledge_base.direct_call_tool("search", {"query": "brine", "knowledge_base": "kitchen"})
        assert isinstance(result, SearchResult)
        hits = result.hits
        assert [hit.id for hit in hits] == ["mu.brine"]

    async def test_with_no_default_source_an_omitted_knowledge_base_still_fans_out(self):
        kitchen = FakeSearch(rows=(_hit("mu.brine", 0.9),))
        wiki = FakeSearch(rows=(_hit("paper.lacto", 0.7),))
        knowledge_base = self._knowledge_base(kitchen_search=kitchen, wiki_search=wiki)
        result = await knowledge_base.direct_call_tool("search", {"query": "brine"})
        assert isinstance(result, SearchResult)
        hits = result.hits
        assert {hit.id for hit in hits} == {"mu.brine", "paper.lacto"}

    async def test_the_description_says_an_omitted_knowledge_base_takes_the_chosen_base_when_a_default_is_set(self):
        description = await self._search_description(self._knowledge_base(default_knowledge_base_source=lambda: "wiki"))
        assert "the base chosen for you" in description
        assert "session" not in description, "engram names no host concept; which base is chosen is the host's business"
        assert 'knowledge_base="all"' in description
        assert "kitchen" in description
        assert "wiki" in description

    async def test_the_description_keeps_the_fan_out_wording_with_no_default_source(self):
        description = await self._search_description(self._knowledge_base())
        assert 'knowledge_base="all"' in description

    @staticmethod
    async def _search_description(knowledge_base: RetrievalKnowledgeBase[None]) -> str:
        [tool] = [published for published in await knowledge_base.list_tools() if published.name == "search"]
        return tool.description or ""


class TestSearchDescriptionNamesTheDefaultPerRequest:
    """Each request's `search` description names the base `default_knowledge_base_source` currently picks.

    Distinct from `TestDefaultKnowledgeBaseSource`'s description tests: those read `list_tools()` (the
    construction-time text, generic on purpose); these read the PER-REQUEST text `_dialed_defs`
    builds, which is only rendered once the toolset is wrapped (a `dials_source` here).
    """

    def _knowledge_base(self, *, default_knowledge_base_source: Callable[[], str | None] | None = None) -> RetrievalKnowledgeBase[None]:
        return RetrievalKnowledgeBase[None](
            surfaces=(
                _surface("kitchen", "kitchen", WORKDOCS),
                _surface("wiki", "wiki", RESEARCH),
            ),
            cross_search=True,
            default_knowledge_base_source=default_knowledge_base_source,
            dials_source=ReadDials,
        )

    async def _descriptions(self, knowledge_base: RetrievalKnowledgeBase[None]) -> dict[str, str]:
        tools = await knowledge_base.get_toolset().get_tools(_ctx())
        return {name: tool.tool_def.description or "" for name, tool in tools.items()}

    async def test_the_hybrid_variant_names_the_chosen_base(self):
        described = await self._descriptions(self._knowledge_base(default_knowledge_base_source=lambda: "wiki"))
        assert "omitting it searches `wiki`" in described["search"]

    async def test_the_keyword_dial_variant_also_names_the_chosen_base(self):
        knowledge_base = RetrievalKnowledgeBase[None](
            surfaces=(_surface("kitchen", "kitchen", WORKDOCS), _surface("wiki", "wiki", RESEARCH)),
            cross_search=True,
            default_knowledge_base_source=lambda: "wiki",
            dials_source=lambda: ReadDials(search="keyword"),
        )
        tools = await knowledge_base.get_toolset().get_tools(_ctx())
        description = tools["search"].tool_def.description or ""
        assert "omitting it searches `wiki`" in description
        assert "keyword ranking" in description  # still the keyword-dial tuning

    async def test_a_source_naming_a_non_surface_keeps_the_generic_chosen_wording(self):
        described = await self._descriptions(self._knowledge_base(default_knowledge_base_source=lambda: "nope"))
        assert "the base chosen for you" in described["search"]

    async def test_with_no_source_the_fan_out_wording_stays(self):
        described = await self._descriptions(self._knowledge_base())
        assert "searches them all together" in described["search"]


STRUCTURE = "Structure: brine logs are pages; a log lists its entries."


class TestTheMapTeachesTheTerritory:
    """Framing a map with the knowledge base's structure is `EnvelopeHandler`'s job now.

    (`test_envelope_handler.py::TestOrientationFallback` pins "about first, structure follows,
    orientation alone when about is silent"). This capability layer adds nothing of its own to a
    surface's envelope any more — `_map`/`_open` serve exactly what `surface.envelope` returns.
    These pin that pass-through by scripting `FakeEnvelope` with the SAME already-framed content a
    real `EnvelopeHandler` would have produced, so the expected `content` values below are
    unchanged from what this class asserted when the framing lived here.
    """

    async def test_a_bare_fetch_serves_the_knowledge_base_structure_as_the_maps_content(self):
        root = "collection:kitchen"
        framed = _envelope(root).model_copy(update={"content": STRUCTURE})
        knowledge_base = RetrievalKnowledgeBase[None](surfaces=(_surface("kitchen", "kitchen", WORKDOCS, envelope=FakeEnvelope(known={root: framed})),))
        result = await knowledge_base.direct_call_tool("fetch", {})
        assert isinstance(result, DisclosureEnvelope)
        assert result.content == STRUCTURE

    async def test_a_knowledge_base_opened_by_name_or_ref_carries_its_structure(self):
        root = "collection:kitchen"
        framed = _envelope(root).model_copy(update={"content": STRUCTURE})
        knowledge_base = RetrievalKnowledgeBase[None](
            surfaces=(
                _surface("kitchen", "kitchen", WORKDOCS, envelope=FakeEnvelope(known={root: framed})),
                _surface("wiki", "wiki", RESEARCH),
            ),
            cross_search=True,
        )
        by_name = await knowledge_base.direct_call_tool("fetch", {"unit_id": "kitchen"})
        by_ref = await knowledge_base.direct_call_tool("fetch", {"unit_id": "collection:kitchen"})
        assert isinstance(by_name, DisclosureEnvelope)
        assert isinstance(by_ref, DisclosureEnvelope)
        assert by_name.content == STRUCTURE
        assert by_ref.content == STRUCTURE

    async def test_a_maps_own_words_come_first_and_the_structure_follows(self):
        root = "collection:kitchen"
        authored = _envelope(root).model_copy(update={"content": f"The knowledge base speaks for itself.\n\n{STRUCTURE}"})
        surface = _surface("kitchen", "kitchen", WORKDOCS, envelope=FakeEnvelope(known={root: authored}))
        result = await RetrievalKnowledgeBase[None](surfaces=(surface,)).direct_call_tool("fetch", {})
        assert isinstance(result, DisclosureEnvelope)
        assert result.content == f"The knowledge base speaks for itself.\n\n{STRUCTURE}"

    async def test_pages_stay_untouched(self):
        page = DisclosureEnvelope(ref="page:kitchen/mu.brine", kind="memory-unit", title="Brine", summary="salt", mode="strict")
        surface = _surface("kitchen", "kitchen", WORKDOCS, envelope=FakeEnvelope(known={"page:kitchen/mu.brine": page}))
        result = await RetrievalKnowledgeBase[None](surfaces=(surface,)).direct_call_tool("fetch", {"unit_id": "mu.brine"})
        assert isinstance(result, DisclosureEnvelope)
        assert result.content is None


class TestNamingTheKnowledgeBaseIsMandatory:
    """With several knowledge bases and `cross_search` off, every read names the one base it reads.

    The refusals matter as much as the schema: a model that fumbles the argument gets the list of
    names back, because the name is the one thing it is missing and the tool description may be
    several turns behind by then.
    """

    def _knowledge_base(self, *, kitchen_search: FakeSearch | None = None, wiki_search: FakeSearch | None = None) -> RetrievalKnowledgeBase[None]:
        return RetrievalKnowledgeBase[None](
            surfaces=(
                _surface("kitchen", "kitchen", WORKDOCS, search=kitchen_search),
                _surface("wiki", "wiki", RESEARCH, search=wiki_search),
            )
        )

    async def _tool(self, knowledge_base: RetrievalKnowledgeBase[None], name: str) -> mcp_types.Tool:
        [tool] = [published for published in await knowledge_base.list_tools() if published.name == name]
        return tool

    async def test_search_declares_knowledge_base_required_in_its_own_schema(self):
        """Prevention before correction: the model cannot emit a nameless call in the first place."""
        tool = await self._tool(self._knowledge_base(), "search")

        assert "knowledge_base" in tool.input_schema["required"]

    async def test_fetch_accepts_a_qualified_ref_without_repeating_the_base(self):
        """A ref already names its base, so requiring the argument beside it would only invite a contradiction."""
        tool = await self._tool(self._knowledge_base(), "fetch")

        assert "knowledge_base" not in tool.input_schema.get("required", [])

    async def test_a_qualified_ref_alone_opens_the_base_it_names(self):
        kimchi = _envelope("page:wiki/ferment.kimchi")
        knowledge_base = RetrievalKnowledgeBase[None](
            surfaces=(
                _surface("kitchen", "kitchen", WORKDOCS),
                _surface("wiki", "wiki", RESEARCH, envelope=FakeEnvelope(known={"page:wiki/ferment.kimchi": kimchi})),
            )
        )

        result = await knowledge_base.direct_call_tool("fetch", {"unit_id": "page:wiki/ferment.kimchi"})

        assert isinstance(result, DisclosureEnvelope)
        assert result.ref == "page:wiki/ferment.kimchi"

    async def test_an_unqualified_id_alone_is_still_refused_with_the_names(self):
        with pytest.raises(ModelRetry, match="kitchen, wiki"):
            await self._knowledge_base().direct_call_tool("fetch", {"unit_id": "ferment.kimchi"})

    async def test_a_search_naming_no_base_is_refused_with_the_names(self):
        kitchen = FakeSearch(rows=(_hit("mu.brine", 0.9),))
        knowledge_base = self._knowledge_base(kitchen_search=kitchen)

        with pytest.raises(ModelRetry, match="kitchen, wiki"):
            await knowledge_base.direct_call_tool("search", {"query": "brine", "knowledge_base": ""})

        assert kitchen.seen == [], "a refused call reads nothing"

    async def test_a_search_naming_an_unknown_base_is_refused_with_the_names(self):
        with pytest.raises(ModelRetry, match=re.escape("No knowledge base named 'nope'. Your knowledge bases: kitchen, wiki.")):
            await self._knowledge_base().direct_call_tool("search", {"query": "brine", "knowledge_base": "nope"})

    async def test_the_all_literal_is_refused_with_the_names(self):
        """Cross-base search is the host's explicit choice, so `all` is a name the agent cannot use here."""
        kitchen = FakeSearch(rows=(_hit("mu.brine", 0.9),))
        wiki = FakeSearch(rows=(_hit("paper.lacto", 0.7),))
        knowledge_base = self._knowledge_base(kitchen_search=kitchen, wiki_search=wiki)

        with pytest.raises(ModelRetry, match=re.escape("Name one of: kitchen, wiki.")):
            await knowledge_base.direct_call_tool("search", {"query": "brine", "knowledge_base": "all"})

        assert kitchen.seen == []
        assert wiki.seen == []

    async def test_a_named_base_is_searched_and_only_it(self):
        kitchen = FakeSearch(rows=(_hit("mu.brine", 0.9),))
        wiki = FakeSearch(rows=(_hit("paper.lacto", 0.7),))
        knowledge_base = self._knowledge_base(kitchen_search=kitchen, wiki_search=wiki)

        result = await knowledge_base.direct_call_tool("search", {"query": "brine", "knowledge_base": "kitchen"})

        assert isinstance(result, SearchResult)
        hits = result.hits
        assert [hit.id for hit in hits] == ["mu.brine"]
        assert wiki.seen == []

    async def test_the_search_description_says_the_argument_is_required_and_lists_the_bases(self):
        tool = await self._tool(self._knowledge_base(), "search")

        description = tool.description or ""
        assert "`knowledge_base` names the one knowledge base to search and is required, one base per call:" in description
        assert "- 'kitchen'" in description
        assert "- 'wiki'" in description
        assert 'knowledge_base="all"' not in description, "the description must not teach a literal this reader refuses"

    async def test_the_fetch_description_names_the_required_argument_and_the_bases(self):
        tool = await self._tool(self._knowledge_base(), "fetch")

        assert "`knowledge_base` names the knowledge base to read and is required for an unqualified id ('kitchen', 'wiki')" in (tool.description or "")
        assert (
            "A `ref` from a hit or a listing already names its base, so pass one back exactly as returned and it needs no `knowledge_base`."
            in (tool.description or "")
        ), "the one handle that needs no `knowledge_base` has to be named where the argument is demanded"

    async def test_a_host_default_is_never_consulted_while_naming_is_mandatory(self):
        """The seam stays for a host that wants a default, but a default cannot stand in for a name."""
        kitchen = FakeSearch(rows=(_hit("mu.brine", 0.9),))
        knowledge_base = RetrievalKnowledgeBase[None](
            surfaces=(_surface("kitchen", "kitchen", WORKDOCS, search=kitchen), _surface("wiki", "wiki", RESEARCH)),
            default_knowledge_base_source=lambda: "kitchen",
        )

        with pytest.raises(ModelRetry, match="Name the knowledge base to search"):
            await knowledge_base.direct_call_tool("search", {"query": "brine", "knowledge_base": ""})

        assert kitchen.seen == []

    async def test_a_single_knowledge_base_keeps_the_plain_signature(self):
        """There is nothing to name with one base, so `search` never grows the argument."""
        knowledge_base = RetrievalKnowledgeBase[None](surfaces=(_surface("kitchen", "kitchen", WORKDOCS, search=FakeSearch(rows=(_hit("mu.brine", 0.9),))),))
        tool = await self._tool(knowledge_base, "search")

        assert "knowledge_base" not in tool.input_schema["properties"]

        result = await knowledge_base.direct_call_tool("search", {"query": "brine"})
        assert isinstance(result, SearchResult)
        hits = result.hits
        assert [hit.id for hit in hits] == ["mu.brine"]


class TestFetchReadsOnlyTheNamedKnowledgeBase:
    """A named `fetch` never answers from a base the agent did not name."""

    def _knowledge_base(self, *, wiki_envelope: FakeEnvelope | None = None, wiki_flat: FakeFlatFetch | None = None) -> RetrievalKnowledgeBase[None]:
        return RetrievalKnowledgeBase[None](
            surfaces=(
                _surface("kitchen", "kitchen", WORKDOCS),
                _surface("wiki", "wiki", RESEARCH, envelope=wiki_envelope, flat=wiki_flat),
            )
        )

    async def test_no_unit_id_opens_the_named_bases_own_map(self):
        result = await self._knowledge_base().direct_call_tool("fetch", {"knowledge_base": "wiki"})

        assert isinstance(result, DisclosureEnvelope)
        assert result.ref == "collection:wiki"

    async def test_an_id_that_lives_in_another_base_is_refused_naming_the_one_asked(self):
        """The id is real, but not here: answering it from `wiki` would read past the name given."""
        kimchi = _envelope("page:wiki/ferment.kimchi")
        knowledge_base = RetrievalKnowledgeBase[None](
            surfaces=(
                _surface("kitchen", "kitchen", WORKDOCS),
                _surface("wiki", "wiki", RESEARCH, envelope=FakeEnvelope(known={"page:wiki/ferment.kimchi": kimchi})),
            )
        )

        with pytest.raises(ModelRetry, match="'kitchen'"):
            await knowledge_base.direct_call_tool("fetch", {"knowledge_base": "kitchen", "unit_id": "ferment.kimchi"})

    async def test_an_id_in_the_named_base_is_answered(self):
        kimchi = _envelope("page:wiki/ferment.kimchi")
        knowledge_base = self._knowledge_base(wiki_envelope=FakeEnvelope(known={"page:wiki/ferment.kimchi": kimchi}))

        result = await knowledge_base.direct_call_tool("fetch", {"knowledge_base": "wiki", "unit_id": "ferment.kimchi"})

        assert isinstance(result, DisclosureEnvelope)
        assert result.ref == kimchi.ref

    async def test_an_id_outside_the_leveled_surface_falls_back_to_that_bases_flat_read(self):
        flat = FakeFlatFetch(units={"claim.acidity": {"id": "claim.acidity", "level": "claim", "title": "Acidity", "body": "pH drops."}})
        knowledge_base = self._knowledge_base(wiki_flat=flat)

        result = await knowledge_base.direct_call_tool("fetch", {"knowledge_base": "wiki", "unit_id": "claim.acidity"})

        assert getattr(result, "id", None) == "claim.acidity"

    async def test_an_unknown_base_is_refused_with_the_names(self):
        with pytest.raises(ModelRetry, match="kitchen, wiki"):
            await self._knowledge_base().direct_call_tool("fetch", {"knowledge_base": "nope"})

    async def test_an_unqualified_id_with_no_base_is_told_what_a_read_needs(self):
        """A read has two ways to name its base, and the refusal names both — not `search`'s one."""
        with pytest.raises(ModelRetry, match=re.escape("Name the knowledge base to read: pass `knowledge_base`")) as refusal:
            await self._knowledge_base().direct_call_tool("fetch", {"unit_id": "ferment.kimchi"})

        assert "`ref`" in str(refusal.value), "the handle that carries its own base is the other way out"
        assert "to search" not in str(refusal.value)


class TestOneKnowledgeBasePerCollection:
    """A ref names a collection, so two surfaces on one collection could never both be reachable."""

    async def test_two_surfaces_claiming_one_collection_are_refused_at_construction(self):
        with pytest.raises(ValueError, match="kitchen"):
            RetrievalKnowledgeBase[None](
                surfaces=(_surface("kitchen", "kitchen", WORKDOCS), _surface("pantry", "kitchen", RESEARCH))
            )

    async def test_two_knowledge_bases_on_their_own_collections_are_fine(self):
        knowledge_base = RetrievalKnowledgeBase[None](
            surfaces=(_surface("kitchen", "kitchen", WORKDOCS), _surface("wiki", "wiki", RESEARCH))
        )

        assert [surface.collection for surface in knowledge_base.surfaces] == ["kitchen", "wiki"]


def _ranked(unit_id: str, score: float, *, lexical_rank: int | None, vector_rank: int | None, snippet: str = "…") -> dict[str, object]:
    """One search row carrying the per-leg facts the generated grammar projects."""
    return {
        "id": unit_id,
        "hit_level": "card",
        "page_id": unit_id,
        "page_title": unit_id,
        "snippet": snippet,
        "score": score,
        "lexical_rank": lexical_rank,
        "vector_rank": vector_rank,
        "vector_distance": None if vector_rank is None else 0.22,
        "snippet_truncated": False,
    }


class TestTheInProcessSearchAnswersWithABoundedResult:
    """The in-process door answers the same bounded, support-labelled result the MCP door does."""

    @staticmethod
    def _base(search: FakeSearch, *, stamp: KnowledgeBaseStamp | None = None) -> RetrievalKnowledgeBase[None]:
        surface = _surface("kitchen", "kitchen", WORKDOCS, search=search)
        if stamp is not None:
            surface = ReadSurface(
                name=surface.name,
                collection=surface.collection,
                handlers=surface.handlers,
                envelope=surface.envelope,
                domain=surface.domain,
                stamp=lambda: _stamped(stamp),
            )
        return RetrievalKnowledgeBase[None](surfaces=(surface,))

    async def test_a_hit_carries_its_component_ranks_and_support(self):
        search = FakeSearch(rows=(_ranked("mu.brine", 0.9, lexical_rank=1, vector_rank=2),))

        result = await self._base(search).direct_call_tool("search", {"query": "brine"})

        assert isinstance(result, SearchResult)
        (hit,) = result.hits
        assert hit.lexical_rank == 1
        assert hit.vector_rank == 2
        assert hit.support == "lexical_and_semantic"
        assert result.status == "lexically_supported_candidates"

    async def test_vector_only_hits_are_semantic_candidates_only(self):
        search = FakeSearch(rows=(_ranked("mu.brine", 0.9, lexical_rank=None, vector_rank=1),))

        result = await self._base(search).direct_call_tool("search", {"query": "brine"})

        assert isinstance(result, SearchResult)
        assert result.status == "semantic_candidates_only"
        assert "nearest semantic candidates" in result.next_steps.summary
        assert "evidence unverified" in result.next_steps.summary
        assert "supported answer" not in result.next_steps.summary
        assert "grounded" not in result.next_steps.summary

    async def test_an_empty_search_is_no_hits_with_a_complete_window(self):
        result = await self._base(FakeSearch()).direct_call_tool("search", {"query": "brine"})

        assert isinstance(result, SearchResult)
        assert result.status == "no_hits"
        assert result.window == ResultWindow(returned_items=0, omitted_items=0, truncated=False)

    async def test_the_next_step_names_the_first_qualified_fetch_ref(self):
        search = FakeSearch(rows=(_ranked("mu.brine", 0.9, lexical_rank=1, vector_rank=None),))

        result = await self._base(search).direct_call_tool("search", {"query": "brine"})

        assert isinstance(result, SearchResult)
        assert "page:kitchen/mu.brine" in " ".join(result.next_steps.options)

    async def test_huge_snippets_are_bounded_and_the_whole_answer_fits_the_budget(self):
        body = "α β γ 段落 " * 4000
        rows = tuple(_ranked(f"mu.{index}", 1.0 - index / 100, lexical_rank=index + 1, vector_rank=None, snippet=body) for index in range(12))
        search = FakeSearch(rows=rows)

        result = await self._base(search).direct_call_tool("search", {"query": "brine"})

        assert isinstance(result, SearchResult)
        assert result.hits
        assert all(hit.snippet is not None and len(hit.snippet) <= 480 for hit in result.hits)
        assert all(hit.snippet_truncated for hit in result.hits)
        assert len(result.model_dump_json()) <= 12_000
        assert result.window.returned_items == len(result.hits)

    async def test_the_answer_carries_its_surfaces_stamp(self):
        search = FakeSearch(rows=(_ranked("mu.brine", 0.9, lexical_rank=1, vector_rank=None),))
        stamp = KnowledgeBaseStamp(slug="kitchen", changed_since=0)

        result = await self._base(search, stamp=stamp).direct_call_tool("search", {"query": "brine"})

        assert isinstance(result, SearchResult)
        assert result.knowledge_base == stamp


async def _stamped(stamp: KnowledgeBaseStamp) -> KnowledgeBaseStamp:
    """The stamp source shape a surface takes: awaited fresh on every answer."""
    return stamp


_MAP_OPTION = re.compile(r"fetch\(unit_id='(collection:[^']*)'\)")
"""How a next-step option offers a knowledge base's own map, so a test can read the ref back out."""


class TestTheMapOptionNamesARefThatReads:
    """A browse option is only useful if the ref it names opens in the base that offered it."""

    @staticmethod
    def _base(*surfaces: ReadSurface, cross_search: bool = False) -> RetrievalKnowledgeBase[None]:
        return RetrievalKnowledgeBase[None](surfaces=surfaces, cross_search=cross_search)

    async def test_a_base_whose_slug_differs_from_its_collection_offers_its_collections_ref(self):
        # Production wires these independently: `name` is the spec's slug, `collection` the partition.
        surface = _surface("wiki", "research-vault", RESEARCH, search=FakeSearch())

        result = await self._base(surface).direct_call_tool("search", {"query": "nothing here"})

        assert isinstance(result, SearchResult)
        matched = _MAP_OPTION.search(" ".join(result.next_steps.options))
        assert matched is not None, result.next_steps.options
        assert parse_ref(matched.group(1), "research-vault").collection == "research-vault"

    async def test_the_offered_ref_opens_the_map_of_the_base_that_offered_it(self):
        surface = _surface("wiki", "research-vault", RESEARCH, search=FakeSearch())
        knowledge_base = self._base(surface)

        result = await knowledge_base.direct_call_tool("search", {"query": "nothing here"})

        assert isinstance(result, SearchResult)
        matched = _MAP_OPTION.search(" ".join(result.next_steps.options))
        assert matched is not None
        opened = await knowledge_base.direct_call_tool("fetch", {"unit_id": matched.group(1)})
        assert isinstance(opened, DisclosureEnvelope)
        assert opened.ref == "collection:research-vault"

    async def test_a_fan_out_offers_no_map_at_all_rather_than_an_unreadable_one(self):
        knowledge_base = self._base(
            _surface("kitchen", "kitchen", WORKDOCS, search=FakeSearch()),
            _surface("wiki", "research-vault", RESEARCH, search=FakeSearch()),
            cross_search=True,
        )

        result = await knowledge_base.direct_call_tool("search", {"query": "nothing here"})

        assert isinstance(result, SearchResult)
        joined = " ".join(result.next_steps.options)
        assert "collection:" not in joined  # no single base answered, so no single map ref is truthful
        assert result.next_steps.options  # but the agent still gets a move

    async def test_a_semantic_only_fan_out_also_offers_no_map(self):
        rows = (_ranked("mu.brine", 0.9, lexical_rank=None, vector_rank=1),)
        knowledge_base = self._base(
            _surface("kitchen", "kitchen", WORKDOCS, search=FakeSearch(rows=rows)),
            _surface("wiki", "research-vault", RESEARCH, search=FakeSearch()),
            cross_search=True,
        )

        result = await knowledge_base.direct_call_tool("search", {"query": "brine"})

        assert isinstance(result, SearchResult)
        assert result.status == "semantic_candidates_only"
        assert "collection:" not in " ".join(result.next_steps.options)
