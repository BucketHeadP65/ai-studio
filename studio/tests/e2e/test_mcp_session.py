r"""End-to-end MCP session against a real, indexed ParadeDB — Docker-gated.

The whole read path through the MCP surface: a mini `knowledge_base_builder` knowledge base →
``memory index`` (the public CLI over the **real container graph**, only the
:class:`BatchEmbedder` overridden to :class:`FakeEmbedding` — the established e2e seeding shape) →
a :class:`~mcp.server.fastmcp.FastMCP` built by :func:`create_mcp_server` over the
production read-only :class:`~engram.retrieval.infrastructure.gateway.ReaderSqlGateway` (the
`kb_reader` role; the `reader_url` fixture) → an **in-process** MCP client
session (the SDK's `create_connected_server_and_client_session` memory transport —
no stdio subprocess needed to prove the tools).

It asserts the agent-facing contract:

* `list_tools` returns exactly `describe_schema` + `run_sql` with non-empty
  descriptions that mention `api.search`;
* `describe_schema` renders the M-Schema and names `api.cards` + `api.search`;
* `run_sql('SELECT * FROM api.about')` returns the seeded orientation row;
* `run_sql('INSERT INTO kb.pages …')` returns the actionable read-only error
  (the role rejects the write) instead of crashing the session;
* `row_limit` is honoured against a real multi-row read.

Tests are *synchronous up to the seeding* (`runner.invoke` drives the Typer
`index` command, which calls `asyncio.run` — so the seeding body has no running
loop); the MCP half is an `async def` that drives the in-memory client, with the
reader engine built + disposed inside it. The module skips when Docker is unavailable
(the `pg` fixture, via `migrated`, raises `pytest.skip`).
"""

from __future__ import annotations

import json

from typing import TYPE_CHECKING

import pytest

from core.ai.embedding import BatchEmbedder
from core.wiring.contracts import ContainerResolver
from engram.indexing.infrastructure import orm
from engram.retrieval.infrastructure.gateway import ReadOnlySqlGateway
from engram.retrieval.infrastructure.mcp_server import create_mcp_server
from engram.retrieval.queries import DescribeSchemaHandler
from engram.testing.fakes.embedding import FakeEmbedding
from engram.testing.knowledge_base_builder import KnowledgeBaseBuilder
from mcp.shared.memory import create_connected_server_and_client_session as connect_in_memory
from sqlalchemy import NullPool
from sqlalchemy.ext.asyncio import create_async_engine
from typer.testing import CliRunner

from studio.knowledge_base.cli import app
from studio.knowledge_base.container import create_container
from studio.knowledge_base.settings import ApplicationSettings, DatabaseSettings, KnowledgeBaseSettings

if TYPE_CHECKING:
    from pathlib import Path

    from mcp.server.fastmcp import FastMCP
    from mcp.types import CallToolResult
    from sqlalchemy.ext.asyncio import AsyncEngine

runner = CliRunner()

# One small, well-formed paper + its mirror — enough for a clean index with pages,
# sections, and a claim, so api.about / api.cards / api.search all have rows to serve.
_PAPER = """\
---
id: paper.graph-cost
type: paper
title: Graph Indexing Token Cost
summary: Building a knowledge graph index incurs substantial token cost during construction.
status: synthesized
zotero_key: GRAPHKEY1
year: 2025
paper_type: method
source_hash: g001
tasks: [long-doc-qa]
stages: [indexing]
---

## TL;DR

Graph index construction spends many tokens extracting entities and relations.

## Findings

- Graph indexing incurs substantial token cost during construction. ^c1
    - loc:: §3.1
    - kind:: observation
"""


def _mirror(source_key: str, source_hash: str, body: str) -> str:
    """A minimal raw mirror — its only job here is to satisfy the pages.zotero_key FK."""
    return f"---\nsource_key: {source_key}\nprovider: example-library\nsource_hash: {source_hash}\n---\n# Mirror\n\n## 1 Body\n\n{body}\n"


def _build_knowledge_base(root: Path) -> Path:
    """Lay down the one paper, its mirror, the registries, and the meta stubs."""
    builder = KnowledgeBaseBuilder(root)
    builder.add_unit_document("Papers", "graph-cost", _PAPER)
    builder.add_source_mirror("graph-cost", _mirror("GRAPHKEY1", "g001", "Graph index construction token cost."))
    builder.add_vocabulary()
    builder.add_meta_stubs()
    builder.add_knowledge_base_schema()
    return root


@pytest.fixture
def indexed(migrated: str, tmp_path: Path) -> str:
    """Index the mini knowledge base into the freshly-migrated DB; return the admin URL.

    Drives the real `memory index` through the **real container graph** — the same
    `create_container` production runs, over settings pointing at the migrated DB, with
    only the `BatchEmbedder` overridden to the deterministic fake (no Azure). Resolving
    the graph (rather than hand-building the handler) means the seeding exercises the
    providers as wired — a factory gaining a parameter cannot silently diverge from what
    this test runs. The MCP tools then read through `reader_url` (the same DB, the
    `kb_reader` role).
    """
    knowledge_base = _build_knowledge_base(tmp_path)
    base = ApplicationSettings()
    settings = ApplicationSettings(
        infrastructure=base.infrastructure.model_copy(update={"database": DatabaseSettings(url=migrated, reader_url=migrated)})
    )
    container = create_container(settings, knowledge_base=KnowledgeBaseSettings(path=knowledge_base))
    container.override.set(BatchEmbedder, FakeEmbedding())
    result = runner.invoke(app, ["index", "--knowledge-base", str(knowledge_base)], obj=lambda: ContainerResolver(container))
    assert result.exit_code == 0, result.stdout
    return migrated


@pytest.fixture
def reader(indexed: str, reader_url: str) -> str:
    """The reader URL over the *indexed* DB (forces indexing before any read)."""
    del indexed  # ordering dependency: index must run before the MCP session reads
    return reader_url


def _text(result: CallToolResult) -> str:
    """The first text block of a tool result (the tools return a single text payload)."""
    block = result.content[0]
    assert block.type == "text"
    return block.text


async def _open_session(reader_url: str) -> tuple[FastMCP, AsyncEngine]:
    """A FastMCP server bound to the reader engine, plus the engine to dispose afterwards.

    The caller opens the in-memory client session over the returned server (an async
    context manager) and disposes the engine once the session closes.
    """
    engine = create_async_engine(reader_url, poolclass=NullPool)
    await orm.register_pgvector(engine)
    gateway = ReadOnlySqlGateway(engine)
    server = create_mcp_server(gateway, DescribeSchemaHandler(gateway), collection="research")
    return server, engine


class TestMcpSession:
    async def test_lists_exactly_the_two_tools_with_search_descriptions(self, reader: str):
        server, engine = await _open_session(reader)
        try:
            async with connect_in_memory(server) as client:
                await client.initialize()
                listing = await client.list_tools()
        finally:
            await engine.dispose()

        by_name = {tool.name: (tool.description or "") for tool in listing.tools}
        assert set(by_name) == {"describe_schema", "run_sql"}
        assert all(by_name.values())
        assert any("api.search" in description for description in by_name.values())

    async def test_describe_schema_renders_the_api_surface(self, reader: str):
        server, engine = await _open_session(reader)
        try:
            async with connect_in_memory(server) as client:
                await client.initialize()
                result = await client.call_tool("describe_schema", {})
        finally:
            await engine.dispose()

        assert result.isError is False
        text = _text(result)
        assert "api.cards" in text
        assert "api.search" in text

    async def test_run_sql_reads_api_about(self, reader: str):
        server, engine = await _open_session(reader)
        try:
            async with connect_in_memory(server) as client:
                await client.initialize()
                result = await client.call_tool("run_sql", {"sql": "SELECT * FROM api.about"})
        finally:
            await engine.dispose()

        assert result.isError is False
        # api.about is a single orientation row — the rendered text is non-empty content.
        assert _text(result).strip()

    async def test_write_sql_returns_the_actionable_read_only_error(self, reader: str):
        server, engine = await _open_session(reader)
        try:
            async with connect_in_memory(server) as client:
                await client.initialize()
                result = await client.call_tool("run_sql", {"sql": "INSERT INTO kb.pages (id, type) VALUES ('x', 'paper')"})
        finally:
            await engine.dispose()

        # The write never runs, and the tool returns a readable message, not a crash.
        text = _text(result)
        assert result.isError
        assert "read-only" in text
        assert "log_session" not in text

    async def test_row_limit_is_honoured(self, reader: str):
        server, engine = await _open_session(reader)
        try:
            async with connect_in_memory(server) as client:
                await client.initialize()
                # api.cards has at least the one indexed paper page; cap the read at one row.
                result = await client.call_tool("run_sql", {"sql": "SELECT id FROM api.cards", "row_limit": 1})
        finally:
            await engine.dispose()

        assert result.isError is False
        # The cap held to one row: the `{"rows": [...], "note": ...}` envelope carries exactly one
        # structured row with the single selected column, and — because the indexed knowledge base has
        # more than one card — the contract's truncation note.
        envelope = json.loads(_text(result))
        assert list(envelope) == ["rows", "note"]
        assert len(envelope["rows"]) == 1
        assert list(envelope["rows"][0]) == ["id"]  # the single selected column
        assert "narrow your filters" in envelope["note"]  # more rows existed → cap engaged
