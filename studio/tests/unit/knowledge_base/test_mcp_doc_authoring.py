"""Pins `studio mcp`'s documentation-authoring opt-in (`knowledge_base_profile` / `skeleton` / `write_note`).

`_build_mcp_server` mounts the trio on an `AuthoringTools` built for the served knowledge base when either
its own `doc_authoring` argument is set, or the served knowledge base's `knowledge-base-schema.json` sets
`retrieval.doc_authoring`. Both halves of that gate, and the read-only default, are pinned here by
listing the built server's registered tools over the SDK's in-memory client session, the same
technique `test_cli.py` uses to pin the `--authoring` opt-in.
"""

from __future__ import annotations

import json

from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from core.ai.embedding import BatchEmbedder
from engram.indexing.contracts import KnowledgeBaseReader
from engram.retrieval.envelope import EnvelopeHandler
from engram.retrieval.queries import DescribeSchemaHandler, FetchHandler, GraphExpandHandler, GraphPathsHandler, SearchHandler
from engram.testing.fakes.embedding import FakeEmbedding
from engram.testing.fakes.knowledge_base_reader import FakeKnowledgeBaseReader
from engram.testing.specs import research_spec
from mcp.client import Client
from sqlalchemy import NullPool
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

from studio.knowledge_base.cli import _build_mcp_server
from studio.knowledge_base.settings import DatabaseSettings, KnowledgeBaseSettings, RetrievalSettings
from studio.knowledge_base.wiring.contracts import AdminSqlGateway, ReaderSqlGateway, SearchReranking

if TYPE_CHECKING:
    from pathlib import Path

    from mcp.server.mcpserver import MCPServer

_DOC_AUTHORING_TOOLS = frozenset({"knowledge_base_profile", "skeleton", "write_note", "decide_page", "resolve_entities", "read_page"})


def _make_knowledge_base(tmp_path: Path, *, doc_authoring_in_spec: bool = False) -> KnowledgeBaseSettings:
    """A real `KnowledgeBaseSettings` over a minimal on-disk knowledge_base.

    With the research template's own schema (`retrieval.doc_authoring` off), the CLI flag is the
    whole gate. `doc_authoring_in_spec=True` writes a minimal `knowledge-base-schema.json` with
    `retrieval.doc_authoring` set instead, so `_build_mcp_server` reads the knowledge_base_schema-driven
    half of the gate rather than the CLI flag.
    """
    (tmp_path / "_meta").mkdir(parents=True)
    if doc_authoring_in_spec:
        spec: dict[str, object] = {"slug": "alpha", "page_types": [{"page_type": "note"}], "retrieval": {"doc_authoring": True}}
        (tmp_path / "knowledge-base-schema.json").write_text(json.dumps(spec), encoding="utf-8")
    else:
        (tmp_path / "knowledge-base-schema.json").write_text(research_spec().model_dump_json(), encoding="utf-8")
    return KnowledgeBaseSettings(path=tmp_path)


def _inert_reader_gateway() -> ReaderSqlGateway:
    """A real `ReaderSqlGateway` over an unconnected engine (wired, never queried by a tool listing)."""
    engine = create_async_engine("postgresql+asyncpg://reader:pw@localhost:5433/knowledge_bases", poolclass=NullPool)
    return ReaderSqlGateway(engine)


def _inert_admin_gateway() -> AdminSqlGateway:
    """A real `AdminSqlGateway` over an unconnected engine, the freshness stamp's read side."""
    engine = create_async_engine("postgresql+asyncpg://admin:pw@localhost:5433/knowledge_bases", poolclass=NullPool)
    return AdminSqlGateway(engine)


def _inert_engine() -> AsyncEngine:
    """A real, never-connected `AsyncEngine`; `_build_mcp_server` disposes it before returning."""
    return create_async_engine("postgresql+asyncpg://admin:pw@localhost:5433/knowledge_bases", poolclass=NullPool)


@dataclass
class _McpContainer:
    """Fake `Resolver` resolving exactly the deps a read-only (`authoring=False`) `_build_mcp_server` asks for.

    Narrowed by `isinstance` (the `FakeContainer` idiom), so `get` honestly returns `T`. The
    documentation-authoring build path resolves nothing beyond what a read-only build already does:
    it builds its own admin engine from `DatabaseSettings.url` and reads the knowledge base's checkout from
    `KnowledgeBaseSettings`, both already resolved here.
    """

    knowledge_base: KnowledgeBaseSettings
    retrieval: RetrievalSettings = field(default_factory=RetrievalSettings)
    gateway: ReaderSqlGateway = field(default_factory=_inert_reader_gateway)
    admin_gateway: AdminSqlGateway = field(default_factory=_inert_admin_gateway)
    reader: KnowledgeBaseReader = field(default_factory=FakeKnowledgeBaseReader)
    embedder: FakeEmbedding = field(default_factory=FakeEmbedding)
    engine: AsyncEngine = field(default_factory=_inert_engine)

    async def get[T](self, klass: type[T]) -> T:
        services: dict[type, object] = {
            ReaderSqlGateway: self.gateway,
            AdminSqlGateway: self.admin_gateway,
            DescribeSchemaHandler: DescribeSchemaHandler(self.gateway),
            GraphExpandHandler: GraphExpandHandler(self.gateway, "research"),
            GraphPathsHandler: GraphPathsHandler(self.gateway, "research"),
            SearchHandler: SearchHandler(self.gateway, self.embedder),
            FetchHandler: FetchHandler(self.gateway, "research"),
            EnvelopeHandler: EnvelopeHandler(self.gateway),
            RetrievalSettings: self.retrieval,
            SearchReranking: SearchReranking(reranking=None),
            KnowledgeBaseSettings: self.knowledge_base,
            DatabaseSettings: DatabaseSettings(
                url="postgresql+asyncpg://u:p@h:5433/knowledge_bases", reader_url="postgresql+asyncpg://r:p@h:5433/knowledge_bases"
            ),
            BatchEmbedder: self.embedder,
            KnowledgeBaseReader: self.reader,
            AsyncEngine: self.engine,
        }
        service = services.get(klass)
        if not isinstance(service, klass):
            msg = f"_McpContainer has no registration for {klass!r}"
            raise KeyError(msg)
        return service


async def _tool_names(server: MCPServer) -> frozenset[str]:
    """The names of the tools the built server registered (over the SDK's in-memory session)."""
    async with Client(server) as client:
        listing = await client.list_tools()
    return frozenset(tool.name for tool in listing.tools)


def _no_registry(tmp_path: Path) -> Path:
    """A settings path that does not exist: the hermetic default for a test not exercising the registry scan.

    `_build_mcp_server` defaults `settings_path` to this deployment's REAL, gitignored settings file,
    so every doc-authoring test here passes one explicitly rather than reading whatever the running
    machine happens to have registered.
    """
    return tmp_path / "does-not-exist" / "application_settings.json"


class TestDocAuthoringFlag:
    """`_build_mcp_server`'s `doc_authoring` argument is the CLI-flag half of the opt-in gate."""

    async def test_flag_on_registers_all_four_tools(self, tmp_path: Path):
        container = _McpContainer(knowledge_base=_make_knowledge_base(tmp_path))

        server = await _build_mcp_server(container, authoring=False, doc_authoring=True, settings_path=_no_registry(tmp_path))

        assert await _tool_names(server) >= _DOC_AUTHORING_TOOLS

    async def test_flag_off_and_spec_off_registers_none_of_the_four(self, tmp_path: Path):
        container = _McpContainer(knowledge_base=_make_knowledge_base(tmp_path))

        server = await _build_mcp_server(container, authoring=False, doc_authoring=False, settings_path=_no_registry(tmp_path))

        assert _DOC_AUTHORING_TOOLS.isdisjoint(await _tool_names(server))


class TestDocAuthoringKnowledgeBasespecOptIn:
    """The knowledge_base_schema half of the gate: `retrieval.doc_authoring` alone, with the CLI flag left off."""

    async def test_knowledge_base_schema_opt_in_alone_registers_all_four_tools(self, tmp_path: Path):
        container = _McpContainer(knowledge_base=_make_knowledge_base(tmp_path, doc_authoring_in_spec=True))

        server = await _build_mcp_server(container, authoring=False, doc_authoring=False, settings_path=_no_registry(tmp_path))

        assert await _tool_names(server) >= _DOC_AUTHORING_TOOLS


def _write_shareable_knowledge_base(root: Path, *, slug: str) -> None:
    """A minimal on-disk knowledge base at `root` whose spec shares and opts into documentation authoring."""
    root.mkdir(parents=True)
    (root / "_meta").mkdir()
    spec = {"slug": slug, "page_types": [{"page_type": "note"}], "retrieval": {"doc_authoring": True}}
    (root / "knowledge-base-schema.json").write_text(json.dumps(spec), encoding="utf-8")


async def _unknown_knowledge_base_error_text(server: MCPServer) -> str:
    """Call `skeleton` with a knowledge base name no target carries; `AuthoringTools._target` answers with the valid names."""
    async with Client(server) as client:
        result = await client.call_tool("skeleton", {"knowledge_base": "not-a-real-knowledge-base", "page_type": "note"})
    assert result.is_error
    block = result.content[0]
    assert block.type == "text"
    return block.text


class TestDocAuthoringMultiTarget:
    """The doors mount this session's own knowledge base plus every other registered knowledge base that shares and opts in."""

    async def test_a_shareable_registered_knowledge_base_becomes_a_reachable_target(self, tmp_path: Path):
        own_root = tmp_path / "own"
        own_root.mkdir()
        other_root = tmp_path / "other"
        _write_shareable_knowledge_base(other_root, slug="beta")
        settings_path = tmp_path / "application_settings.json"
        settings_path.write_text(json.dumps({"knowledge_bases": {"beta": {"path": str(other_root)}}}), encoding="utf-8")
        container = _McpContainer(knowledge_base=_make_knowledge_base(own_root, doc_authoring_in_spec=True))

        server = await _build_mcp_server(container, authoring=False, doc_authoring=False, settings_path=settings_path)

        assert "beta" in await _unknown_knowledge_base_error_text(server)

    async def test_a_private_registered_knowledge_base_never_becomes_a_target(self, tmp_path: Path):
        own_root = tmp_path / "own"
        own_root.mkdir()
        private_root = tmp_path / "private"
        _write_shareable_knowledge_base(private_root, slug="secret")
        settings_path = tmp_path / "application_settings.json"
        settings_path.write_text(json.dumps({"knowledge_bases": {"secret": {"path": str(private_root), "private": True}}}), encoding="utf-8")
        container = _McpContainer(knowledge_base=_make_knowledge_base(own_root, doc_authoring_in_spec=True))

        server = await _build_mcp_server(container, authoring=False, doc_authoring=False, settings_path=settings_path)

        assert "secret" not in await _unknown_knowledge_base_error_text(server)

    async def test_a_missing_registry_mounts_only_this_sessions_own_knowledge_base(self, tmp_path: Path):
        container = _McpContainer(knowledge_base=_make_knowledge_base(tmp_path, doc_authoring_in_spec=True))

        server = await _build_mcp_server(container, authoring=False, doc_authoring=False, settings_path=_no_registry(tmp_path))

        assert "alpha" in await _unknown_knowledge_base_error_text(server)  # only this session's own slug is a valid target

    async def test_a_missing_registry_adds_no_note_to_the_instructions(self, tmp_path: Path):
        container = _McpContainer(knowledge_base=_make_knowledge_base(tmp_path, doc_authoring_in_spec=True))

        server = await _build_mcp_server(container, authoring=False, doc_authoring=False, settings_path=_no_registry(tmp_path))

        assert server.instructions is not None
        assert "could not be read" not in server.instructions

    async def test_an_unreadable_registry_notes_the_degrade_in_the_instructions(self, tmp_path: Path):
        settings_path = tmp_path / "application_settings.json"
        settings_path.write_text("not valid json", encoding="utf-8")
        container = _McpContainer(knowledge_base=_make_knowledge_base(tmp_path, doc_authoring_in_spec=True))

        server = await _build_mcp_server(container, authoring=False, doc_authoring=False, settings_path=settings_path)

        assert server.instructions is not None
        assert "could not be read" in server.instructions
        # the surface still serves, and this session's own knowledge base is still reachable
        assert "alpha" in await _unknown_knowledge_base_error_text(server)
