"""`studio mcp` run as its own process against a database nobody answers at, driven over stdio by an MCP client.

The server must announce the unreachable database on stderr, complete the MCP handshake and list its tools, and answer
each tool call with an error rather than exit, so a client that started before the database still has a server once the
database comes up. The address is a real one with nothing listening (`127.0.0.1:1`), never a fake container.
"""

from __future__ import annotations

import json
import os
import sys

from pathlib import Path

from engram.kb.contracts import KnowledgeBaseSchema
from engram.testing.knowledge_base_builder import KnowledgeBaseBuilder
from fastmcp import Client
from fastmcp.client.transports import StdioTransport

_STUDIO = Path(sys.executable).parent / "studio"
_NOBODY = "postgresql+asyncpg://kb_reader:unused@127.0.0.1:1/knowledge_bases"

# The studio refuses to start without these sections, and an empty one takes every default.
_STUDIO_SECTIONS = ("APP_RETRIEVAL", "APP_INGESTION", "APP_SOURCES", "APP_EVALS")

# The server builds its embedding model as it starts, so it needs an endpoint. Keyword search never calls it.
_UNCALLED_EMBEDDING_ENDPOINT = "https://embeddings.example.invalid"

_INIT_TIMEOUT_SECONDS = 120


def _knowledge_base(root: Path) -> None:
    """Lay down one knowledge base of one page, searched by keyword alone."""
    spec = KnowledgeBaseSchema.model_validate(
        {
            "slug": "served",
            "page_types": [{"page_type": "reference", "id_prefix": "paper", "folder": "Units/References"}],
            "retrieval": {"collection": "served", "mode": "fixed", "profile": "search-keyword"},
        }
    )
    builder = KnowledgeBaseBuilder(root)
    builder.add_knowledge_base_schema(spec)
    builder.add_unit_document(
        "Units/References", "graph", "---\nid: paper.graph\ntype: reference\ntitle: Graph\nstatus: active\n---\n\n## Findings\n\nBody.\n"
    )


def _env() -> dict[str, str]:
    """This process's environment with both database URLs at the unanswered address and every required section set."""
    env = {
        **os.environ,
        "APP_INFRASTRUCTURE__DATABASE__URL": _NOBODY,
        "APP_INFRASTRUCTURE__DATABASE__READER_URL": _NOBODY,
        "APP_INFRASTRUCTURE__AZURE_OPENAI__ENDPOINT": _UNCALLED_EMBEDDING_ENDPOINT,
        "APP_KNOWLEDGE_BASES": json.dumps({}),
    }
    env.update(dict.fromkeys(_STUDIO_SECTIONS, "{}"))
    return env


async def test_the_server_serves_anyway_and_every_call_fails_until_the_database_answers(tmp_path: Path) -> None:
    root = tmp_path / "served"
    _knowledge_base(root)
    log = tmp_path / "server.log"
    transport = StdioTransport(command=str(_STUDIO), args=["mcp", "--knowledge-base", str(root)], env=_env(), cwd=str(tmp_path), log_file=log)

    async with Client(transport, init_timeout=_INIT_TIMEOUT_SECONDS) as client:
        names = {tool.name for tool in await client.list_tools()}
        searched = await client.call_tool("search", {"query": "graph"}, raise_on_error=False)
        still_listed = {tool.name for tool in await client.list_tools()}

    assert {"search", "fetch"} <= names
    assert searched.is_error
    assert still_listed == names
    assert "serving anyway, every call fails until the database is reachable." in log.read_text(encoding="utf-8")
