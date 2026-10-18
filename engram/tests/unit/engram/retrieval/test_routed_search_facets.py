"""The `search` over several knowledge bases narrows by the facets each searched base's schema declares.

Each `ReadSurface` carries its knowledge base's declared facet keys. The routed `search` (the pair `studio mcp`
serves, inside `run_code` too) and the capability's `search` both take `facets` with the single-base meaning:
values for one key match any, several keys must all match. A key is checked against the knowledge bases the call
searches. One named base refuses a key it does not declare, naming its keys. Several searched bases take a key only
when every one declares it, and the refusal names each base's keys and asks for one knowledge base. The
descriptions name each served base's keys.
"""

from __future__ import annotations

import json

from dataclasses import replace
from typing import TYPE_CHECKING

import pytest

from mcp.server.fastmcp import FastMCP
from mcp.shared.memory import create_connected_server_and_client_session as connect_in_memory
from pydantic_ai import FunctionToolset, ModelRetry

from engram.retrieval.capability import RetrievalKnowledgeBase
from engram.retrieval.domain import SearchResult, SqlResult
from engram.retrieval.envelope import EnvelopeHandler
from engram.retrieval.infrastructure.mcp_server import create_mcp_server
from engram.retrieval.queries import FetchHandler, GraphExpandHandler, GraphPathsHandler, SearchHandler
from engram.retrieval.tools import add_routed_read_tools, register_routed_mcp_tools
from engram.testing.fakes.embedding import FakeEmbedding
from engram.testing.fakes.gateway import FakeSqlGateway
from tests.unit.engram.retrieval.test_mcp_server import _describe_handler, _fresh, _no_reach, _routed_surface, _schema
from tests.unit.engram.retrieval.test_retrieval_knowledge_base import RESEARCH, WORKDOCS, FakeSearch, _hit, _surface
from tests.unit.engram.retrieval.test_tools import _call, _text

if TYPE_CHECKING:
    from collections.abc import Callable

    from engram.retrieval.capability import ReadSurface
    from engram.retrieval.tools import RetrievalToolset

_KITCHEN_KEYS = ("shade", "texture")
_WIKI_KEYS = ("weight",)


def _kitchen(search: FakeSearch, keys: tuple[str, ...] = _KITCHEN_KEYS) -> ReadSurface:
    return replace(_surface("kitchen", "kitchen", WORKDOCS, search=search), facet_keys=keys)


def _wiki(search: FakeSearch, keys: tuple[str, ...] = _WIKI_KEYS) -> ReadSurface:
    return replace(_surface("wiki", "wiki", RESEARCH, search=search), facet_keys=keys)


def _routed(*surfaces: ReadSurface, k_source: Callable[[], int] | None = None) -> RetrievalToolset:
    toolset: RetrievalToolset = FunctionToolset()
    add_routed_read_tools(toolset, surfaces, default_knowledge_base=surfaces[0].name, k_source=k_source)
    return toolset


class TestRoutedTools:
    @pytest.mark.parametrize("k_source", [None, lambda: 4])
    async def test_the_facets_reach_the_base_the_call_names_and_no_other(self, k_source: Callable[[], int] | None):
        kitchen, wiki = FakeSearch(), FakeSearch()
        toolset = _routed(_kitchen(kitchen), _wiki(wiki), k_source=k_source)

        await _call(toolset, "search", {"query": "brine", "collection": "wiki", "facets": {"weight": ["heavy", "light"]}})

        assert [message.facets for message in wiki.seen] == [{"weight": ["heavy", "light"]}]
        assert kitchen.seen == []

    async def test_an_omitted_collection_filters_the_default_base_by_its_own_keys(self):
        kitchen, wiki = FakeSearch(), FakeSearch()
        toolset = _routed(_kitchen(kitchen), _wiki(wiki))

        await _call(toolset, "search", {"query": "brine", "facets": {"shade": ["teal"], "texture": ["matte"]}})

        assert [message.facets for message in kitchen.seen] == [{"shade": ["teal"], "texture": ["matte"]}]

    async def test_a_search_without_facets_sends_an_empty_filter(self):
        kitchen = FakeSearch()
        toolset = _routed(_kitchen(kitchen))

        await _call(toolset, "search", {"query": "brine"})

        assert [message.facets for message in kitchen.seen] == [{}]

    async def test_a_key_the_named_base_does_not_declare_is_refused_naming_that_bases_keys(self):
        kitchen, wiki = FakeSearch(), FakeSearch()
        toolset = _routed(_kitchen(kitchen), _wiki(wiki))

        with pytest.raises(ModelRetry) as refused:
            await _call(toolset, "search", {"query": "brine", "collection": "wiki", "facets": {"shade": ["teal"]}})

        assert str(refused.value) == (
            "Not a facet key the 'wiki' knowledge base declares: 'shade'. The declared keys are `weight`. "
            "Search again with those keys, or without `facets`."
        )
        assert wiki.seen == kitchen.seen == []

    async def test_a_base_that_declares_no_facets_refuses_every_key(self):
        kitchen = FakeSearch()
        toolset = _routed(_kitchen(kitchen, keys=()))

        with pytest.raises(ModelRetry) as refused:
            await _call(toolset, "search", {"query": "brine", "facets": {"shade": ["teal"]}})

        assert str(refused.value) == (
            "The 'kitchen' knowledge base declares no facets, so `facets` takes no key (given: 'shade'). Search again without `facets`."
        )
        assert kitchen.seen == []

    async def test_a_search_without_facets_that_finds_nothing_names_only_the_query(self):
        toolset = _routed(_kitchen(FakeSearch(), keys=()), _wiki(FakeSearch()))

        answer = await _call(toolset, "search", {"query": "brine"})

        assert isinstance(answer, SearchResult)
        assert answer.next_steps.options[0] == "Try broader or different terms."

    async def test_a_faceted_search_that_finds_nothing_names_the_facets_as_a_lever(self):
        toolset = _routed(_kitchen(FakeSearch()))

        answer = await _call(toolset, "search", {"query": "brine", "facets": {"shade": ["teal"]}})

        assert isinstance(answer, SearchResult)
        assert "loosen or drop `facets`" in answer.next_steps.options[0]

    def test_the_description_names_each_bases_declared_keys(self):
        toolset = _routed(_kitchen(FakeSearch()), _wiki(FakeSearch(), keys=()))

        description = toolset.tools["search"].description or ""
        assert "The facet keys each knowledge base declares: 'kitchen' (`shade`, `texture`), 'wiki' (none)." in description
        assert "A call may use only the keys of the knowledge base it searches." in description
        assert "every knowledge base it searches" not in description
        assert "exactly as the knowledge base stores it" in description

    def test_one_base_reads_like_the_single_base_search(self):
        toolset = _routed(_kitchen(FakeSearch()))

        assert "The facet keys this knowledge base declares: `shade`, `texture`." in (toolset.tools["search"].description or "")

    def test_with_no_base_declaring_facets_the_description_says_to_leave_facets_out(self):
        toolset = _routed(_kitchen(FakeSearch(), keys=()), _wiki(FakeSearch(), keys=()))

        assert "No knowledge base here declares facets, so leave `facets` out." in (toolset.tools["search"].description or "")


class TestRoutedMcpServer:
    async def test_the_served_routed_search_takes_facets_and_refuses_an_undeclared_key_as_a_tool_error(self):
        kitchen, wiki = FakeSearch(), FakeSearch()
        server = FastMCP(name="routed")
        register_routed_mcp_tools(server, (_kitchen(kitchen), _wiki(wiki)), default_knowledge_base="kitchen")

        async with connect_in_memory(server) as client:
            await client.initialize()
            listing = await client.list_tools()
            answered = await client.call_tool("search", {"query": "brine", "collection": "wiki", "facets": {"weight": ["heavy"]}})
            refused = await client.call_tool("search", {"query": "brine", "facets": {"weight": ["heavy"]}})

        [tool] = [published for published in listing.tools if published.name == "search"]
        assert "facets" in tool.inputSchema["properties"]
        assert "'kitchen' (`shade`, `texture`), 'wiki' (`weight`)" in (tool.description or "")
        assert answered.isError is False
        assert refused.isError is True
        assert "The declared keys are `shade`, `texture`." in _text(refused)
        assert [message.facets for message in wiki.seen] == [{"weight": ["heavy"]}]
        assert kitchen.seen == []


class TestRoutedSandbox:
    async def test_the_sandboxed_routed_search_hands_the_facets_to_the_base_the_program_names(self):
        repo = FakeSqlGateway()
        other = FakeSqlGateway(result=SqlResult(rows=(), columns=()))
        server = create_mcp_server(
            repo,
            _describe_handler(_schema()),
            search_handler=SearchHandler(repo, FakeEmbedding()),
            fetch_handler=FetchHandler(repo, "repo-docs"),
            graph_expand_handler=GraphExpandHandler(_no_reach(repo), "repo-docs"),
            find_paths_handler=GraphPathsHandler(repo, "repo-docs"),
            envelope_handler=EnvelopeHandler(repo, collection="repo-docs"),
            read_surfaces=(
                replace(_routed_surface("repo-docs", repo, stamp=_fresh("repo-docs")), facet_keys=_KITCHEN_KEYS),
                replace(_routed_surface("other", other, stamp=_fresh("other")), facet_keys=_WIKI_KEYS),
            ),
            only=frozenset({"search", "fetch", "graph_expand", "find_paths", "run_code"}),
            collection="repo-docs",
        )
        code = "found = await search(query='brine', collection='other', facets={'weight': ['heavy']})\nfound['status']\n"

        async with connect_in_memory(server) as client:
            await client.initialize()
            result = await client.call_tool("run_code", {"code": code})

        assert result.isError is False, _text(result)
        assert json.loads(str(other.rows_calls[-1].params["facets"])) == {"weight": ["heavy"]}
        assert repo.rows_calls == []


class TestCapability:
    @pytest.mark.parametrize("k_source", [None, lambda: 4])
    async def test_one_base_hands_the_facets_to_its_handler(self, k_source: Callable[[], int] | None):
        kitchen = FakeSearch()
        knowledge_base = RetrievalKnowledgeBase[None](surfaces=(_kitchen(kitchen),), k_source=k_source)

        await knowledge_base.direct_call_tool("search", {"query": "brine", "facets": {"shade": ["teal"]}})

        assert [message.facets for message in kitchen.seen] == [{"shade": ["teal"]}]

    async def test_one_base_refuses_a_key_it_does_not_declare_naming_its_keys(self):
        kitchen = FakeSearch()
        knowledge_base = RetrievalKnowledgeBase[None](surfaces=(_kitchen(kitchen),))

        with pytest.raises(
            ModelRetry,
            match=r"Not a facet key the 'kitchen' knowledge base declares: 'weight'\. The declared keys are `shade`, `texture`\.",
        ):
            await knowledge_base.direct_call_tool("search", {"query": "brine", "facets": {"weight": ["heavy"]}})

        assert kitchen.seen == []

    @pytest.mark.parametrize("k_source", [None, lambda: 4])
    async def test_the_named_base_alone_gets_the_facets(self, k_source: Callable[[], int] | None):
        kitchen, wiki = FakeSearch(), FakeSearch()
        knowledge_base = RetrievalKnowledgeBase[None](surfaces=(_kitchen(kitchen), _wiki(wiki)), k_source=k_source)

        await knowledge_base.direct_call_tool("search", {"query": "brine", "knowledge_base": "wiki", "facets": {"weight": ["heavy"]}})

        assert [message.facets for message in wiki.seen] == [{"weight": ["heavy"]}]
        assert kitchen.seen == []

    async def test_a_named_base_refuses_a_key_only_another_base_declares(self):
        kitchen, wiki = FakeSearch(), FakeSearch()
        knowledge_base = RetrievalKnowledgeBase[None](surfaces=(_kitchen(kitchen), _wiki(wiki)))

        with pytest.raises(
            ModelRetry, match=r"Not a facet key the 'wiki' knowledge base declares: 'shade'\. The declared keys are `weight`\."
        ):
            await knowledge_base.direct_call_tool("search", {"query": "brine", "knowledge_base": "wiki", "facets": {"shade": ["teal"]}})

        assert wiki.seen == kitchen.seen == []

    async def test_several_searched_bases_take_a_key_every_one_declares(self):
        kitchen, wiki = FakeSearch(rows=(_hit("k1", 0.9),)), FakeSearch(rows=(_hit("w1", 0.8),))
        knowledge_base = RetrievalKnowledgeBase[None](surfaces=(_kitchen(kitchen), _wiki(wiki, keys=("shade",))), cross_search=True)

        await knowledge_base.direct_call_tool("search", {"query": "brine", "knowledge_base": "all", "facets": {"shade": ["teal"]}})

        assert [message.facets for message in kitchen.seen] == [{"shade": ["teal"]}]
        assert [message.facets for message in wiki.seen] == [{"shade": ["teal"]}]

    async def test_several_searched_bases_refuse_a_key_one_does_not_declare_naming_each_bases_keys(self):
        kitchen, wiki = FakeSearch(), FakeSearch()
        knowledge_base = RetrievalKnowledgeBase[None](surfaces=(_kitchen(kitchen), _wiki(wiki)), cross_search=True)

        with pytest.raises(ModelRetry) as refused:
            await knowledge_base.direct_call_tool("search", {"query": "brine", "facets": {"shade": ["teal"]}})

        assert str(refused.value) == (
            "Not a facet key every searched knowledge base declares: 'shade'. "
            "The keys each one declares: 'kitchen' (`shade`, `texture`), 'wiki' (`weight`). "
            "Name one knowledge base to filter by its own keys, or search again without `facets`."
        )
        assert kitchen.seen == wiki.seen == []

    async def test_a_faceted_search_that_finds_nothing_names_the_facets_as_a_lever(self):
        knowledge_base = RetrievalKnowledgeBase[None](surfaces=(_kitchen(FakeSearch()),))

        answer = await knowledge_base.direct_call_tool("search", {"query": "brine", "facets": {"shade": ["teal"]}})

        assert isinstance(answer, SearchResult)
        assert "loosen or drop `facets`" in answer.next_steps.options[0]

    async def test_the_single_base_description_names_its_keys(self):
        knowledge_base = RetrievalKnowledgeBase[None](surfaces=(_kitchen(FakeSearch()),))

        [tool] = [published for published in await knowledge_base.list_tools() if published.name == "search"]

        assert "The facet keys this knowledge base declares: `shade`, `texture`." in (tool.description or "")

    @pytest.mark.parametrize("cross_search", [False, True])
    async def test_the_several_base_description_names_each_bases_keys_and_every_signature_takes_facets(self, cross_search: bool):
        knowledge_base = RetrievalKnowledgeBase[None](surfaces=(_kitchen(FakeSearch()), _wiki(FakeSearch())), cross_search=cross_search)

        [tool] = [published for published in await knowledge_base.list_tools() if published.name == "search"]

        description = tool.description or ""
        assert "'kitchen' (`shade`, `texture`), 'wiki' (`weight`)" in description
        assert ("every knowledge base it searches declares it" in description) is cross_search
        assert ("A call may use only the keys of the knowledge base it searches." in description) is not cross_search
        assert "facets" in tool.inputSchema["properties"]
