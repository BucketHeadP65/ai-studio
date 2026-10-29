r"""Composition-root contract for the evals slice (`src/studio/knowledge_base/container.py`).

The evals slice contributes its DI via `studio.knowledge_base.wiring.evals`, which the composition
root registers in `create_container`'s `injectables` list exactly as it registers
`studio.knowledge_base.wiring.serve`. Pinned here through the real wireup graph: resolving the evals retrieval
surface (the :class:`~engram.retrieval.tools.RetrievalHandlers` group and the retrieval
:class:`~pydantic_ai.FunctionToolset`) succeeds, and the :class:`~studio.knowledge_base.settings.EvalsSettings`
sub-instance is resolvable.

The retrieval group's providers inject the four serve query handlers, which in
turn depend on the read-only :class:`~studio.knowledge_base.wiring.serve.ReaderSqlGateway` (and the
:class:`~core.ai.embedding.BatchEmbedder` for search). Those are overridden with
inert fakes so resolving the group touches no Postgres or Azure — mirroring the
sync slice's `test_container` (which overrides the engine + embedder identically).
"""

from __future__ import annotations

import json

from typing import TYPE_CHECKING

import pytest

from core.ai.embedding import BatchEmbedder
from core.cqrs.contracts import Command, MessageBus, PostgresOutbox
from engram.indexing.contracts import CitingPagesChanged, PagesLoaded
from engram.retrieval.tools import RetrievalHandlers, RetrievalToolset, build_retrieval_toolset
from engram.sources.contracts import MirrorStore
from engram.sources.infrastructure.mirror import ObsidianMirrorStore
from engram.testing.fakes.embedding import FakeEmbedding
from engram.testing.fakes.gateway import FakeSqlGateway
from engram.testing.specs import research_spec
from engram_connectors.conversion.contracts import HybridConverter
from engram_connectors.zotero.contracts import SqliteZoteroConnector
from pydantic_ai import FunctionToolset
from sqlalchemy.ext.asyncio import AsyncEngine

from studio.knowledge_base.container import create_container
from studio.knowledge_base.refresh.contracts import RefreshAbandoned, RefreshCompleted, RefreshStarted
from studio.knowledge_base.settings import (
    ApplicationSettings,
    DatabaseSettings,
    EvalsSettings,
    InfrastructureSettings,
    KnowledgeBaseSettings,
    LogfireSettings,
    RetrievalSettings,
    SettingsFile,
    TuningSettings,
    default_settings_path,
)
from studio.knowledge_base.wiring.contracts import ReaderSqlGateway
from studio.knowledge_base.wiring.conversion import DocumentConverterMap
from studio.knowledge_base.wiring.sources import ConnectorMap
from tests.support.http_app import SETTINGS_JSON

if TYPE_CHECKING:
    from pathlib import Path


def _knowledge_base_root(tmp_path: Path) -> Path:
    """A research knowledge base on disk, the shape `KnowledgeBaseSettings` accepts."""
    knowledge_base_root = tmp_path / "knowledge_base"
    (knowledge_base_root / "_meta").mkdir(parents=True)
    (knowledge_base_root / "knowledge-base-schema.json").write_text(research_spec().model_dump_json(), encoding="utf-8")
    return knowledge_base_root


def _settings(tmp_path: Path) -> ApplicationSettings:
    """A settings tree carrying every section, read from a file under `tmp_path` — no env, no network."""
    json_file = tmp_path / "application_settings.json"
    json_file.write_text(json.dumps(SETTINGS_JSON | {"sources": {"zotero": {"data_dir": str(tmp_path)}}}), encoding="utf-8")
    return ApplicationSettings(json_file=json_file)


class TestTheKnowledgeBaseIsAnArgument:
    """The knowledge base selects the container variant, and is not read off the settings."""

    def test_a_container_built_with_no_knowledge_base_constructs(self, tmp_path: Path):
        create_container(_settings(tmp_path))

    async def test_resolving_knowledge_base_settings_without_one_refuses(self, tmp_path: Path):
        container = create_container(_settings(tmp_path))

        with pytest.raises(LookupError, match="this process serves the registry and acts on no knowledge base, name one"):
            await container.get(KnowledgeBaseSettings)

    async def test_the_knowledge_base_argument_is_what_resolves(self, tmp_path: Path):
        knowledge_base = KnowledgeBaseSettings(path=_knowledge_base_root(tmp_path))
        container = create_container(_settings(tmp_path), knowledge_base=knowledge_base)

        assert await container.get(KnowledgeBaseSettings) is knowledge_base


class TestTheSectionsAreDerived:
    """Every model-typed settings field is registered under its own type, none by hand."""

    async def test_each_derived_section_resolves_to_itself(self, tmp_path: Path):
        settings = _settings(tmp_path)
        container = create_container(settings)

        assert await container.get(RetrievalSettings) is settings.retrieval
        assert await container.get(InfrastructureSettings) is settings.infrastructure
        assert await container.get(DatabaseSettings) is settings.infrastructure.database
        assert await container.get(TuningSettings) is settings.evals.tuning

    async def test_the_settings_file_registration_is_the_default_path(self, tmp_path: Path):
        container = create_container(_settings(tmp_path))

        assert (await container.get(SettingsFile)).path == default_settings_path()


class _StubEngine:
    """Inert stand-in for the admin `AsyncEngine` — never connects (wiring test only)."""


class _UnroutedCommand(Command):
    """A command no handler is registered for, dispatched only to prove the registry bus refuses it."""


class TestMessageBusRegistration:
    """`create_container` registers `MessageBus` from `bus_provider` when set, else a registry-scoped one."""

    async def test_a_knowledge_base_less_container_resolves_the_bus(self, tmp_path: Path):
        # A knowledge base-bound handler in the resolution chain would resolve `KnowledgeBaseSettings`, which
        # `_refuse_knowledge_base_settings` raises on: this succeeding is the proof none was touched.
        container = create_container(_settings(tmp_path))

        with container.override.injectable(target=AsyncEngine, new=_StubEngine()):
            bus = await container.get(MessageBus)

        assert isinstance(bus.outbox, PostgresOutbox)

    async def test_a_knowledge_base_less_bus_routes_no_command(self, tmp_path: Path):
        container = create_container(_settings(tmp_path))

        with container.override.injectable(target=AsyncEngine, new=_StubEngine()):
            bus = await container.get(MessageBus)

        with pytest.raises(KeyError, match="_UnroutedCommand"):
            await bus.handle(_UnroutedCommand())

    async def test_a_knowledge_base_less_bus_still_carries_the_qa_generation_subscription(self, tmp_path: Path):
        container = create_container(_settings(tmp_path))

        with container.override.injectable(target=AsyncEngine, new=_StubEngine()):
            bus = await container.get(MessageBus)

        assert [s.name for s in bus.subscriptions_for(CitingPagesChanged)] == ["qa_generation"]

    async def test_a_knowledge_base_less_bus_forwards_both_refresh_endings_to_the_harness(self, tmp_path: Path):
        container = create_container(_settings(tmp_path))

        with container.override.injectable(target=AsyncEngine, new=_StubEngine()):
            bus = await container.get(MessageBus)

        assert [s.name for s in bus.subscriptions_for(RefreshCompleted)] == ["harness_forward"]
        assert [s.name for s in bus.subscriptions_for(RefreshAbandoned)] == ["harness_forward"]
        assert list(bus.subscriptions_for(RefreshStarted)) == []

    async def test_a_knowledge_base_less_bus_forwards_each_index_to_the_harness(self, tmp_path: Path):
        container = create_container(_settings(tmp_path))

        with container.override.injectable(target=AsyncEngine, new=_StubEngine()):
            bus = await container.get(MessageBus)

        assert [s.name for s in bus.subscriptions_for(PagesLoaded)] == ["harness_index_forward"]


class TestEvalsDepsContainerWiring:
    """The real wireup graph: `create_container` resolves the evals slice's retrieval surface."""

    async def test_resolves_the_retrieval_handlers_bundle(self, tmp_path: Path):
        container = create_container(_settings(tmp_path), knowledge_base=KnowledgeBaseSettings(path=_knowledge_base_root(tmp_path)))

        with (
            container.override.injectable(target=ReaderSqlGateway, new=FakeSqlGateway()),
            container.override.injectable(target=BatchEmbedder, new=FakeEmbedding()),
        ):
            group = await container.get(RetrievalHandlers)

        assert isinstance(group, RetrievalHandlers)

    async def test_container_resolved_bundle_feeds_the_single_source_toolset(self, tmp_path: Path):
        # The toolset is deliberately NOT a registration: the tool loadout is a per-run experiment
        # axis, so the harness builds it per run. Here the group is resolved from the real graph and
        # run through the same `build_retrieval_toolset` the harness calls, proving the
        # container-wired handlers yield the four single-source tools the toolset exposes.
        container = create_container(_settings(tmp_path), knowledge_base=KnowledgeBaseSettings(path=_knowledge_base_root(tmp_path)))

        with (
            container.override.injectable(target=ReaderSqlGateway, new=FakeSqlGateway()),
            container.override.injectable(target=BatchEmbedder, new=FakeEmbedding()),
        ):
            group = await container.get(RetrievalHandlers)

        toolset: RetrievalToolset = build_retrieval_toolset(group, collection="workdocs")

        assert isinstance(toolset, FunctionToolset)
        assert {tool.name for tool in toolset.tools.values()} == {"search", "fetch", "graph_expand", "find_paths"}


class TestIngestionAdapterSliceOwnership:
    """The conversion and connector adapters resolve through `engram.sources.registry`, not sync's own wiring.

    `studio.knowledge_base.wiring.sync` composes the `DocumentConverterMap` / `ConnectorMap`
    strategy-registry bags (one entry per connector the knowledge base declares, keyed by name) and the
    `MirrorStore` port (from `studio.knowledge_base.wiring.mirror`) only; it carries no per-connector
    construction logic of its own — that lives in the `connectors` project, discovered by
    `engram.sources.registry` at the `engram.sources.connectors` / `engram.sources.converters`
    entry-point groups, never a sibling wiring module. The adapters are framework-pure (they store
    settings, touch no `zotero.sqlite` / Azure DI / `Sources/` at construction), so no override is
    needed.
    """

    async def test_resolves_the_document_converter_through_the_registry(self, tmp_path: Path):
        container = create_container(_settings(tmp_path), knowledge_base=KnowledgeBaseSettings(path=_knowledge_base_root(tmp_path)))

        converters = await container.get(DocumentConverterMap)

        assert isinstance(converters.by_key["zotero"], HybridConverter)

    async def test_resolves_the_source_provider_through_the_registry(self, tmp_path: Path):
        container = create_container(_settings(tmp_path), knowledge_base=KnowledgeBaseSettings(path=_knowledge_base_root(tmp_path)))

        connectors = await container.get(ConnectorMap)

        assert isinstance(connectors.by_key["zotero"], SqliteZoteroConnector)

    async def test_resolves_the_mirror_store_from_the_mirror_slice(self, tmp_path: Path):
        container = create_container(_settings(tmp_path), knowledge_base=KnowledgeBaseSettings(path=_knowledge_base_root(tmp_path)))

        store = await container.get(MirrorStore)

        assert isinstance(store, ObsidianMirrorStore)

    def test_sync_wiring_composes_ports_only(self):
        # `studio.knowledge_base.wiring.sync` composes ports only: its only provider is the handler over the three ports.
        from studio.knowledge_base.wiring import sync as sync_wiring

        assert not hasattr(sync_wiring, "document_converters_provider")
        assert not hasattr(sync_wiring, "connectors_provider")
        assert not hasattr(sync_wiring, "mirror_store_provider")
        assert hasattr(sync_wiring, "sync_handler_provider")


class TestEvalsSettingsRegistration:
    """`create_container` registers `settings.evals` as an injectable `EvalsSettings` instance."""

    async def test_resolves_the_evals_settings_sub_instance(self, tmp_path: Path):
        settings = _settings(tmp_path)
        container = create_container(settings)

        resolved = await container.get(EvalsSettings)

        assert resolved is settings.evals


class TestLogfireSettingsRegistration:
    """`create_container` registers `settings.infrastructure.logfire` as an injectable `LogfireSettings`."""

    async def test_resolves_the_logfire_resource_sub_instance(self, tmp_path: Path):
        settings = _settings(tmp_path)
        container = create_container(settings)

        resolved = await container.get(LogfireSettings)

        assert resolved is settings.infrastructure.logfire
