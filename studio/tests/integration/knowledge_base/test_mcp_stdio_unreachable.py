"""`studio mcp` run as its own process against a database it cannot reach, driven over stdio by an MCP client.

The server must announce the unreachable database on stderr, complete the MCP handshake and list its tools, and answer
each tool call with an error rather than exit, so a client that started before the database still has a server once the
database comes up. It cannot check its role at launch, so it builds no tool that writes, even when the schema opts in.
The addresses are real: one with nothing listening (`127.0.0.1:1`), and a throwaway Postgres refusing a wrong password
or a database name it does not hold, never a fake container. A server launched with the reader role while nothing
listens answers `search` and `fetch` with no restart once a forwarder starts listening there for the throwaway Postgres.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import socket
import sys

from pathlib import Path
from typing import TYPE_CHECKING

import pytest

from core.ai.retrieval.contracts import KnowledgeBaseStamp
from engram.indexing.contracts import (
    FilesystemKnowledgeBaseReader,
    IndexKnowledgeBase,
    IndexKnowledgeBaseHandler,
    SqlAlchemyKnowledgeBaseUnitOfWork,
)
from engram.indexing.infrastructure import orm
from engram.kb.contracts import KnowledgeBaseSchema
from engram.retrieval.domain import SearchResult
from engram.retrieval.tools import FetchEnvelope
from engram.testing.fakes.embedding import FakeEmbedding
from engram.testing.fakes.uow import FakeValidationReportSink
from engram.testing.knowledge_base_builder import KnowledgeBaseBuilder
from fastmcp import Client
from fastmcp.client.transports import StdioTransport
from sqlalchemy import NullPool
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import create_async_engine

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator

    from fastmcp.client.client import CallToolResult

_STUDIO = Path(sys.executable).parent / "studio"
_NOBODY = "postgresql+asyncpg://kb_reader:unused@127.0.0.1:1/knowledge_bases"

# The studio reads the settings file at a path fixed to its own code, so a checkout holding one feeds it to the spawned
# server, and an exported value wins over the file key by key. The studio refuses to start without these sections. The
# retrieval section pins the served profile to keyword search and turns re-ranking off, so no search reaches a model.
_STUDIO_SECTIONS = {
    "APP_RETRIEVAL": json.dumps({"retrieval_profile": "search-keyword", "rerank": {"enabled": False}}),
    "APP_INGESTION": "{}",
    "APP_SOURCES": "{}",
    "APP_EVALS": "{}",
}

# The server builds its embedding model as it starts, so it needs an endpoint. Keyword search never calls it.
_UNCALLED_EMBEDDING_ENDPOINT = "https://embeddings.example.invalid"

_INIT_TIMEOUT_SECONDS = 120

_WRITE_TOOLS = frozenset({"knowledge_base_profile", "skeleton", "write_note", "decide_page", "resolve_entities", "read_page"})


def _knowledge_base(root: Path) -> KnowledgeBaseSchema:
    """Lay down one knowledge base of one page, searched by keyword alone, whose schema turns the documentation-authoring tools on."""
    spec = KnowledgeBaseSchema.model_validate(
        {
            "slug": "served",
            "page_types": [{"page_type": "reference", "id_prefix": "paper", "folder": "Units/References"}],
            "retrieval": {"collection": "served", "mode": "fixed", "profile": "search-keyword", "doc_authoring": True},
        }
    )
    builder = KnowledgeBaseBuilder(root)
    builder.add_knowledge_base_schema(spec)
    builder.add_unit_document(
        "Units/References", "graph", "---\nid: paper.graph\ntype: reference\ntitle: Graph\nstatus: active\n---\n\n## Findings\n\nBody.\n"
    )
    return spec


def _env(url: str) -> dict[str, str]:
    """This process's environment with both database URLs at `url` and every required section set."""
    env = {
        **os.environ,
        "APP_INFRASTRUCTURE__DATABASE__URL": url,
        "APP_INFRASTRUCTURE__DATABASE__READER_URL": url,
        "APP_INFRASTRUCTURE__AZURE_OPENAI__ENDPOINT": _UNCALLED_EMBEDDING_ENDPOINT,
        "APP_KNOWLEDGE_BASES": json.dumps({}),
    }
    env.update(_STUDIO_SECTIONS)
    return env


async def _serve(tmp_path: Path, url: str) -> str:
    """Serve a knowledge base against `url`, check the tool list and a failing search, and answer the server's stderr."""
    root = tmp_path / "served"
    _knowledge_base(root)
    log = tmp_path / "server.log"
    transport = StdioTransport(command=str(_STUDIO), args=["mcp", "--knowledge-base", str(root)], env=_env(url), cwd=str(tmp_path), log_file=log)

    async with Client(transport, init_timeout=_INIT_TIMEOUT_SECONDS) as client:
        names = {tool.name for tool in await client.list_tools()}
        searched = await client.call_tool("search", {"query": "graph"}, raise_on_error=False)
        still_listed = {tool.name for tool in await client.list_tools()}

    assert {"search", "fetch"} <= names
    assert not names & _WRITE_TOOLS
    assert searched.is_error
    assert still_listed == names
    return log.read_text(encoding="utf-8")


async def test_the_server_serves_anyway_with_no_write_tool_and_every_call_fails_until_the_database_answers(tmp_path: Path) -> None:
    err = await _serve(tmp_path, _NOBODY)

    assert "db 127.0.0.1:1/knowledge_bases unreachable, connection refused." in err
    assert "serving anyway, every call fails until the database is reachable." in err
    assert "left out the documentation-authoring tools, since the database role could not be checked at launch." in err


@pytest.mark.parametrize(
    ("refused", "reason"), [("password", "authentication failed"), ("database", "database does not exist")], ids=["wrong-password", "missing-database"]
)
async def test_a_refused_login_serves_anyway_too(tmp_path: Path, pg: str, refused: str, reason: str) -> None:
    base = make_url(pg)
    url = (base.set(password="wrong-password") if refused == "password" else base.set(database="no_such_database")).render_as_string(hide_password=False)

    err = await _serve(tmp_path, url)

    assert f"unreachable, {reason}." in err
    assert "serving anyway, every call fails until the database is reachable." in err


async def _index(root: Path, spec: KnowledgeBaseSchema, url: str) -> None:
    """Index the knowledge base through the official handler, as the admin role."""
    engine = create_async_engine(url, poolclass=NullPool)
    await orm.register_pgvector(engine)
    try:
        handler = IndexKnowledgeBaseHandler(
            FilesystemKnowledgeBaseReader(root, note_folders=spec.note_folders),
            FakeEmbedding(),
            lambda: SqlAlchemyKnowledgeBaseUnitOfWork(engine),
            FakeValidationReportSink(),
            spec=spec,
        )
        await handler.handle(IndexKnowledgeBase(collection="served", collection_named=True, embed=False))
    finally:
        await engine.dispose()


def _free_port() -> int:
    """A local port nothing listens on, as the operating system hands one out."""
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


async def _pipe(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
    """Copy one direction of a forwarded connection until it closes."""
    try:
        while chunk := await reader.read(65536):
            writer.write(chunk)
            await writer.drain()
    except ConnectionError:
        pass
    finally:
        writer.close()


@contextlib.asynccontextmanager
async def _forwarding(port: int, *, host: str, to_port: int) -> AsyncGenerator[None]:
    """Listen on `port` and forward every connection to `host:to_port`, as a database coming back up would answer."""

    async def forward(client_reader: asyncio.StreamReader, client_writer: asyncio.StreamWriter) -> None:
        upstream_reader, upstream_writer = await asyncio.open_connection(host, to_port)
        await asyncio.gather(_pipe(client_reader, upstream_writer), _pipe(upstream_reader, client_writer))

    server = await asyncio.start_server(forward, "127.0.0.1", port)
    try:
        yield
    finally:
        server.close()
        server.close_clients()
        await server.wait_closed()


def _answer(result: CallToolResult) -> str:
    """A tool's answer, the text of its single block, refused when the call failed."""
    block = result.content[0]
    assert block.type == "text"
    assert not result.is_error, block.text
    return block.text


@pytest.mark.timeout(300)
async def test_a_reader_server_launched_while_the_database_is_down_answers_once_it_comes_back(
    tmp_path: Path, migrated: str, reader_url: str
) -> None:
    root = tmp_path / "served"
    await _index(root, _knowledge_base(root), migrated)
    database = make_url(reader_url)
    port = _free_port()
    url = database.set(host="127.0.0.1", port=port).render_as_string(hide_password=False)
    log = tmp_path / "server.log"
    transport = StdioTransport(command=str(_STUDIO), args=["mcp", "--knowledge-base", str(root)], env=_env(url), cwd=str(tmp_path), log_file=log)

    async with Client(transport, init_timeout=_INIT_TIMEOUT_SECONDS) as client:
        while_down = await client.call_tool("search", {"query": "graph"}, raise_on_error=False)
        async with _forwarding(port, host=database.host or "127.0.0.1", to_port=database.port or 5432):
            searched = SearchResult.model_validate_json(_answer(await client.call_tool("search", {"query": "graph"}, raise_on_error=False)))
            page_ref = next(hit.ref for hit in searched.hits if hit.hit_level == "card")
            assert page_ref is not None
            fetched = FetchEnvelope.model_validate_json(_answer(await client.call_tool("fetch", {"unit_id": page_ref}, raise_on_error=False)))

    assert while_down.is_error
    assert "serving anyway, every call fails until the database is reachable." in log.read_text(encoding="utf-8")
    assert page_ref == "page:served/paper.graph"
    assert searched.knowledge_base == KnowledgeBaseStamp(slug="served")
    assert fetched.ref == page_ref
    assert "Body." in (fetched.content or "")
