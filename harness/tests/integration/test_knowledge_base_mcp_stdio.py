"""The cross-process knowledge base plugin, spawned over stdio against a throwaway database, read-only.

The harness spawns `studio mcp` from the studio checkout with the knowledge base reader role in both database URLs,
naming the knowledge base it serves. The server must complete the MCP handshake and answer `search` and `fetch`
through that role alone. The knowledge base is registered by name, so the server's startup reads its last refresh,
which the reader role has no grant on. The test skips when Docker is unavailable (the `migrated` fixture raises
`pytest.skip`).
"""

from __future__ import annotations

import json

from typing import TYPE_CHECKING

import pytest

from engram.indexing.contracts import (
    FilesystemKnowledgeBaseReader,
    IndexKnowledgeBase,
    IndexKnowledgeBaseHandler,
    SqlAlchemyKnowledgeBaseUnitOfWork,
)
from engram.indexing.infrastructure import orm
from engram.kb.contracts import KnowledgeBaseSchema
from engram.testing.fakes.embedding import FakeEmbedding
from engram.testing.fakes.uow import FakeValidationReportSink
from engram.testing.knowledge_base_builder import KnowledgeBaseBuilder
from fastmcp import Client
from sqlalchemy import NullPool
from sqlalchemy.ext.asyncio import create_async_engine

from harness.adapters.knowledge_base import knowledge_base_stdio_transport
from harness.adapters.store import KNOWLEDGE_BASE_READER_ENV

if TYPE_CHECKING:
    from pathlib import Path

    from fastmcp.client.client import CallToolResult

_NAME = "served"

# The studio refuses to start without these sections, and an empty one takes every default. Exported here, they win
# over any settings file the studio checkout holds, so the subprocess reads no machine's own retrieval or sources.
_STUDIO_SECTIONS = ("APP_RETRIEVAL", "APP_INGESTION", "APP_SOURCES", "APP_EVALS")

# The server builds its embedding model as it starts, so it needs an endpoint. Keyword search never calls it.
_UNCALLED_EMBEDDING_ENDPOINT = ("APP_INFRASTRUCTURE__AZURE_OPENAI__ENDPOINT", "https://embeddings.example.invalid")

# The handshake waits for `uv run` and the studio's imports, which a cold machine takes many seconds over.
_INIT_TIMEOUT_SECONDS = 120


def _knowledge_base(root: Path) -> KnowledgeBaseSchema:
    """Lay down one knowledge base of one page, searched by keyword alone so no query is embedded."""
    spec = KnowledgeBaseSchema.model_validate(
        {
            "slug": _NAME,
            "page_types": [{"page_type": "reference", "id_prefix": "paper", "folder": "Units/References"}],
            "retrieval": {"collection": _NAME, "mode": "fixed", "profile": "search-keyword"},
        }
    )
    builder = KnowledgeBaseBuilder(root)
    builder.add_knowledge_base_schema(spec)
    builder.add_unit_document(
        "Units/References",
        "graph",
        "---\nid: paper.graph\ntype: reference\ntitle: Graph\nsummary: Graph evidence assessment\nstatus: active\n---\n\n"
        "## Findings\n\nGraph evidence assessment body.\n",
    )
    return spec


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
        await handler.handle(IndexKnowledgeBase(collection=_NAME, collection_named=True, embed=False))
    finally:
        await engine.dispose()


def _text(result: CallToolResult) -> str:
    """The tool result's single text block."""
    block = result.content[0]
    assert block.type == "text"
    return block.text


@pytest.mark.timeout(300)
async def test_the_read_only_server_completes_the_handshake_and_serves_search_and_fetch(
    tmp_path: Path, migrated: str, reader_url: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / _NAME
    await _index(root, _knowledge_base(root), migrated)
    monkeypatch.setenv(KNOWLEDGE_BASE_READER_ENV, reader_url.replace("postgresql+asyncpg://", "postgresql://"))
    monkeypatch.setenv("APP_KNOWLEDGE_BASES", json.dumps({_NAME: {"path": str(root)}}))
    for section in _STUDIO_SECTIONS:
        monkeypatch.setenv(section, "{}")
    monkeypatch.setenv(*_UNCALLED_EMBEDDING_ENDPOINT)

    async with Client(knowledge_base_stdio_transport(knowledge_base=root), init_timeout=_INIT_TIMEOUT_SECONDS) as client:
        names = {tool.name for tool in await client.list_tools()}
        assert {"search", "fetch"} <= names

        searched = json.loads(_text(await client.call_tool("search", {"query": "graph evidence assessment"})))
        page_ref = next(hit["ref"] for hit in searched["hits"] if hit["hit_level"] == "card")
        assert page_ref == f"page:{_NAME}/paper.graph"

        fetched = json.loads(_text(await client.call_tool("fetch", {"unit_id": page_ref})))
        assert fetched["ref"] == page_ref
        assert "Graph evidence assessment body" in json.dumps(fetched["content"])
