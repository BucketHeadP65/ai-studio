"""Composition-root contract for the sync slice (`src/studio/knowledge_base/container.py` + `studio/knowledge_base/wiring/sync.py`).

The sync slice's `sync_handler_provider` (in `studio/knowledge_base/wiring/sync.py`) is a plain handler
factory, merging one `SyncMirrorsHandler` per connector a knowledge base declares into one
`MultiConnectorSync`; the composition root's single `bus_provider` routes `SyncMirrors` to it inside
the declarative bus assembly built at construction. Pinned here both directly (the provider function
over fakes) and through the real wireup graph: a `create_container` container's unqualified
`MessageBus` routes **both** commands — the contract the `memory sync` CLI and the jobs API
(`POST /api/jobs/sync`) dispatch against. The engine and embedder are overridden with inert fakes so
no Postgres or Azure is touched.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from core.ai.embedding import BatchEmbedder
from core.cqrs.contracts import MessageBus
from engram.indexing.contracts import IndexKnowledgeBase
from engram.sources.contracts import MultiConnectorSync, Resolved, SourceQueue, SourceType, SyncMirrors, SyncReport, claimants_for
from engram.testing.factories.source_item import SourceItemFactory
from engram.testing.fakes.connector import FakeConnector
from engram.testing.fakes.conversion import FakeDocumentConverter
from engram.testing.fakes.embedding import FakeEmbedding
from engram.testing.fakes.mirror import FakeMirrorStore
from engram.testing.specs import research_spec
from engram_connectors.conversion.contracts import PassthroughConverter
from engram_connectors.source_queue.contracts import SourceQueueConnector
from engram_connectors.testing.fakes.source_queue import FakeResolver
from sqlalchemy.ext.asyncio import AsyncEngine

from studio.knowledge_base.container import create_container
from studio.knowledge_base.settings import ApplicationSettings, KnowledgeBaseSettings, SourceSettings
from studio.knowledge_base.wiring.conversion import DocumentConverterMap
from studio.knowledge_base.wiring.sources import ConnectorMap
from studio.knowledge_base.wiring.sync import sync_handler_provider
from tests.support.http_app import write_settings_json

if TYPE_CHECKING:
    from pathlib import Path


def _knowledge_base_root(tmp_path: Path) -> Path:
    """A research knowledge base on disk, the shape `KnowledgeBaseSettings` accepts."""
    knowledge_base_root = tmp_path / "knowledge_base"
    (knowledge_base_root / "_meta").mkdir(parents=True)
    (knowledge_base_root / "knowledge-base-schema.json").write_text(research_spec().model_dump_json(), encoding="utf-8")
    return knowledge_base_root


def _settings(tmp_path: Path) -> ApplicationSettings:
    """A settings tree read from a real file under `tmp_path` — no env, no network."""
    return ApplicationSettings(
        json_file=write_settings_json(tmp_path / "application_settings.json"),
        sources=SourceSettings({"zotero": {"data_dir": str(tmp_path)}}),
    )


def _knowledge_base(tmp_path: Path) -> KnowledgeBaseSettings:
    """The knowledge base the sync slice acts on."""
    return KnowledgeBaseSettings(path=_knowledge_base_root(tmp_path))


class TestSyncHandlerProvider:
    """The provider function itself, over fakes — a plain handler factory, no bus side effect."""

    async def test_returns_the_handler_over_its_collaborators(self, tmp_path: Path):
        connectors = ConnectorMap({"zotero": FakeConnector(name="zotero")})
        converters = DocumentConverterMap({"zotero": FakeDocumentConverter()})

        handler = sync_handler_provider(connectors, converters, FakeMirrorStore(), schema=research_spec(), knowledge_base=_knowledge_base(tmp_path))

        assert isinstance(handler, MultiConnectorSync)
        report = await handler(SyncMirrors(dry_run=True))
        assert isinstance(report, SyncReport)
        assert report.dry_run is True


class TestMultiProviderSync:
    """`sync_handler_provider` runs one `SyncMirrorsHandler` per declared provider and merges the reports."""

    async def test_syncing_two_providers_merges_their_reports(self, tmp_path: Path):
        zotero_pdf = tmp_path / "ITEM0001.pdf"
        zotero_pdf.write_bytes(b"%PDF data")
        zotero_item = SourceItemFactory.build(provider_key="ITEM0001", source_id="ITEM0001", version=1, pdf_path=zotero_pdf)
        zotero = FakeConnector(items=[zotero_item], name="zotero")

        ledger = SourceQueue(tmp_path / "source-queue.yaml", connectors=claimants_for(("intake",)))
        resolved = Resolved(
            title="Agents that plan", markdown="# Agents\n\nbody", url="https://blog.example.org/agents", converter="article/trafilatura"
        )
        intake = SourceQueueConnector(ledger, {SourceType.ARTICLE: FakeResolver(results={"url": resolved})}, tmp_path / "cache")
        ledger.add("https://blog.example.org/agents")

        connectors = ConnectorMap({"zotero": zotero, "intake": intake})
        converters = DocumentConverterMap({"zotero": FakeDocumentConverter(), "intake": PassthroughConverter("intake")})
        store = FakeMirrorStore()

        handler = sync_handler_provider(connectors, converters, store, schema=research_spec(), knowledge_base=_knowledge_base(tmp_path))
        report = await handler(SyncMirrors())

        assert report.created == 2
        assert report.errors == ()
        assert {frontmatter.provider for frontmatter in store.written} == {"zotero", "intake"}


class TestContainerWiring:
    """The real wireup graph: `create_container` wires the sync slice end to end."""

    async def test_resolving_the_handler_routes_both_commands_on_the_shared_bus(self, tmp_path: Path):
        container = create_container(_settings(tmp_path), knowledge_base=_knowledge_base(tmp_path))

        with (
            container.override.injectable(target=AsyncEngine, new=_StubEngine()),
            container.override.injectable(target=BatchEmbedder, new=FakeEmbedding()),
        ):
            handler = await container.get(MultiConnectorSync)
            bus = await container.get(MessageBus)

        assert isinstance(handler, MultiConnectorSync)
        assert bus._command_handlers[SyncMirrors] is handler
        assert IndexKnowledgeBase in bus._command_handlers

    async def test_handler_is_a_singleton_so_the_route_registers_once(self, tmp_path: Path):
        container = create_container(_settings(tmp_path), knowledge_base=_knowledge_base(tmp_path))

        with (
            container.override.injectable(target=AsyncEngine, new=_StubEngine()),
            container.override.injectable(target=BatchEmbedder, new=FakeEmbedding()),
        ):
            first = await container.get(MultiConnectorSync)
            second = await container.get(MultiConnectorSync)

        assert first is second


class _StubEngine:
    """Inert stand-in for the admin `AsyncEngine` — never connects (wiring test only)."""
