r"""Unit specs for the dialect seam's settings + DI wiring.

Four concerns, all pure (no database, no network):

* the ``RetrievalSettings.dialect`` field defaults to :attr:`DbDialect.PG_TEXTSEARCH`
  (the analog/cloud grammar; ParadeDB is the opt-in) and coerces a
  string into the enum — exercised via `model_validate` and explicit init kwargs, so
  the mapping is proven without depending on ambient state;
* the package's curated public surface is importable from
  `engram.retrieval.search_dialect` directly (`SearchDialect`, `DbDialect`,
  `SearchDialects`, `PgSearchDialect`, `PgTextSearchDialect`);
* the `search_dialects_provider` wireup factory builds a registry whose
  `select` returns the concrete dialect per backend — pinned both directly (the
  provider function in isolation) and through the real `create_container` graph;
* the `graph_backends_provider` factory builds the link-graph backend registry
  (pinned directly and through `create_container`, mirroring the BM25 registry),
  and the graph query handlers (multi-hop :class:`GraphExpandHandler`, ranked
  :class:`GraphPathsHandler`) bind around the injected reader gateway — proven via
  their provider functions over a :class:`ReaderSqlGateway` wrapping a
  lazily-created (unconnected) engine.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from engram.retrieval.graph_backend import GraphBackends, build_graph_backends
from engram.retrieval.queries import GraphExpandHandler, GraphPathsHandler
from engram.retrieval.search_dialect import (
    DbDialect,
    PgSearchDialect,
    PgTextSearchDialect,
    SearchDialect,
    SearchDialects,
    build_search_dialects,
)
from engram.testing.specs import research_spec
from sqlalchemy.ext.asyncio import create_async_engine

from studio.knowledge_base import settings as settings_module
from studio.knowledge_base.settings import ApplicationSettings, GraphBackend, KnowledgeBaseSettings, RetrievalSettings
from studio.knowledge_base.wiring.contracts import ReaderSqlGateway
from tests.support.http_app import write_settings_json

if TYPE_CHECKING:
    from pathlib import Path

    import pytest

_READER_URL = "postgresql+asyncpg://kb_reader:kb_reader@localhost:5433/knowledge_bases"


def _retrieval(dialect: DbDialect | None = None) -> RetrievalSettings:
    """A `RetrievalSettings` carrying the dialect (now its home — the connection lives in the resource).

    Init kwargs are explicit, so the result depends only on what is passed — no
    ambient environment, no monkeypatching. When `dialect` is omitted the field
    falls back to its default.
    """
    if dialect is None:
        return RetrievalSettings()
    return RetrievalSettings(dialect=dialect)


class TestDialectSetting:
    def test_defaults_to_pg_textsearch(self):
        assert _retrieval().dialect is DbDialect.PG_TEXTSEARCH

    def test_is_typed_as_the_db_dialect_enum(self):
        assert isinstance(_retrieval().dialect, DbDialect)

    def test_explicit_override_is_honoured(self):
        assert _retrieval(DbDialect.PG_TEXTSEARCH).dialect is DbDialect.PG_TEXTSEARCH

    def test_model_validate_coerces_the_dialect_string(self):
        settings = RetrievalSettings.model_validate({"dialect": "pg_textsearch"})

        assert settings.dialect is DbDialect.PG_TEXTSEARCH

    def test_app_retrieval_dialect_env_seam_resolves_to_the_enum(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
        # The APP_ override seam: APP_RETRIEVAL__DIALECT maps onto retrieval.dialect and coerces.
        knowledge_base_root = tmp_path / "knowledge_base"
        (knowledge_base_root / "_meta").mkdir(parents=True, exist_ok=True)
        (knowledge_base_root / "knowledge-base-schema.json").write_text("{}", encoding="utf-8")
        monkeypatch.setenv("APP_RETRIEVAL__DIALECT", "pg_textsearch")

        settings = ApplicationSettings()

        assert settings.retrieval.dialect is DbDialect.PG_TEXTSEARCH


class TestPublicSurface:
    def test_exports_the_curated_symbols(self):
        from engram.retrieval import search_dialect

        assert set(search_dialect.__all__) == {
            "DbDialect",
            "PgSearchDialect",
            "PgTextSearchDialect",
            "SearchDialect",
            "SearchDialects",
            "build_search_dialects",
        }

    def test_each_export_is_the_real_object(self):
        # The names re-exported from the package are the same objects the
        # sub-modules (or settings, for DbDialect) define — not shadows or stale aliases.
        from engram.retrieval.search_dialect import dialect, pg_search, pg_textsearch, registry

        assert SearchDialect is dialect.SearchDialect
        assert DbDialect is settings_module.DbDialect
        assert SearchDialects is registry.SearchDialects
        assert PgSearchDialect is pg_search.PgSearchDialect
        assert PgTextSearchDialect is pg_textsearch.PgTextSearchDialect


class TestBuildSearchDialects:
    """The `build_search_dialects` factory — the single backend-registration point."""

    def test_returns_a_registry_binding_each_concrete_dialect(self):
        registry = build_search_dialects()

        assert isinstance(registry, SearchDialects)
        assert isinstance(registry.select(DbDialect.PG_SEARCH), PgSearchDialect)
        assert isinstance(registry.select(DbDialect.PG_TEXTSEARCH), PgTextSearchDialect)


class TestSearchDialectsProvider:
    """The `search_dialects_provider` factory — registry built with both dialects."""

    def test_provider_returns_a_registry_selecting_each_backend(self):
        from studio.knowledge_base.wiring.serve import search_dialects_provider

        registry = search_dialects_provider()

        assert isinstance(registry, SearchDialects)
        assert registry.select(DbDialect.PG_SEARCH).name == "pg_search"
        assert registry.select(DbDialect.PG_TEXTSEARCH).name == "pg_textsearch"

    def test_provider_binds_the_concrete_dialect_types(self):
        from studio.knowledge_base.wiring.serve import search_dialects_provider

        registry = search_dialects_provider()

        assert isinstance(registry.select(DbDialect.PG_SEARCH), PgSearchDialect)
        assert isinstance(registry.select(DbDialect.PG_TEXTSEARCH), PgTextSearchDialect)


class TestSearchDialectsContainerWiring:
    """The real wireup graph: `create_container` resolves the registry as a singleton."""

    async def test_resolves_the_registry_and_selects_each_backend(self, tmp_path: Path):
        from studio.knowledge_base.container import create_container

        container = create_container(_settings(tmp_path), knowledge_base=KnowledgeBaseSettings(path=_knowledge_base_root(tmp_path)))

        registry = await container.get(SearchDialects)

        assert registry.select(DbDialect.PG_SEARCH).name == "pg_search"
        assert registry.select(DbDialect.PG_TEXTSEARCH).name == "pg_textsearch"

    async def test_registry_is_a_singleton(self, tmp_path: Path):
        from studio.knowledge_base.container import create_container

        container = create_container(_settings(tmp_path), knowledge_base=KnowledgeBaseSettings(path=_knowledge_base_root(tmp_path)))

        first = await container.get(SearchDialects)
        second = await container.get(SearchDialects)

        assert first is second


class TestGraphBackendsProvider:
    """The `graph_backends_provider` factory — registry built with both graph backends."""

    def test_provider_returns_a_registry_selecting_each_backend(self):
        from studio.knowledge_base.wiring.serve import graph_backends_provider

        registry = graph_backends_provider()

        assert isinstance(registry, GraphBackends)
        assert registry.select(GraphBackend.CTE).name == "cte"
        assert registry.select(GraphBackend.AGE).name == "age"

    def test_provider_builds_via_the_factory(self):
        from studio.knowledge_base.wiring.serve import graph_backends_provider

        registry = graph_backends_provider()
        reference = build_graph_backends()

        assert registry.by_key.keys() == reference.by_key.keys()


class TestGraphBackendsContainerWiring:
    """The real wireup graph: `create_container` resolves the backend registry as a singleton."""

    async def test_resolves_the_registry_and_selects_each_backend(self, tmp_path: Path):
        from studio.knowledge_base.container import create_container

        container = create_container(_settings(tmp_path), knowledge_base=KnowledgeBaseSettings(path=_knowledge_base_root(tmp_path)))

        registry = await container.get(GraphBackends)

        assert registry.select(GraphBackend.CTE).name == "cte"
        assert registry.select(GraphBackend.AGE).name == "age"

    async def test_registry_is_a_singleton(self, tmp_path: Path):
        from studio.knowledge_base.container import create_container

        container = create_container(_settings(tmp_path), knowledge_base=KnowledgeBaseSettings(path=_knowledge_base_root(tmp_path)))

        first = await container.get(GraphBackends)
        second = await container.get(GraphBackends)

        assert first is second


def _reader_gateway() -> ReaderSqlGateway:
    """A :class:`ReaderSqlGateway` over a lazily-created engine — no connection is opened.

    `create_async_engine` does not connect, and the gateway constructor only stores the
    engine, so this is the production gateway type the connectors expect without touching a
    database — proving the wiring without the eager-codec engine the container path builds.
    """
    return ReaderSqlGateway(create_async_engine(_READER_URL))


class TestGraphHandlerProviders:
    """The graph handler connectors bind each handler around the injected reader gateway.

    Pinned over a real :class:`ReaderSqlGateway` (the production injection type) wrapping a
    lazily-created engine.
    """

    def test_graph_expand_handler_provider_binds_the_gateway(self):
        from studio.knowledge_base.wiring.serve import graph_expand_handler_provider

        handler = graph_expand_handler_provider(_reader_gateway(), research_spec())

        assert isinstance(handler, GraphExpandHandler)

    def test_graph_paths_handler_provider_binds_the_gateway(self):
        from studio.knowledge_base.wiring.serve import graph_paths_handler_provider

        handler = graph_paths_handler_provider(_reader_gateway(), research_spec())

        assert isinstance(handler, GraphPathsHandler)


def _knowledge_base_root(tmp_path: Path) -> Path:
    """A knowledge base on disk, the shape `KnowledgeBaseSettings` accepts."""
    knowledge_base_root = tmp_path / "knowledge_base"
    (knowledge_base_root / "_meta").mkdir(parents=True, exist_ok=True)
    (knowledge_base_root / "knowledge-base-schema.json").write_text("{}", encoding="utf-8")
    return knowledge_base_root


def _settings(tmp_path: Path) -> ApplicationSettings:
    """A settings tree read from a real file under `tmp_path` — no env, no network."""
    return ApplicationSettings(json_file=write_settings_json(tmp_path / "application_settings.json"))
