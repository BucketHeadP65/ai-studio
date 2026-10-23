"""`fetch` reads every id form the other read tools hand out.

`search` ends each answer naming its top hit by its exact `ref` (`page:`, `section:` or `unit:` and the
collection), a graph answer names its nodes and claims the same way, and a listing names its children
so. Every `fetch` must read that exact string, and the bare id must keep working beside it. The specs
drive the real handlers over a gateway that answers only the ids it holds, so a ref passed through to
the database unparsed reads nothing, exactly as the live store does. An id that names nothing is still
refused, and the refusal names the forms `fetch` takes.
"""

from __future__ import annotations

import ast
import json
import re

from dataclasses import dataclass, field
from typing import TYPE_CHECKING

import pytest

from core.ai.disclosure.contracts import DisclosureEnvelope
from mcp.client import Client
from mcp.server.mcpserver import MCPServer
from pydantic_ai import FunctionToolset, ModelRetry
from pydantic_ai.models.test import TestModel
from pydantic_ai.tools import RunContext
from pydantic_ai.usage import RunUsage

from engram.retrieval.capability import ReadSurface, RetrievalKnowledgeBase
from engram.retrieval.dials import ReadDials
from engram.retrieval.domain import FetchResult, GraphResult, SchemaDescription, Search, SearchResult, SqlResult
from engram.retrieval.envelope import EnvelopeHandler
from engram.retrieval.infrastructure.mcp_server import create_mcp_server
from engram.retrieval.queries import DescribeSchemaHandler, FetchHandler, GraphExpandHandler, GraphPathsHandler
from engram.retrieval.tools import RetrievalHandlers, add_routed_read_tools, build_retrieval_toolset, register_mcp_tools
from engram.testing.fakes.gateway import FakeSqlGateway

if TYPE_CHECKING:
    from collections.abc import Mapping

    from core.ai.disclosure.contracts import DisclosureMode
    from mcp.types import CallToolResult
    from pydantic_ai.toolsets import AbstractToolset

    from engram.retrieval.domain import Row


_HOME = "field-notes"
"""The knowledge base every single-source door here is built over."""

_OTHER = "bench-log"
"""A second knowledge base: a door over several reads it, a single-source door refuses its refs."""

_PAGE = "note.alpha"
_SECTION = "note.alpha#method"
_CLAIM = "note.alpha#c1"
_RAW = "src-alpha:2"

_LEVELS = (("card", _PAGE), ("section", _SECTION), ("claim", _CLAIM), ("raw", _RAW))
"""Each search hit level with the id of the unit a hit at that level names."""

_FETCH_LEVEL = {"card": "page", "section": "section", "claim": "claim", "raw": "raw"}
"""The `level` `api.fetch` answers for the unit a hit at each search level names."""

_FORMS = ("page:{collection}/<id>", "section:{collection}/<id>", "unit:{collection}/<id>", "bare id")
"""What a refusal names: each qualified form in the refusing knowledge base, and the bare id."""


def _unit(unit_id: str, level: str) -> Row:
    return {"id": unit_id, "level": level, "title": f"Title of {unit_id}", "body": f"Full text of {unit_id}."}


def _units() -> dict[str, Row]:
    return {unit_id: _unit(unit_id, _FETCH_LEVEL[hit_level]) for hit_level, unit_id in _LEVELS}


def _page_rows() -> tuple[Row, ...]:
    return (
        {"row_type": "page", "id": _PAGE, "page_type": "note", "title": "Alpha", "summary": "The alpha note."},
        {"row_type": "section", "id": _SECTION, "page_type": "section", "title": "Method", "summary": "How.", "body": "Method text."},
    )


def _graph_rows() -> tuple[Row, ...]:
    """One seed and one node it reaches through a relation a claim asserts."""
    seed = {"id": _PAGE, "type": "note", "title": "Alpha", "status": "active", "superseded": False}
    reached = {"id": "note.beta", "type": "note", "title": "Beta", "status": "active", "superseded": False}
    return (
        {**seed, "src_id": None, "dst_id": None, "rel": None, "provenance_claim": None},
        {**reached, "src_id": _PAGE, "dst_id": "note.beta", "rel": "cites", "provenance_claim": _CLAIM},
    )


@dataclass
class FakeUnitGateway:
    """A reader gateway over one knowledge base that answers only the ids it holds.

    `api.fetch` answers the unit bound as `unit_id`, the page and section envelopes answer the page or
    section bound by id, and `api.graph_expand` answers `graph`. A read bound to another collection
    answers nothing, as a collection-scoped function does.
    """

    collection: str
    units: dict[str, Row] = field(default_factory=dict)
    pages: dict[str, tuple[Row, ...]] = field(default_factory=dict)
    sections: dict[str, Row] = field(default_factory=dict)
    graph: tuple[Row, ...] = ()
    fetched: list[str] = field(default_factory=list)

    async def rows(self, sql: str, params: Mapping[str, object] | None = None, *, cap: int = 50) -> SqlResult:
        del cap
        bound = dict(params or {})
        if bound.get("collection") != self.collection:
            return SqlResult()
        if "api.fetch(" in sql:
            self.fetched.append(str(bound["unit_id"]))
            unit = self.units.get(str(bound["unit_id"]))
            return SqlResult(rows=(unit,) if unit else (), columns=("id", "level", "title", "body"))
        if "api.envelope_page(" in sql:
            return SqlResult(rows=self.pages.get(str(bound["page_id"]), ()))
        if "api.envelope_section(" in sql:
            section = self.sections.get(str(bound["section_id"]))
            return SqlResult(rows=(section,) if section else ())
        if "api.graph_expand(" in sql:
            return SqlResult(rows=self.graph)
        return SqlResult()

    async def api_objects(self) -> SchemaDescription:
        return SchemaDescription(objects=())


def _gateway(collection: str = _HOME) -> FakeUnitGateway:
    return FakeUnitGateway(
        collection=collection,
        units=_units(),
        pages={_PAGE: _page_rows()},
        sections={_SECTION: {"id": _SECTION, "title": "Method", "summary": "How.", "body": "Method text."}},
        graph=_graph_rows(),
    )


@dataclass
class FakeOneHitSearch:
    """A search handler that ranks one hit at one level, for whichever collection the call scopes."""

    hit_level: str = "card"
    unit_id: str = _PAGE

    async def __call__(self, query: Search) -> SqlResult:
        del query
        row: Row = {
            "id": self.unit_id,
            "hit_level": self.hit_level,
            "page_id": self.unit_id.split("#", 1)[0],
            "page_title": "Alpha",
            "snippet": "an excerpt",
            "score": 0.8,
        }
        return SqlResult(rows=(row,), columns=tuple(row))


@dataclass
class FakeNoHitSearch:
    """A search handler that ranks nothing."""

    async def __call__(self, query: Search) -> SqlResult:
        del query
        return SqlResult()


def _handlers(gateway: FakeUnitGateway, search: FakeOneHitSearch | FakeNoHitSearch | None = None) -> RetrievalHandlers:
    return RetrievalHandlers(
        search=search or FakeOneHitSearch(),
        fetch=FetchHandler(gateway, gateway.collection),
        graph_expand=GraphExpandHandler(gateway, gateway.collection),
        find_paths=GraphPathsHandler(gateway, gateway.collection),
    )


def _ctx() -> RunContext[None]:
    return RunContext(deps=None, model=TestModel(), usage=RunUsage())


async def _call(toolset: AbstractToolset[None], name: str, args: dict[str, object]) -> object:
    ctx = _ctx()
    tools = await toolset.get_tools(ctx)
    return await toolset.call_tool(name, args, ctx, tools[name])


def _named_fetch(options: tuple[str, ...]) -> str:
    """The exact `unit_id` the first `fetch(unit_id=...)` option names."""
    for option in options:
        if (match := re.search(r"fetch\(unit_id=('[^']*')\)", option)) is not None:
            return str(ast.literal_eval(match.group(1)))
    msg = f"no fetch option among {options!r}"
    raise AssertionError(msg)


def _assert_names_the_forms(message: str, collection: str) -> None:
    for form in _FORMS:
        assert form.format(collection=collection) in message, message


def _text(result: CallToolResult) -> str:
    block = result.content[0]
    assert block.type == "text"
    return block.text


class TestTheFlatToolsetReadsWhatSearchNames:
    """The eval's toolset with no envelope: `fetch` is the flat full-text read."""

    @pytest.mark.parametrize(("hit_level", "unit_id"), _LEVELS)
    async def test_the_exact_ref_the_next_step_names_reads_the_hit(self, hit_level: str, unit_id: str):
        gateway = _gateway()
        toolset = build_retrieval_toolset(_handlers(gateway, FakeOneHitSearch(hit_level, unit_id)), collection=_HOME)

        found = await _call(toolset, "search", {"query": "alpha"})
        assert isinstance(found, SearchResult)
        named = _named_fetch(found.next_steps.options)
        unit = await _call(toolset, "fetch", {"unit_id": named})

        assert named == found.hits[0].ref
        assert named.endswith(f":{_HOME}/{unit_id}")
        assert isinstance(unit, FetchResult)
        assert unit.id == unit_id
        assert unit.body == f"Full text of {unit_id}."

    @pytest.mark.parametrize(("hit_level", "unit_id"), _LEVELS)
    async def test_the_bare_id_still_reads_the_hit(self, hit_level: str, unit_id: str):
        gateway = _gateway()
        toolset = build_retrieval_toolset(_handlers(gateway, FakeOneHitSearch(hit_level, unit_id)), collection=_HOME)

        unit = await _call(toolset, "fetch", {"unit_id": unit_id})

        assert isinstance(unit, FetchResult)
        assert unit.id == unit_id

    async def test_the_database_is_asked_for_the_bare_id(self):
        gateway = _gateway()
        toolset = build_retrieval_toolset(_handlers(gateway), collection=_HOME)

        await _call(toolset, "fetch", {"unit_id": f"page:{_HOME}/{_PAGE}"})

        assert gateway.fetched == [_PAGE]

    async def test_a_graph_answer_names_refs_the_flat_fetch_reads(self):
        toolset = build_retrieval_toolset(_handlers(_gateway()), collection=_HOME)

        reached = await _call(toolset, "graph_expand", {"seed_ids": [_PAGE]})
        assert isinstance(reached, GraphResult)
        assert reached.next_steps is not None
        named = [ast.literal_eval(match) for match in re.findall(r"fetch\(unit_id=('[^']*')\)", " ".join(reached.next_steps.options))]
        units = [await _call(toolset, "fetch", {"unit_id": ref}) for ref in named]

        assert named == [f"page:{_HOME}/{_PAGE}", f"unit:{_HOME}/{_CLAIM}"]
        assert [unit.id for unit in units if isinstance(unit, FetchResult)] == [_PAGE, _CLAIM]

    async def test_an_unknown_id_is_refused_naming_the_forms(self):
        toolset = build_retrieval_toolset(_handlers(_gateway()), collection=_HOME)

        with pytest.raises(ModelRetry) as refused:
            await _call(toolset, "fetch", {"unit_id": f"page:{_HOME}/note.missing"})

        message = str(refused.value)
        assert "No unit found with id 'note.missing'" in message
        _assert_names_the_forms(message, _HOME)
        assert "`search`" in message

    async def test_a_ref_naming_another_knowledge_base_is_refused_naming_both(self):
        toolset = build_retrieval_toolset(_handlers(_gateway()), collection=_HOME)

        with pytest.raises(ModelRetry) as refused:
            await _call(toolset, "fetch", {"unit_id": f"page:{_OTHER}/{_PAGE}"})

        assert _HOME in str(refused.value)
        assert _OTHER in str(refused.value)

    async def test_a_map_ref_is_refused_because_a_flat_read_has_no_map(self):
        toolset = build_retrieval_toolset(_handlers(_gateway()), collection=_HOME)

        with pytest.raises(ModelRetry, match="no browsable map"):
            await _call(toolset, "fetch", {"unit_id": f"collection:{_HOME}"})

    async def test_the_map_refusal_is_the_one_the_flat_dial_gives(self):
        toolset = build_retrieval_toolset(_handlers(_gateway()), collection=_HOME)
        dials = ReadDials(fetch="flat")
        knowledge_base = RetrievalKnowledgeBase[None](surfaces=(_surface(_HOME),), dials_source=lambda: dials)

        with pytest.raises(ModelRetry) as from_toolset:
            await _call(toolset, "fetch", {"unit_id": f"collection:{_HOME}"})
        with pytest.raises(ModelRetry) as from_dial:
            await knowledge_base.direct_call_tool("fetch", {"unit_id": f"collection:{_HOME}"})

        assert str(from_toolset.value) == str(from_dial.value)

    async def test_a_search_with_no_hits_never_offers_a_map_the_flat_fetch_cannot_open(self):
        toolset = build_retrieval_toolset(_handlers(_gateway(), FakeNoHitSearch()), collection=_HOME)

        found = await _call(toolset, "search", {"query": "nothing"})

        assert isinstance(found, SearchResult)
        assert "collection:" not in " ".join(found.next_steps.options)

    async def test_the_description_names_both_forms(self):
        toolset = build_retrieval_toolset(_handlers(_gateway()), collection=_HOME)

        description = toolset.tools["fetch"].description or ""

        _assert_names_the_forms(description, _HOME)


class TestTheFlatMcpFetchReadsWhatSearchNames:
    """The single-source MCP pair with no envelope, refusing through a tool error."""

    @pytest.mark.parametrize(("hit_level", "unit_id"), _LEVELS)
    async def test_the_exact_ref_the_next_step_names_reads_the_hit(self, hit_level: str, unit_id: str):
        server = MCPServer("fetch-forms")
        register_mcp_tools(server, _handlers(_gateway(), FakeOneHitSearch(hit_level, unit_id)), collection=_HOME)

        async with Client(server) as client:
            found = json.loads(_text(await client.call_tool("search", {"query": "alpha"})))
            named = _named_fetch(tuple(found["next_steps"]["options"]))
            unit = await client.call_tool("fetch", {"unit_id": named})
            bare = await client.call_tool("fetch", {"unit_id": unit_id})

        assert unit.is_error is False
        assert json.loads(_text(unit))["id"] == unit_id
        assert json.loads(_text(bare))["id"] == unit_id

    async def test_an_unknown_id_fails_the_call_naming_the_forms(self):
        server = MCPServer("fetch-forms")
        register_mcp_tools(server, _handlers(_gateway()), collection=_HOME)

        async with Client(server) as client:
            failed = await client.call_tool("fetch", {"unit_id": "note.missing"})

        assert failed.is_error is True
        assert "No unit found with id 'note.missing'" in _text(failed)
        _assert_names_the_forms(_text(failed), _HOME)


class TestTheGraphOnlyMcpFetchReadsWhatTheGraphNames:
    """A server wired with no search handler serves the flat fetch beside the graph tools."""

    def _server(self) -> MCPServer:
        gateway = _gateway()
        return create_mcp_server(
            gateway,
            DescribeSchemaHandler(FakeSqlGateway()),
            fetch_handler=FetchHandler(gateway, _HOME),
            graph_expand_handler=GraphExpandHandler(gateway, _HOME),
            find_paths_handler=GraphPathsHandler(gateway, _HOME),
            collection=_HOME,
        )

    async def test_every_ref_a_graph_answer_names_reads_its_unit(self):
        async with Client(self._server()) as client:
            reached = json.loads(_text(await client.call_tool("graph_expand", {"seed_ids": [_PAGE]})))
            joined = " ".join(reached["next_steps"]["options"])
            named = [ast.literal_eval(match) for match in re.findall(r"fetch\(unit_id=('[^']*')\)", joined)]
            units = [json.loads(_text(await client.call_tool("fetch", {"unit_id": ref}))) for ref in named]

        assert named == [f"page:{_HOME}/{_PAGE}", f"unit:{_HOME}/{_CLAIM}"]
        assert [unit["id"] for unit in units] == [_PAGE, _CLAIM]

    async def test_an_unknown_id_fails_the_call_naming_the_forms_without_search(self):
        async with Client(self._server()) as client:
            failed = await client.call_tool("fetch", {"unit_id": f"unit:{_HOME}/note.missing#c9"})

        assert failed.is_error is True
        message = _text(failed)
        assert "No unit found with id 'note.missing#c9'" in message
        _assert_names_the_forms(message, _HOME)
        assert "search" not in message


class TestTheLeveledToolsetReadsWhatSearchNames:
    """The toolset with an envelope: pages and sections open leveled, claims and raw chunks read flat."""

    def _toolset(self, search: FakeOneHitSearch) -> FunctionToolset[object]:
        gateway = _gateway()
        return build_retrieval_toolset(_handlers(gateway, search), collection=_HOME, envelope=EnvelopeHandler(gateway, collection=_HOME))

    @pytest.mark.parametrize(("hit_level", "unit_id"), _LEVELS)
    async def test_the_exact_ref_the_next_step_names_reads_the_hit(self, hit_level: str, unit_id: str):
        toolset = self._toolset(FakeOneHitSearch(hit_level, unit_id))

        found = await _call(toolset, "search", {"query": "alpha"})
        assert isinstance(found, SearchResult)
        named = _named_fetch(found.next_steps.options)
        read = await _call(toolset, "fetch", {"unit_id": named})
        bare = await _call(toolset, "fetch", {"unit_id": unit_id})

        assert named.endswith(f":{_HOME}/{unit_id}")
        assert isinstance(read, (DisclosureEnvelope, FetchResult))
        assert isinstance(bare, (DisclosureEnvelope, FetchResult))
        assert read.ref == bare.ref == named

    async def test_an_unknown_id_is_refused_naming_the_forms(self):
        toolset = self._toolset(FakeOneHitSearch())

        with pytest.raises(ModelRetry) as refused:
            await _call(toolset, "fetch", {"unit_id": "note.missing"})

        assert "No unit found with id 'note.missing'" in str(refused.value)
        _assert_names_the_forms(str(refused.value), _HOME)

    async def test_the_description_names_both_forms(self):
        toolset = self._toolset(FakeOneHitSearch())

        description = toolset.tools["fetch"].description or ""

        _assert_names_the_forms(description, _HOME)


def _surface(name: str, search: FakeOneHitSearch | FakeNoHitSearch | None = None, *, mode: DisclosureMode = "strict") -> ReadSurface:
    gateway = _gateway(name)
    return ReadSurface(
        name=name,
        collection=name,
        handlers=_handlers(gateway, search),
        envelope=EnvelopeHandler(gateway, mode=mode, collection=name),
        domain=f"The {name} knowledge base.",
    )


class TestTheRoutedFetchReadsWhatSearchNames:
    """The routed pair: a ref naming a registered knowledge base opens in that one."""

    def _toolset(self, search: FakeOneHitSearch) -> FunctionToolset[object]:
        toolset = FunctionToolset[object]()
        add_routed_read_tools(toolset, (_surface(_HOME), _surface(_OTHER, search)), default_knowledge_base=_HOME)
        return toolset

    @pytest.mark.parametrize(("hit_level", "unit_id"), _LEVELS)
    async def test_the_exact_ref_from_another_knowledge_base_reads_there(self, hit_level: str, unit_id: str):
        toolset = self._toolset(FakeOneHitSearch(hit_level, unit_id))

        found = await _call(toolset, "search", {"query": "alpha", "collection": _OTHER})
        assert isinstance(found, SearchResult)
        named = _named_fetch(found.next_steps.options)
        read = await _call(toolset, "fetch", {"unit_id": named})

        assert named.endswith(f":{_OTHER}/{unit_id}")
        assert isinstance(read, (DisclosureEnvelope, FetchResult))
        assert read.ref == named

    async def test_a_bare_id_reads_the_default_knowledge_base(self):
        toolset = self._toolset(FakeOneHitSearch())

        read = await _call(toolset, "fetch", {"unit_id": _CLAIM})

        assert isinstance(read, FetchResult)
        assert read.ref == f"unit:{_HOME}/{_CLAIM}"

    async def test_an_unknown_id_is_refused_naming_the_forms_of_the_base_it_asked(self):
        toolset = self._toolset(FakeOneHitSearch())

        with pytest.raises(ModelRetry) as refused:
            await _call(toolset, "fetch", {"unit_id": f"unit:{_OTHER}/note.missing"})

        assert "No unit found with id 'note.missing'" in str(refused.value)
        _assert_names_the_forms(str(refused.value), _OTHER)

    async def test_a_ref_naming_an_unregistered_knowledge_base_is_refused_naming_the_registered_ones(self):
        toolset = self._toolset(FakeOneHitSearch())

        with pytest.raises(ModelRetry, match="Registered: field-notes, bench-log"):
            await _call(toolset, "fetch", {"unit_id": f"page:elsewhere/{_PAGE}"})


class TestTheCapabilityReadsWhatSearchNames:
    """`RetrievalKnowledgeBase`, under the leveled dial and under the flat one."""

    @pytest.mark.parametrize("dial", ["leveled", "flat"])
    @pytest.mark.parametrize(("hit_level", "unit_id"), _LEVELS)
    async def test_the_exact_ref_the_next_step_names_reads_the_hit(self, hit_level: str, unit_id: str, dial: str):
        dials = ReadDials(fetch="flat") if dial == "flat" else ReadDials()
        knowledge_base = RetrievalKnowledgeBase[None](surfaces=(_surface(_HOME, FakeOneHitSearch(hit_level, unit_id)),), dials_source=lambda: dials)

        found = await knowledge_base.direct_call_tool("search", {"query": "alpha"})
        assert isinstance(found, SearchResult)
        named = _named_fetch(found.next_steps.options)
        read = await knowledge_base.direct_call_tool("fetch", {"unit_id": named})
        bare = await knowledge_base.direct_call_tool("fetch", {"unit_id": unit_id})

        assert isinstance(read, (DisclosureEnvelope, FetchResult))
        assert isinstance(bare, (DisclosureEnvelope, FetchResult))
        assert read.ref == bare.ref
        assert read.ref is not None
        assert read.ref.endswith(f":{_HOME}/{unit_id}")

    async def test_a_flat_dial_search_with_no_hits_never_offers_a_map(self):
        dials = ReadDials(fetch="flat")
        knowledge_base = RetrievalKnowledgeBase[None](surfaces=(_surface(_HOME, FakeNoHitSearch()),), dials_source=lambda: dials)

        found = await knowledge_base.direct_call_tool("search", {"query": "nothing"})

        assert isinstance(found, SearchResult)
        assert "collection:" not in " ".join(found.next_steps.options)

    @pytest.mark.parametrize("dial", ["leveled", "flat"])
    async def test_an_unknown_id_is_refused_naming_the_forms(self, dial: str):
        dials = ReadDials(fetch="flat") if dial == "flat" else ReadDials()
        knowledge_base = RetrievalKnowledgeBase[None](surfaces=(_surface(_HOME),), dials_source=lambda: dials)

        with pytest.raises(ModelRetry) as refused:
            await knowledge_base.direct_call_tool("fetch", {"unit_id": "note.missing"})

        assert "No unit found with id 'note.missing'" in str(refused.value)
        _assert_names_the_forms(str(refused.value), _HOME)

    async def test_an_id_no_base_of_several_holds_is_refused_naming_the_forms(self):
        knowledge_base = RetrievalKnowledgeBase[None](surfaces=(_surface(_HOME), _surface(_OTHER)), cross_search=True)

        with pytest.raises(ModelRetry) as refused:
            await knowledge_base.direct_call_tool("fetch", {"unit_id": "note.missing"})

        message = str(refused.value)
        assert "No unit found with id 'note.missing'" in message
        _assert_names_the_forms(message, "<collection>")


class TestRefusalsNameTheToolsTheSurfaceServes:
    """A refusal sends the agent only to tools the surface registers, as its description does."""

    _GRAPH_ONLY = frozenset({"fetch", "graph_expand", "find_paths"})

    async def test_a_graph_only_flat_fetch_re_grounds_with_the_graph_tools(self):
        toolset = build_retrieval_toolset(_handlers(_gateway()), only=self._GRAPH_ONLY, collection=_HOME)

        with pytest.raises(ModelRetry) as refused:
            await _call(toolset, "fetch", {"unit_id": "note.missing"})

        message = str(refused.value)
        assert "Re-ground the id with `graph_expand` or `find_paths`, or the ids supplied with the task" in message
        assert "search" not in message

    async def test_a_graph_only_leveled_fetch_re_grounds_with_the_graph_tools(self):
        gateway = _gateway()
        toolset = build_retrieval_toolset(
            _handlers(gateway), only=self._GRAPH_ONLY, collection=_HOME, envelope=EnvelopeHandler(gateway, collection=_HOME)
        )

        with pytest.raises(ModelRetry) as refused:
            await _call(toolset, "fetch", {"unit_id": f"unit:{_HOME}/note.missing"})

        message = str(refused.value)
        assert "Re-ground the id with `graph_expand` or `find_paths`, or the ids supplied with the task" in message
        assert "search" not in message

    async def test_a_fetch_alone_re_grounds_with_the_ids_supplied_with_the_task(self):
        toolset = build_retrieval_toolset(_handlers(_gateway()), only=frozenset({"fetch"}), collection=_HOME)

        with pytest.raises(ModelRetry) as refused:
            await _call(toolset, "fetch", {"unit_id": "note.missing"})

        message = str(refused.value)
        assert "Re-ground the id with the ids supplied with the task" in message
        assert "graph_expand" not in message


class TestASearchOnlySurfaceOffersNoMap:
    """A `search_only` knowledge base withholds its map, so its `search` never offers one."""

    async def test_a_search_with_no_hits_offers_no_map(self):
        gateway = _gateway()
        envelope = EnvelopeHandler(gateway, mode="search_only", collection=_HOME)
        toolset = build_retrieval_toolset(_handlers(gateway, FakeNoHitSearch()), collection=_HOME, envelope=envelope)

        found = await _call(toolset, "search", {"query": "nothing"})

        assert isinstance(found, SearchResult)
        assert "collection:" not in " ".join(found.next_steps.options)

    async def test_a_strict_surface_still_offers_its_map(self):
        gateway = _gateway()
        envelope = EnvelopeHandler(gateway, collection=_HOME)
        toolset = build_retrieval_toolset(_handlers(gateway, FakeNoHitSearch()), collection=_HOME, envelope=envelope)

        found = await _call(toolset, "search", {"query": "nothing"})

        assert isinstance(found, SearchResult)
        assert f"fetch(unit_id='collection:{_HOME}')" in " ".join(found.next_steps.options)


class TestASingleSourceSearchReadsOnlyItsOwnCollection:
    """A single-source `search` refuses another collection, so it never hands out a ref its own `fetch` refuses."""

    async def test_another_collection_is_refused_naming_the_one_it_serves(self):
        toolset = build_retrieval_toolset(_handlers(_gateway()), collection=_HOME)

        with pytest.raises(ModelRetry) as refused:
            await _call(toolset, "search", {"query": "alpha", "collection": _OTHER})

        assert f"{_HOME!r}" in str(refused.value)

    async def test_its_own_collection_is_searched(self):
        toolset = build_retrieval_toolset(_handlers(_gateway()), collection=_HOME)

        found = await _call(toolset, "search", {"query": "alpha", "collection": _HOME})

        assert isinstance(found, SearchResult)
        assert found.hits[0].ref == f"page:{_HOME}/{_PAGE}"

    async def test_the_mcp_search_fails_the_call_naming_the_one_it_serves(self):
        server = MCPServer("fetch-forms")
        register_mcp_tools(server, _handlers(_gateway()), collection=_HOME)

        async with Client(server) as client:
            failed = await client.call_tool("search", {"query": "alpha", "collection": _OTHER})

        assert failed.is_error is True
        assert f"{_HOME!r}" in _text(failed)

    async def test_the_description_offers_no_other_collection(self):
        toolset = build_retrieval_toolset(_handlers(_gateway()), collection=_HOME)

        description = toolset.tools["search"].description or ""

        assert "search a different one" not in description
        assert f"'{_HOME}'" in description


class TestNoSurfaceOffersTheMapOfASearchOnlyKnowledgeBase:
    """A `search_only` knowledge base withholds its map, so no `search` over it offers one, routed or not."""

    async def test_the_routed_search_offers_no_map_of_a_search_only_base(self):
        toolset = FunctionToolset[object]()
        surfaces = (_surface(_HOME, FakeNoHitSearch(), mode="search_only"), _surface(_OTHER, FakeNoHitSearch()))
        add_routed_read_tools(toolset, surfaces, default_knowledge_base=_HOME)

        found = await _call(toolset, "search", {"query": "nothing"})

        assert isinstance(found, SearchResult)
        assert "collection:" not in " ".join(found.next_steps.options)

    async def test_the_routed_search_still_offers_the_map_of_a_strict_base(self):
        toolset = FunctionToolset[object]()
        surfaces = (_surface(_HOME, FakeNoHitSearch(), mode="search_only"), _surface(_OTHER, FakeNoHitSearch()))
        add_routed_read_tools(toolset, surfaces, default_knowledge_base=_HOME)

        found = await _call(toolset, "search", {"query": "nothing", "collection": _OTHER})

        assert isinstance(found, SearchResult)
        assert f"fetch(unit_id='collection:{_OTHER}')" in " ".join(found.next_steps.options)

    async def test_the_capability_search_offers_no_map_of_a_search_only_base(self):
        knowledge_base = RetrievalKnowledgeBase[None](surfaces=(_surface(_HOME, FakeNoHitSearch(), mode="search_only"),))

        found = await knowledge_base.direct_call_tool("search", {"query": "nothing"})

        assert isinstance(found, SearchResult)
        assert "collection:" not in " ".join(found.next_steps.options)

    async def test_the_capability_search_still_offers_the_map_of_a_strict_base(self):
        knowledge_base = RetrievalKnowledgeBase[None](surfaces=(_surface(_HOME, FakeNoHitSearch()),))

        found = await knowledge_base.direct_call_tool("search", {"query": "nothing"})

        assert isinstance(found, SearchResult)
        assert f"fetch(unit_id='collection:{_HOME}')" in " ".join(found.next_steps.options)


class TestRefusalsNameOnlyTheGraphToolsServed:
    """A refusal and a description name a graph tool only when the surface registers it."""

    async def test_a_toolset_serving_one_graph_tool_names_only_that_one(self):
        toolset = build_retrieval_toolset(_handlers(_gateway()), only=frozenset({"fetch", "graph_expand"}), collection=_HOME)

        with pytest.raises(ModelRetry) as refused:
            await _call(toolset, "fetch", {"unit_id": "note.missing"})

        message = str(refused.value)
        assert "Re-ground the id with `graph_expand`, or the ids supplied with the task" in message
        assert "find_paths" not in message

    async def test_a_routed_server_names_only_the_graph_tools_it_registers_not_the_ones_only_lists(self):
        gateway = _gateway()
        server = create_mcp_server(
            gateway,
            DescribeSchemaHandler(FakeSqlGateway()),
            fetch_handler=FetchHandler(gateway, _HOME),
            graph_expand_handler=GraphExpandHandler(gateway, _HOME),
            read_surfaces=(_surface(_HOME),),
            collection=_HOME,
            only=frozenset({"fetch", "graph_expand", "find_paths"}),
        )

        async with Client(server) as client:
            listing = await client.list_tools()
            failed = await client.call_tool("fetch", {"unit_id": f"unit:{_HOME}/note.missing"})

        assert "find_paths" not in {tool.name for tool in listing.tools}
        message = _text(failed)
        assert "Re-ground the id with `graph_expand`, or the ids supplied with the task" in message
        assert "find_paths" not in message

    async def test_a_fetch_alone_description_names_no_graph_tool(self):
        toolset = build_retrieval_toolset(_handlers(_gateway()), only=frozenset({"fetch"}), collection=_HOME)

        description = toolset.tools["fetch"].description or ""

        assert "graph_expand" not in description
        assert "find_paths" not in description
        assert "supplied with the task" in description

    async def test_a_one_graph_tool_description_names_only_that_one(self):
        toolset = build_retrieval_toolset(_handlers(_gateway()), only=frozenset({"fetch", "find_paths"}), collection=_HOME)

        description = toolset.tools["fetch"].description or ""

        assert "`find_paths`" in description
        assert "graph_expand" not in description
