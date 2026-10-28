"""`studio mcp` run as its own process against a database it cannot reach, driven over stdio by an MCP client.

The server must announce the unreachable database on stderr, complete the MCP handshake and list its tools, and answer
each tool call with an error rather than exit, so a client that started before the database still has a server once the
database comes up. It cannot check its role at launch, so it builds no tool that writes, even when the schema opts in.
The addresses are real: one with nothing listening (`127.0.0.1:1`), and a throwaway Postgres refusing a wrong password
or a database name it does not hold, never a fake container.
"""

from __future__ import annotations

import json
import os
import sys

from pathlib import Path

import pytest

from engram.kb.contracts import KnowledgeBaseSchema
from engram.testing.knowledge_base_builder import KnowledgeBaseBuilder
from fastmcp import Client
from fastmcp.client.transports import StdioTransport
from sqlalchemy.engine import make_url

_STUDIO = Path(sys.executable).parent / "studio"
_NOBODY = "postgresql+asyncpg://kb_reader:unused@127.0.0.1:1/knowledge_bases"

# The studio refuses to start without these sections, and an empty one takes every default.
_STUDIO_SECTIONS = ("APP_RETRIEVAL", "APP_INGESTION", "APP_SOURCES", "APP_EVALS")

# The server builds its embedding model as it starts, so it needs an endpoint. Keyword search never calls it.
_UNCALLED_EMBEDDING_ENDPOINT = "https://embeddings.example.invalid"

_INIT_TIMEOUT_SECONDS = 120

_WRITE_TOOLS = frozenset({"knowledge_base_profile", "skeleton", "write_note", "decide_page", "resolve_entities", "read_page"})


def _knowledge_base(root: Path) -> None:
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


def _env(url: str) -> dict[str, str]:
    """This process's environment with both database URLs at `url` and every required section set."""
    env = {
        **os.environ,
        "APP_INFRASTRUCTURE__DATABASE__URL": url,
        "APP_INFRASTRUCTURE__DATABASE__READER_URL": url,
        "APP_INFRASTRUCTURE__AZURE_OPENAI__ENDPOINT": _UNCALLED_EMBEDDING_ENDPOINT,
        "APP_KNOWLEDGE_BASES": json.dumps({}),
    }
    env.update(dict.fromkeys(_STUDIO_SECTIONS, "{}"))
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
