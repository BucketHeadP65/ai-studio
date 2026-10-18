"""Every `search` surface mentions `collection` in its description exactly when the tool takes that argument.

A tool description is what the model believes about the tool, so a sentence naming an argument the
tool does not take is false. Each surface here is built the way a host builds it, and its published
description is read beside its published parameter schema.
"""

from __future__ import annotations

from dataclasses import dataclass

import pytest

from mcp.server.fastmcp import FastMCP
from mcp.shared.memory import create_connected_server_and_client_session as connect_in_memory
from pydantic_ai import FunctionToolset
from pydantic_ai.models.test import TestModel
from pydantic_ai.tools import RunContext
from pydantic_ai.usage import RunUsage

from engram.retrieval.capability import ReadSurface, RetrievalKnowledgeBase
from engram.retrieval.dials import ReadDials
from engram.retrieval.domain import Search, SqlResult
from engram.retrieval.envelope import EnvelopeHandler
from engram.retrieval.queries import FetchHandler, GraphExpandHandler, GraphPathsHandler
from engram.retrieval.tools import (
    RetrievalHandlers,
    add_routed_read_tools,
    build_retrieval_toolset,
    register_mcp_tools,
    register_routed_mcp_tools,
)
from engram.testing.fakes.gateway import FakeSqlGateway

_HOME = "field-notes"
_OTHER = "bench-log"
_COLLECTION_ARGUMENT = "collection"


@dataclass
class FakeNoHitSearch:
    """A search handler that ranks nothing."""

    async def __call__(self, query: Search) -> SqlResult:
        del query
        return SqlResult()


def _handlers(collection: str) -> RetrievalHandlers:
    gateway = FakeSqlGateway()
    return RetrievalHandlers(
        search=FakeNoHitSearch(),
        fetch=FetchHandler(gateway, collection),
        graph_expand=GraphExpandHandler(gateway, collection),
        find_paths=GraphPathsHandler(gateway, collection),
    )


def _surface(name: str) -> ReadSurface:
    return ReadSurface(
        name=name,
        collection=name,
        handlers=_handlers(name),
        envelope=EnvelopeHandler(FakeSqlGateway(), collection=name),
        domain=f"The {name} knowledge base.",
    )


def _ctx() -> RunContext[None]:
    return RunContext(deps=None, model=TestModel(), usage=RunUsage())


async def _published(toolset: FunctionToolset[object] | RetrievalKnowledgeBase[None]) -> tuple[str, frozenset[str]]:
    """The `search` description and parameter names a toolset or capability publishes for this request."""
    source = toolset.get_toolset() if isinstance(toolset, RetrievalKnowledgeBase) else toolset
    tools = await source.get_tools(_ctx())
    definition = tools["search"].tool_def
    return definition.description or "", frozenset(definition.parameters_json_schema["properties"])


async def _published_mcp(server: FastMCP) -> tuple[str, frozenset[str]]:
    """The `search` description and parameter names an MCP server lists."""
    async with connect_in_memory(server) as client:
        await client.initialize()
        listing = await client.list_tools()
    search = next(tool for tool in listing.tools if tool.name == "search")
    return search.description or "", frozenset(search.inputSchema["properties"])


def _routed_toolset() -> FunctionToolset[object]:
    toolset = FunctionToolset[object]()
    add_routed_read_tools(toolset, (_surface(_HOME), _surface(_OTHER)), default_knowledge_base=_HOME)
    return toolset


_TOOLSETS = {
    "single-source": lambda: build_retrieval_toolset(_handlers(_HOME), collection=_HOME),
    "single-source-capped": lambda: build_retrieval_toolset(_handlers(_HOME), collection=_HOME, k_source=lambda: 5),
    "single-source-keyword": lambda: build_retrieval_toolset(_handlers(_HOME), collection=_HOME, search_hybrid=False, can_sql=True),
    "single-source-pinned": lambda: build_retrieval_toolset(_handlers(_HOME), collection=_HOME, pinned=True),
    "routed": _routed_toolset,
    "one-base": lambda: RetrievalKnowledgeBase[None](surfaces=(_surface(_HOME),)),
    "one-base-capped": lambda: RetrievalKnowledgeBase[None](surfaces=(_surface(_HOME),), k_source=lambda: 5),
    "one-base-keyword-dial": lambda: RetrievalKnowledgeBase[None](surfaces=(_surface(_HOME),), dials_source=lambda: ReadDials(search="keyword")),
    "several-bases": lambda: RetrievalKnowledgeBase[None](surfaces=(_surface(_HOME), _surface(_OTHER))),
    "several-bases-cross-search": lambda: RetrievalKnowledgeBase[None](surfaces=(_surface(_HOME), _surface(_OTHER)), cross_search=True),
}
"""Every in-process `search` surface, by the name a failure reports."""


@pytest.mark.parametrize("surface", list(_TOOLSETS), ids=list(_TOOLSETS))
async def test_an_in_process_search_names_collection_only_when_it_takes_it(surface: str):
    description, parameters = await _published(_TOOLSETS[surface]())

    assert ("`collection`" in description) == (_COLLECTION_ARGUMENT in parameters), description


async def test_the_single_source_mcp_search_names_collection_only_when_it_takes_it():
    server = FastMCP("search-surface")
    register_mcp_tools(server, _handlers(_HOME), collection=_HOME)

    description, parameters = await _published_mcp(server)

    assert ("`collection`" in description) == (_COLLECTION_ARGUMENT in parameters), description


async def test_the_routed_mcp_search_names_collection_only_when_it_takes_it():
    server = FastMCP("search-surface")
    register_routed_mcp_tools(server, (_surface(_HOME), _surface(_OTHER)), default_knowledge_base=_HOME)

    description, parameters = await _published_mcp(server)

    assert ("`collection`" in description) == (_COLLECTION_ARGUMENT in parameters), description
