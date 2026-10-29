"""A `search` whose query cannot be embedded runs keyword only and says why, the way `run_sql` does.

The handler stays strict unless the search asks for the fallback, so a caller that scores the hybrid leg still sees
the failure. Every served `search` tool asks for it: the toolset's, the MCP server's, the routed one and the read
capability's. Each answers the keyword hits with `run_sql`'s own wording. A toolset built for an evaluation does not.
"""

from __future__ import annotations

import asyncio
import json

from typing import TYPE_CHECKING

import httpx2
import pytest

from mcp.client import Client
from openai import APIConnectionError
from pydantic_ai.models.test import TestModel
from pydantic_ai.tools import RunContext
from pydantic_ai.usage import RunUsage

from engram.retrieval.capability import ReadSurface, RetrievalKnowledgeBase
from engram.retrieval.domain import KeywordOnlyResult, Search, SearchResult, SqlResult
from engram.retrieval.envelope import EnvelopeHandler
from engram.retrieval.infrastructure.embedded import slug_only_stamp
from engram.retrieval.infrastructure.mcp_server import create_mcp_server
from engram.retrieval.queries import DescribeSchemaHandler, FetchHandler, GraphExpandHandler, GraphPathsHandler, SearchHandler
from engram.retrieval.tools import KEYWORD_FALLBACK_SENTENCE, RetrievalHandlers, build_retrieval_toolset
from engram.testing.fakes.embedding import FakeEmbedding
from engram.testing.fakes.gateway import FakeSqlGateway

if TYPE_CHECKING:
    from collections.abc import Sequence

    from core.ai.embedding import Embeddings
    from mcp.types import CallToolResult

_COLLECTION = "research"
_EMBEDDINGS_URL = "https://example.openai.azure.com/openai/deployments/embed/embeddings"
_REFUSED = "(embedding unavailable, ran BM25-only: Connection error.)"
_ROWS = SqlResult(
    rows=({"id": "paper.graph", "hit_level": "card", "page_title": "Graph", "snippet": "graph evidence", "score": 0.5, "lexical_rank": 1},),
    columns=("id", "hit_level", "page_title", "snippet", "score", "lexical_rank"),
)
_TRUNCATED = _ROWS.model_copy(update={"truncated_message": "… 3 more rows"})


class _RefusingEmbedder(FakeEmbedding):
    """An embedder whose endpoint refuses the connection."""

    async def embed_batch(self, texts: Sequence[str]) -> Embeddings:
        del texts
        raise APIConnectionError(request=httpx2.Request("POST", _EMBEDDINGS_URL))


class _SilentEmbedder(FakeEmbedding):
    """An embedder whose endpoint never answers."""

    async def embed_batch(self, texts: Sequence[str]) -> Embeddings:
        del texts
        await asyncio.Event().wait()
        msg = "unreachable: the event is never set"
        raise AssertionError(msg)


def _handlers(gateway: FakeSqlGateway, embedder: FakeEmbedding) -> RetrievalHandlers[Search]:
    return RetrievalHandlers(
        search=SearchHandler(gateway, embedder),
        fetch=FetchHandler(gateway, _COLLECTION),
        graph_expand=GraphExpandHandler(gateway, _COLLECTION),
        find_paths=GraphPathsHandler(gateway, _COLLECTION),
    )


def _ctx() -> RunContext[None]:
    return RunContext(deps=None, model=TestModel(), usage=RunUsage())


def _search_params(gateway: FakeSqlGateway) -> dict[str, object]:
    (call,) = [call for call in gateway.rows_calls if "api.search(" in call.sql]
    return dict(call.params)


def _text(result: CallToolResult) -> str:
    block = result.content[0]
    assert block.type == "text"
    return block.text


class TestTheHandler:
    async def test_an_embedding_failure_still_raises_unless_the_search_asks_to_fall_back(self):
        handler = SearchHandler(FakeSqlGateway(result=_ROWS), _RefusingEmbedder())

        with pytest.raises(APIConnectionError):
            await handler(Search(query="graph evidence", collection=_COLLECTION))

    async def test_a_refused_embedding_runs_keyword_only_and_names_the_error(self):
        gateway = FakeSqlGateway(result=_TRUNCATED)
        handler = SearchHandler(gateway, _RefusingEmbedder())

        result = await handler(Search(query="graph evidence", collection=_COLLECTION, keyword_fallback=True))

        assert isinstance(result, KeywordOnlyResult)
        assert result.reason == "Connection error."
        assert result.model_dump(exclude={"reason"}) == _TRUNCATED.model_dump()
        assert _search_params(gateway)["qvec"] is None
        assert _search_params(gateway)["query_model"] is None

    async def test_an_embedding_past_the_deadline_runs_keyword_only_and_names_the_deadline(self):
        gateway = FakeSqlGateway(result=_ROWS)
        handler = SearchHandler(gateway, _SilentEmbedder(), embed_deadline=0.01)

        result = await handler(Search(query="graph evidence", collection=_COLLECTION, keyword_fallback=True))

        assert isinstance(result, KeywordOnlyResult)
        assert result.reason == "embedding took longer than 0.01 seconds"
        assert _search_params(gateway)["qvec"] is None

    async def test_an_embedded_query_answers_a_plain_result(self):
        gateway = FakeSqlGateway(result=_ROWS)
        handler = SearchHandler(gateway, FakeEmbedding())

        result = await handler(Search(query="graph evidence", collection=_COLLECTION, keyword_fallback=True))

        assert not isinstance(result, KeywordOnlyResult)
        assert _search_params(gateway)["qvec"] is not None


class TestTheSearchTools:
    async def test_the_toolset_search_answers_the_keyword_hits_with_run_sqls_wording(self):
        toolset = build_retrieval_toolset(_handlers(FakeSqlGateway(result=_ROWS), _RefusingEmbedder()), search_hybrid=True, collection=_COLLECTION)
        ctx = _ctx()
        tools = await toolset.get_tools(ctx)

        result = await toolset.call_tool("search", {"query": "graph evidence"}, ctx, tools["search"])

        assert isinstance(result, SearchResult)
        assert result.keyword_only_reason == _REFUSED
        assert [hit.answered_by for hit in result.hits] == ["lexical"]
        assert result.next_steps.summary.endswith("Answered by lexical.")

    async def test_a_toolset_built_without_the_fallback_raises_the_embedding_error_and_promises_nothing(self):
        gateway = FakeSqlGateway(result=_ROWS)
        toolset = build_retrieval_toolset(_handlers(gateway, _RefusingEmbedder()), search_hybrid=True, collection=_COLLECTION, keyword_fallback=False)
        ctx = _ctx()
        tools = await toolset.get_tools(ctx)

        with pytest.raises(APIConnectionError):
            await toolset.call_tool("search", {"query": "graph evidence"}, ctx, tools["search"])

        assert KEYWORD_FALLBACK_SENTENCE not in (tools["search"].tool_def.description or "")
        assert not [call for call in gateway.rows_calls if "api.search(" in call.sql]

    async def test_a_served_toolset_promises_the_fallback(self):
        toolset = build_retrieval_toolset(_handlers(FakeSqlGateway(), FakeEmbedding()), search_hybrid=True, collection=_COLLECTION)

        tools = await toolset.get_tools(_ctx())

        assert KEYWORD_FALLBACK_SENTENCE in (tools["search"].tool_def.description or "")

    async def test_a_search_that_ran_as_asked_carries_no_reason(self):
        toolset = build_retrieval_toolset(_handlers(FakeSqlGateway(result=_ROWS), FakeEmbedding()), search_hybrid=True, collection=_COLLECTION)
        ctx = _ctx()
        tools = await toolset.get_tools(ctx)

        result = await toolset.call_tool("search", {"query": "graph evidence"}, ctx, tools["search"])

        assert isinstance(result, SearchResult)
        assert result.keyword_only_reason is None

    async def test_the_mcp_search_answers_the_keyword_hits_with_the_reason(self):
        gateway = FakeSqlGateway(result=_ROWS)
        server = create_mcp_server(
            gateway,
            DescribeSchemaHandler(gateway),
            search_handler=SearchHandler(gateway, _RefusingEmbedder()),
            fetch_handler=FetchHandler(gateway, _COLLECTION),
            graph_expand_handler=GraphExpandHandler(gateway, _COLLECTION),
            find_paths_handler=GraphPathsHandler(gateway, _COLLECTION),
            collection=_COLLECTION,
        )

        async with Client(server) as client:
            result = await client.call_tool("search", {"query": "graph evidence"})

        assert not result.is_error
        answer = json.loads(_text(result))
        assert answer["keyword_only_reason"] == _REFUSED
        assert [hit["id"] for hit in answer["hits"]] == ["paper.graph"]

    async def test_the_routed_mcp_search_answers_the_keyword_hits_with_the_reason(self):
        gateway = FakeSqlGateway(result=_ROWS)
        surface = ReadSurface(
            name=_COLLECTION,
            collection=_COLLECTION,
            domain="The research knowledge base.",
            stamp=slug_only_stamp(_COLLECTION),
            handlers=_handlers(gateway, _RefusingEmbedder()),
            envelope=EnvelopeHandler(gateway, collection=_COLLECTION),
        )
        server = create_mcp_server(gateway, DescribeSchemaHandler(gateway), read_surfaces=(surface,), collection=_COLLECTION)

        async with Client(server) as client:
            result = await client.call_tool("search", {"query": "graph evidence"})

        assert not result.is_error
        assert json.loads(_text(result))["keyword_only_reason"] == _REFUSED

    async def test_the_read_capability_search_answers_the_keyword_hits_with_the_reason(self):
        gateway = FakeSqlGateway(result=_ROWS)
        surface = ReadSurface(
            name=_COLLECTION,
            collection=_COLLECTION,
            domain="The research knowledge base.",
            handlers=_handlers(gateway, _RefusingEmbedder()),
            envelope=EnvelopeHandler(gateway, collection=_COLLECTION),
        )
        knowledge_base = RetrievalKnowledgeBase[None](surfaces=(surface,))

        result = await knowledge_base.direct_call_tool("search", {"query": "graph evidence"})

        assert isinstance(result, SearchResult)
        assert result.keyword_only_reason == _REFUSED
        assert result.next_steps.summary.endswith("Answered by lexical.")

    async def test_a_search_over_several_bases_names_the_ones_that_ran_keyword_only(self):
        refused = ReadSurface(
            name="research",
            collection="research",
            domain="The research knowledge base.",
            handlers=_handlers(FakeSqlGateway(result=_ROWS), _RefusingEmbedder()),
            envelope=EnvelopeHandler(FakeSqlGateway(), collection="research"),
        )
        embedded = ReadSurface(
            name="notes",
            collection="notes",
            domain="The notes knowledge base.",
            handlers=_handlers(FakeSqlGateway(result=_ROWS), FakeEmbedding()),
            envelope=EnvelopeHandler(FakeSqlGateway(), collection="notes"),
        )
        knowledge_base = RetrievalKnowledgeBase[None](surfaces=(refused, embedded), cross_search=True)

        result = await knowledge_base.direct_call_tool("search", {"query": "graph evidence"})

        assert isinstance(result, SearchResult)
        assert result.keyword_only_reason == "(embedding unavailable, ran BM25-only in research: Connection error.)"
        assert {hit.knowledge_base: hit.answered_by for hit in result.hits} == {"research": "lexical", "notes": "lexical"}
