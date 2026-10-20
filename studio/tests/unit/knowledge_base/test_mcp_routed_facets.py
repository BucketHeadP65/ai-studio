"""`studio mcp` serves a `search` that filters each knowledge base by the facet keys its own schema declares.

`_build_mcp_server` routes `search` over the served knowledge base and every shared registered one. Each read
route carries the facet keys of its own `knowledge-base-schema.json`, so a key filters the base that declares it,
a key the searched base does not declare is refused naming that base's keys, and the tool's description names
every base's keys. Driven over the SDK's in-memory client session against the server as the command builds it.
"""

from __future__ import annotations

import json

from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from engram.retrieval.domain import SqlResult
from engram.testing.fakes.gateway import FakeSqlGateway, RecordedQuery
from mcp.client import Client

from studio.knowledge_base.cli import _build_mcp_server
from studio.knowledge_base.settings import KnowledgeBaseSettings
from tests.unit.knowledge_base.test_cli_serve import _McpServerContainer

if TYPE_CHECKING:
    from collections.abc import Mapping
    from pathlib import Path

    from mcp.types import CallToolResult


def _knowledge_base(root: Path, *, slug: str, facet_keys: tuple[str, ...]) -> Path:
    """A minimal on-disk knowledge base at `root` whose schema declares `facet_keys`."""
    (root / "_meta").mkdir(parents=True)
    schema = {"slug": slug, "page_types": [{"page_type": "note"}], "facets": [{"key": key} for key in facet_keys]}
    (root / "knowledge-base-schema.json").write_text(json.dumps(schema), encoding="utf-8")
    return root


def _text(result: CallToolResult) -> str:
    block = result.content[0]
    assert block.type == "text"
    return block.text


async def test_the_served_search_filters_each_knowledge_base_by_the_facet_keys_its_own_schema_declares(tmp_path: Path) -> None:
    served = _knowledge_base(tmp_path / "alpha", slug="alpha", facet_keys=("shade", "texture"))
    registered = _knowledge_base(tmp_path / "beta", slug="beta", facet_keys=("weight",))
    settings_path = tmp_path / "application_settings.json"
    settings_path.write_text(json.dumps({"knowledge_bases": {"beta": {"path": str(registered)}}}), encoding="utf-8")
    gateway = FakeSqlGateway(result=SqlResult(rows=(), columns=()))
    container = _McpServerContainer(knowledge_base=KnowledgeBaseSettings(path=served), gateway=gateway)

    server = await _build_mcp_server(container, settings_path=settings_path)
    async with Client(server) as client:
        listing = await client.list_tools()
        own = await client.call_tool("search", {"query": "brine", "facets": {"shade": ["teal", "amber"]}})
        other = await client.call_tool("search", {"query": "brine", "collection": "beta", "facets": {"weight": ["heavy"]}})
        refused = await client.call_tool("search", {"query": "brine", "facets": {"weight": ["heavy"]}})

    [tool] = [published for published in listing.tools if published.name == "search"]
    assert "facets" in tool.input_schema["properties"]
    assert "'alpha' (`shade`, `texture`), 'beta' (`weight`)" in (tool.description or "")
    assert own.is_error is False, _text(own)
    assert other.is_error is False, _text(other)
    assert refused.is_error is True
    assert "Not a facet key the 'alpha' knowledge base declares: 'weight'. The declared keys are `shade`, `texture`." in _text(refused)
    searched = [
        (json.loads(str(call.params["facets"])), call.params["collection"]) for call in gateway.rows_calls if "facets" in call.params
    ]
    assert searched == [({"shade": ["teal", "amber"]}, "alpha"), ({"weight": ["heavy"]}, "beta")]


async def test_the_served_search_states_the_cap_on_hits_per_document_the_database_applies(tmp_path: Path) -> None:
    served = _knowledge_base(tmp_path / "alpha", slug="alpha", facet_keys=("shade",))
    registered = _knowledge_base(tmp_path / "beta", slug="beta", facet_keys=("weight",))
    settings_path = tmp_path / "application_settings.json"
    settings_path.write_text(json.dumps({"knowledge_bases": {"beta": {"path": str(registered)}}}), encoding="utf-8")
    capped = SqlResult(rows=({"hits_per_document": 2},), columns=("hits_per_document",))
    gateway = FakeSqlGateway(result=SqlResult(rows=(), columns=()), routed={"api.search_hits_per_document": capped})
    container = _McpServerContainer(knowledge_base=KnowledgeBaseSettings(path=served), gateway=gateway)

    server = await _build_mcp_server(container, settings_path=settings_path)
    async with Client(server) as client:
        listing = await client.list_tools()

    [tool] = [published for published in listing.tools if published.name == "search"]
    assert "At most 2 hits come from one document" in (tool.description or "")


@dataclass
class _CapsByCollection(FakeSqlGateway):
    """A gateway answering each knowledge base's cap on hits per document by the collection the read names."""

    caps: dict[str, int] = field(default_factory=dict)

    async def rows(self, sql: str, params: Mapping[str, object] | None = None, *, cap: int = 50) -> SqlResult:
        if "api.search_hits_per_document" not in sql:
            return await super().rows(sql, params, cap=cap)
        self.rows_calls.append(RecordedQuery(sql=sql, params=dict(params or {}), cap=cap))
        answer = self.caps.get(str((params or {}).get("collection")), 0)
        return SqlResult(rows=({"hits_per_document": answer},), columns=("hits_per_document",))


async def test_each_knowledge_base_reads_its_own_cap_and_bases_that_disagree_state_none(tmp_path: Path) -> None:
    served = _knowledge_base(tmp_path / "alpha", slug="alpha", facet_keys=("shade",))
    registered = _knowledge_base(tmp_path / "beta", slug="beta", facet_keys=("weight",))
    settings_path = tmp_path / "application_settings.json"
    settings_path.write_text(json.dumps({"knowledge_bases": {"beta": {"path": str(registered)}}}), encoding="utf-8")
    gateway = _CapsByCollection(result=SqlResult(rows=(), columns=()), caps={"alpha": 2, "beta": 1})
    container = _McpServerContainer(knowledge_base=KnowledgeBaseSettings(path=served), gateway=gateway)

    server = await _build_mcp_server(container, settings_path=settings_path)
    async with Client(server) as client:
        listing = await client.list_tools()

    [tool] = [published for published in listing.tools if published.name == "search"]
    read = [call.params["collection"] for call in gateway.rows_calls if "api.search_hits_per_document" in call.sql]
    assert read == ["alpha", "beta"]
    assert "hits come from one document" not in (tool.description or "")
    assert "distinct document" not in (tool.description or "")
