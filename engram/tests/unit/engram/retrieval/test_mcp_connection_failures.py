"""A tool whose connection to the database or an endpoint fails answers its MCP client with the service it reached.

mcp 2 keeps the text of any exception other than `ToolError` on the server, so the server built by
`create_mcp_server` raises a failed connection again as a `ToolError` naming the service. One test per server
variant (flat, leveled, routed, the eval authoring tools) drives a tool over a gateway whose connection fails and
reads the answer through `mcp.client.Client`. A crash that is not a connection failure keeps its text on the server.
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import TYPE_CHECKING

import httpx2

from mcp.client import Client
from openai import APIConnectionError
from sqlalchemy.exc import DBAPIError

from engram.retrieval.capability import ReadSurface
from engram.retrieval.domain import ColumnInfo, ObjectInfo, SchemaDescription, SqlResult
from engram.retrieval.envelope import EnvelopeHandler
from engram.retrieval.infrastructure.embedded import slug_only_stamp
from engram.retrieval.infrastructure.mcp_server import PROPOSE_SEEDS, create_mcp_server
from engram.retrieval.queries import DescribeSchemaHandler, FetchHandler, GraphExpandHandler, GraphPathsHandler, SearchHandler
from engram.retrieval.tools import RetrievalHandlers
from engram.testing.fakes.embedding import FakeEmbedding
from engram.testing.fakes.gateway import FakeSqlGateway

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator, Mapping, Sequence
    from pathlib import Path

    from core.ai.embedding import Embeddings
    from mcp.server.mcpserver import MCPServer
    from mcp.types import CallToolResult

    from engram.evals.generation.contracts import SupportsSeeding

_DATABASE = "postgresql+asyncpg://kb:***@db.example:5432/kb"
_COLLECTION = "research"
_REFUSED = "[Errno 61] Connect call failed ('10.0.0.5', 5432)"
_EMBEDDINGS_URL = "https://example.openai.azure.com/openai/deployments/embed/embeddings"


@dataclass
class _UnreachableGateway(FakeSqlGateway):
    """A gateway whose every read raises `failure`, as a database that refuses the connection does."""

    failure: Exception | None = None

    async def rows(self, sql: str, params: Mapping[str, object] | None = None, *, cap: int = 50) -> SqlResult:
        del sql, params, cap
        raise self.failure or ConnectionRefusedError(61, "Connect call failed ('10.0.0.5', 5432)")


class _UnreachableEmbedder(FakeEmbedding):
    """An embedder whose endpoint refuses the connection."""

    async def embed_batch(self, texts: Sequence[str]) -> Embeddings:
        del texts
        raise APIConnectionError(request=httpx2.Request("POST", _EMBEDDINGS_URL))


def _schema() -> DescribeSchemaHandler:
    objects = (ObjectInfo(name="api.cards", kind="view", comment="Cards.", columns=(ColumnInfo(name="id", data_type="text", comment="Id."),)),)
    return DescribeSchemaHandler(FakeSqlGateway(schema=SchemaDescription(objects=objects)))


def _flat_server(gateway: FakeSqlGateway, embedder: FakeEmbedding | None = None, *, database: str | None = _DATABASE) -> MCPServer:
    return create_mcp_server(
        gateway,
        _schema(),
        search_handler=SearchHandler(gateway, embedder or FakeEmbedding()),
        fetch_handler=FetchHandler(gateway, _COLLECTION),
        graph_expand_handler=GraphExpandHandler(gateway, _COLLECTION),
        find_paths_handler=GraphPathsHandler(gateway, _COLLECTION),
        collection=_COLLECTION,
        database=database,
    )


def _text(result: CallToolResult) -> str:
    block = result.content[0]
    assert block.type == "text"
    return block.text


async def _call(server: MCPServer, tool: str, arguments: dict[str, object]) -> CallToolResult:
    async with Client(server) as client:
        return await client.call_tool(tool, arguments)


class TestTheFlatServer:
    async def test_a_search_whose_database_refuses_the_connection_names_the_database(self):
        result = await _call(_flat_server(_UnreachableGateway()), "search", {"query": "graph evidence", "bm25_only": True})

        assert result.is_error
        assert _text(result) == f"Error executing tool search: database {_DATABASE}: {_REFUSED}"

    async def test_a_search_whose_embedding_endpoint_is_unreachable_names_the_endpoint(self):
        result = await _call(_flat_server(FakeSqlGateway(), _UnreachableEmbedder()), "search", {"query": "graph evidence"})

        assert result.is_error
        assert _text(result) == "Error executing tool search: embedding endpoint example.openai.azure.com: Connection error."

    async def test_a_statement_whose_connection_drops_names_the_database_not_the_read_only_hint(self):
        reset = ConnectionResetError(54, "Connection reset by peer")
        dropped = DBAPIError("SELECT 1", None, reset)
        dropped.__cause__ = reset  # SQLAlchemy raises the driver's error from the one it wraps.

        result = await _call(_flat_server(_UnreachableGateway(failure=dropped)), "run_sql", {"sql": "SELECT 1"})

        assert result.is_error
        assert _text(result).startswith(f"Error executing tool run_sql: database {_DATABASE}: ")
        assert "Connection reset by peer" in _text(result)
        assert "read-only" not in _text(result)

    async def test_a_crash_that_is_no_connection_failure_keeps_its_text_on_the_server(self):
        result = await _call(_flat_server(_UnreachableGateway(failure=RuntimeError("secret detail"))), "search", {"query": "q", "bm25_only": True})

        assert result.is_error
        assert _text(result) == "Error executing tool search"

    async def test_with_no_database_named_a_database_failure_stays_a_bare_error(self):
        result = await _call(_flat_server(_UnreachableGateway(), database=None), "search", {"query": "q", "bm25_only": True})

        assert result.is_error
        assert _text(result) == "Error executing tool search"


class TestTheLeveledServer:
    async def test_a_leveled_fetch_whose_database_refuses_the_connection_names_the_database(self):
        gateway = _UnreachableGateway()
        server = create_mcp_server(
            gateway,
            _schema(),
            search_handler=SearchHandler(gateway, FakeEmbedding()),
            fetch_handler=FetchHandler(gateway, _COLLECTION),
            graph_expand_handler=GraphExpandHandler(gateway, _COLLECTION),
            find_paths_handler=GraphPathsHandler(gateway, _COLLECTION),
            envelope_handler=EnvelopeHandler(gateway, collection=_COLLECTION),
            k_source=lambda: 5,
            search_hybrid=False,
            collection=_COLLECTION,
            database=_DATABASE,
        )

        result = await _call(server, "fetch", {"unit_id": f"collection:{_COLLECTION}"})

        assert result.is_error
        assert _text(result) == f"Error executing tool fetch: database {_DATABASE}: {_REFUSED}"


class TestTheRoutedServer:
    async def test_a_routed_search_whose_database_refuses_the_connection_names_the_database(self):
        gateway = _UnreachableGateway()
        surface = ReadSurface(
            name=_COLLECTION,
            collection=_COLLECTION,
            domain="The research knowledge base.",
            hybrid=False,
            stamp=slug_only_stamp(_COLLECTION),
            handlers=RetrievalHandlers(
                search=SearchHandler(gateway, FakeEmbedding()),
                fetch=FetchHandler(gateway, _COLLECTION),
                graph_expand=GraphExpandHandler(gateway, _COLLECTION),
                find_paths=GraphPathsHandler(gateway, _COLLECTION),
            ),
            envelope=EnvelopeHandler(gateway, collection=_COLLECTION, group_by="app"),
        )
        server = create_mcp_server(gateway, _schema(), read_surfaces=(surface,), collection=_COLLECTION, database=_DATABASE)

        result = await _call(server, "search", {"query": "graph evidence", "collection": _COLLECTION})

        assert result.is_error
        assert _text(result) == f"Error executing tool search: database {_DATABASE}: {_REFUSED}"


class TestTheEvalAuthoringServer:
    async def test_proposing_seeds_over_a_refused_connection_names_the_database(self, tmp_path: Path):
        @asynccontextmanager
        async def unreachable_repository() -> AsyncGenerator[SupportsSeeding]:
            raise ConnectionRefusedError(61, "Connect call failed ('10.0.0.5', 5432)")
            yield

        server = create_mcp_server(
            FakeSqlGateway(),
            _schema(),
            seed_repo_factory=unreachable_repository,
            datasets_dir=tmp_path,
            only=frozenset({PROPOSE_SEEDS}),
            collection=_COLLECTION,
            database=_DATABASE,
        )

        result = await _call(server, PROPOSE_SEEDS, {"archetype": "pinpoint", "count": 3})

        assert result.is_error
        assert _text(result) == f"Error executing tool {PROPOSE_SEEDS}: database {_DATABASE}: {_REFUSED}"
