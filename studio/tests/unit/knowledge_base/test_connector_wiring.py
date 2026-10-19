r"""Unit specs for the `Connector` registry (`studio.knowledge_base.wiring.sources`).

Concerns, pinned directly (no database, no network):

* `source_resources_provider` assembles the one set of resources every connector's and every scanner's
  `build` takes: a `document_intelligence` with no endpoint crosses as `None`, the knowledge base's declared source
  settings cross whole and unread so each connector validates its own block, and the knowledge base's own
  declared connector names ride along so nothing has to read the schema off disk to classify a URL;
* `connectors_provider` builds every connector the resolved knowledge base schema's `sources.connectors`
  declares, returned as a `ConnectorMap` keyed by name — the `"zotero"` entry builds a
  `SqliteZoteroConnector`, the `"youtube"` entry builds a `YouTubeConnector` over the knowledge base's own
  `_meta/source-queue.yaml` queue plus its `_meta/youtube-cache/` transcript cache, the `"intake"` entry builds
  a `SourceQueueConnector` over the knowledge base's own `_meta/source-queue.yaml`. A `document_intelligence` with no
  endpoint crosses into `SourceResources` as `None` rather than an invented bare instance, and an
  `xai` settings block that is absent or carries no `api_key` reads as no connection, which narrows
  the paper resolver to the arXiv HTML route alone and drops the `POST` source type from the resolver
  table, a configured pair wires the Azure DI fallback and the `PostResolver`; an uninstalled or
  undeclared name raises a
  `ValueError` naming the knowledge base and everything `engram.sources.registry` actually has installed;
* the real wireup graph (`create_container`) resolves `ConnectorMap` to the entries the
  knowledge base's own `knowledge-base-schema.json` declares.
"""

from __future__ import annotations

import json

from typing import TYPE_CHECKING

import pytest

from engram.kb.contracts import KnowledgeBaseSchema
from engram.sources.contracts import SourceType
from engram_connectors.conversion.contracts import ArxivHtmlConverter, HybridConverter
from engram_connectors.source_queue.contracts import ArxivResolver, PaperResolver, PdfResolver, PostResolver, SourceQueueConnector
from engram_connectors.youtube.contracts import YouTubeConnector
from engram_connectors.zotero.contracts import SqliteZoteroConnector

from studio.knowledge_base.container import create_container
from studio.knowledge_base.settings import ApplicationSettings, DocumentIntelligenceSettings, KnowledgeBaseSettings, SourceSettings
from studio.knowledge_base.wiring.sources import ConnectorMap

if TYPE_CHECKING:
    from pathlib import Path

    from engram.config import SourceResources
    from pydantic import JsonValue

_PAGE_TYPES = [{"page_type": "concept", "id_prefix": "concept", "canonical_sections": ["overview"], "claims_enabled": False}]


def _spec_json(connector: str, *, slug: str = "test-knowledge-base") -> str:
    return json.dumps(
        {
            "version": "0.1",
            "slug": slug,
            "purpose": "Test",
            "page_types": _PAGE_TYPES,
            "sources": {
                "enabled": True,
                "connectors": [connector],
                "key_field": "zotero_key",
                "digest_link_field": "digest_of",
                "hash_field": "source_hash",
                "digest_types": {"paper": "concept"},
            },
        }
    )


def _multi_spec_json(connectors: list[str], *, slug: str = "test-knowledge-base") -> str:
    return json.dumps(
        {
            "version": "0.1",
            "slug": slug,
            "purpose": "Test",
            "page_types": _PAGE_TYPES,
            "sources": {
                "enabled": True,
                "connectors": connectors,
                "key_field": "zotero_key",
                "digest_link_field": "digest_of",
                "hash_field": "source_hash",
                "digest_types": {"paper": "concept"},
            },
        }
    )


def _spec(connector: str, *, slug: str = "test-knowledge-base") -> KnowledgeBaseSchema:
    return KnowledgeBaseSchema.from_json(_spec_json(connector, slug=slug))


def _knowledge_base_settings(tmp_path: Path) -> KnowledgeBaseSettings:
    (tmp_path / "_meta").mkdir(parents=True, exist_ok=True)
    (tmp_path / "knowledge-base-schema.json").write_text("{}", encoding="utf-8")
    return KnowledgeBaseSettings(path=tmp_path)


def _sources(knowledge_base_root: Path, *, xai_key: str = "") -> SourceSettings:
    blocks: dict[str, dict[str, JsonValue]] = {"zotero": {"data_dir": str(knowledge_base_root)}}
    if xai_key:
        blocks["xai"] = {"api_key": xai_key}
    return SourceSettings(blocks)


def _application_settings(knowledge_base_root: Path, *, spec_json: str | None = None, xai_key: str = "") -> ApplicationSettings:
    (knowledge_base_root / "_meta").mkdir(parents=True, exist_ok=True)
    (knowledge_base_root / "knowledge-base-schema.json").write_text("{}", encoding="utf-8")
    if spec_json is not None:
        (knowledge_base_root / "knowledge-base-schema.json").write_text(spec_json, encoding="utf-8")
    return ApplicationSettings(sources=_sources(knowledge_base_root, xai_key=xai_key))


def _resources_for(
    spec: KnowledgeBaseSchema,
    knowledge_base: KnowledgeBaseSettings,
    *,
    sources: SourceSettings | None = None,
    document_intelligence: DocumentIntelligenceSettings | None = None,
    xai_key: str = "",
) -> SourceResources:
    """The resources through the real factory, so every connector test builds from the same rule the app uses."""
    from studio.knowledge_base.wiring.sources import source_resources_provider

    return source_resources_provider(
        spec,
        knowledge_base,
        document_intelligence if document_intelligence is not None else DocumentIntelligenceSettings(),
        sources if sources is not None else _sources(knowledge_base.path, xai_key=xai_key),
    )


class TestConnectorsProvider:
    def test_a_zotero_schema_resolves_the_zotero_repository(self, tmp_path: Path):
        from studio.knowledge_base.wiring.sources import connectors_provider

        knowledge_base = _knowledge_base_settings(tmp_path)
        spec = _spec("zotero")

        connectors = connectors_provider(spec, _resources_for(spec, knowledge_base))

        assert set(connectors.by_key) == {"zotero"}
        connector = connectors.by_key["zotero"]
        assert isinstance(connector, SqliteZoteroConnector)
        assert connector.name == "zotero"

    def test_a_youtube_schema_resolves_the_youtube_connector(self, tmp_path: Path):
        from studio.knowledge_base.wiring.sources import connectors_provider

        knowledge_base = _knowledge_base_settings(tmp_path)
        spec = _spec("youtube")

        connectors = connectors_provider(spec, _resources_for(spec, knowledge_base))

        connector = connectors.by_key["youtube"]
        assert isinstance(connector, YouTubeConnector)
        assert connector.name == "youtube"

    def test_two_declared_connectors_resolve_to_both_by_name(self, tmp_path: Path):
        from studio.knowledge_base.wiring.sources import connectors_provider

        spec = KnowledgeBaseSchema.from_json(_multi_spec_json(["zotero", "intake"]))
        knowledge_base = _knowledge_base_settings(tmp_path)
        connectors = connectors_provider(spec, _resources_for(spec, knowledge_base))

        assert set(connectors.by_key) == {"zotero", "intake"}
        assert isinstance(connectors.by_key["zotero"], SqliteZoteroConnector)
        assert isinstance(connectors.by_key["intake"], SourceQueueConnector)

    def test_the_map_holds_the_declared_connectors_in_declared_order(self, tmp_path: Path):
        from studio.knowledge_base.wiring.sources import connectors_provider

        schema = KnowledgeBaseSchema.from_json(_multi_spec_json(["zotero", "intake"]))
        knowledge_base = _knowledge_base_settings(tmp_path)

        connectors = connectors_provider(schema, _resources_for(schema, knowledge_base))

        assert list(connectors.by_key) == list(schema.sources.connectors)

    def test_an_uninstalled_connector_names_the_knowledge_base_and_what_is_installed(self, tmp_path: Path):
        from studio.knowledge_base.wiring.sources import connectors_provider

        knowledge_base = _knowledge_base_settings(tmp_path)
        spec = _spec("vimeo", slug="test-knowledge-base")
        resources = _resources_for(spec, knowledge_base)

        with pytest.raises(ValueError, match="test-knowledge-base") as excinfo:
            connectors_provider(spec, resources)

        message = str(excinfo.value)
        assert "vimeo" in message
        assert "youtube" in message
        assert "zotero" in message
        assert "intake" in message

    def test_intake_has_no_post_resolver_without_an_xai_key(self, tmp_path: Path):
        from studio.knowledge_base.wiring.sources import connectors_provider

        knowledge_base = _knowledge_base_settings(tmp_path)
        spec = _spec("intake")

        connectors = connectors_provider(spec, _resources_for(spec, knowledge_base))

        intake = connectors.by_key["intake"]
        assert isinstance(intake, SourceQueueConnector)
        assert SourceType.POST not in intake._resolvers

    def test_intake_registers_the_post_resolver_when_an_xai_key_is_configured(self, tmp_path: Path):
        from studio.knowledge_base.wiring.sources import connectors_provider

        knowledge_base = _knowledge_base_settings(tmp_path)
        spec = _spec("intake")

        connectors = connectors_provider(spec, _resources_for(spec, knowledge_base, xai_key="xai-secret"))

        intake = connectors.by_key["intake"]
        assert isinstance(intake, SourceQueueConnector)
        assert isinstance(intake._resolvers[SourceType.POST], PostResolver)

    def test_intake_paper_resolver_is_html_only_without_a_doc_intel_endpoint(self, tmp_path: Path):
        from studio.knowledge_base.wiring.sources import connectors_provider

        knowledge_base = _knowledge_base_settings(tmp_path)
        spec = _spec("intake")

        connectors = connectors_provider(spec, _resources_for(spec, knowledge_base))

        intake = connectors.by_key["intake"]
        assert isinstance(intake, SourceQueueConnector)
        paper = intake._resolvers[SourceType.PAPER]
        assert isinstance(paper, PaperResolver)
        assert isinstance(paper._arxiv, ArxivResolver)
        assert isinstance(paper._arxiv._converter, ArxivHtmlConverter)
        assert isinstance(paper._pdf, PdfResolver)
        assert isinstance(paper._pdf._converter, ArxivHtmlConverter)

    def test_intake_paper_resolver_falls_back_to_azure_when_a_doc_intel_endpoint_is_configured(self, tmp_path: Path):
        from studio.knowledge_base.wiring.sources import connectors_provider

        knowledge_base = _knowledge_base_settings(tmp_path)
        spec = _spec("intake")

        connectors = connectors_provider(
            spec, _resources_for(spec, knowledge_base, document_intelligence=DocumentIntelligenceSettings(endpoint="https://di.example.org"))
        )

        intake = connectors.by_key["intake"]
        assert isinstance(intake, SourceQueueConnector)
        paper = intake._resolvers[SourceType.PAPER]
        assert isinstance(paper, PaperResolver)
        assert isinstance(paper._arxiv, ArxivResolver)
        assert isinstance(paper._arxiv._converter, HybridConverter)
        assert isinstance(paper._pdf, PdfResolver)
        assert isinstance(paper._pdf._converter, HybridConverter)
        assert paper._arxiv._converter is paper._pdf._converter


class TestSourceResourcesProvider:
    """The one place the app decides what counts as a configured resource, and what a connector's `build` sees."""

    def test_every_declared_settings_block_crosses_unread(self, tmp_path: Path):
        """The app carries the blocks a knowledge base declares without reading a key of one; each connector validates its own."""
        from studio.knowledge_base.wiring.sources import source_resources_provider

        spec = _spec("zotero")
        sources = SourceSettings({"zotero": {"collection": "Reading"}, "xai": {"api_key": "xai-secret"}})

        resources = source_resources_provider(spec, _knowledge_base_settings(tmp_path), DocumentIntelligenceSettings(), sources)

        assert resources.settings == {"zotero": {"collection": "Reading"}, "xai": {"api_key": "xai-secret"}}

    def test_no_declared_blocks_cross_as_an_empty_mapping(self, tmp_path: Path):
        from studio.knowledge_base.wiring.sources import source_resources_provider

        spec = _spec("zotero")

        resources = source_resources_provider(spec, _knowledge_base_settings(tmp_path), DocumentIntelligenceSettings(), SourceSettings({}))

        assert resources.settings == {}

    def test_an_unconfigured_doc_intel_crosses_as_none(self, tmp_path: Path):
        from studio.knowledge_base.wiring.sources import source_resources_provider

        spec = _spec("intake")

        resources = source_resources_provider(spec, _knowledge_base_settings(tmp_path), DocumentIntelligenceSettings(), SourceSettings({}))

        assert resources.document_intelligence is None

    def test_a_configured_doc_intel_crosses_through(self, tmp_path: Path):
        from studio.knowledge_base.wiring.sources import source_resources_provider

        spec = _spec("intake")
        document_intelligence = DocumentIntelligenceSettings(endpoint="https://di.example.org")

        resources = source_resources_provider(spec, _knowledge_base_settings(tmp_path), document_intelligence, SourceSettings({}))

        assert resources.document_intelligence == document_intelligence

    def test_the_knowledge_base_declared_connector_names_ride_along_in_its_own_order(self, tmp_path: Path):
        from studio.knowledge_base.wiring.sources import source_resources_provider

        spec = KnowledgeBaseSchema.from_json(_multi_spec_json(["zotero", "intake"]))

        resources = source_resources_provider(spec, _knowledge_base_settings(tmp_path), DocumentIntelligenceSettings(), SourceSettings({}))

        assert resources.connectors == ("zotero", "intake")


class TestConnectorContainerWiring:
    """The real wireup graph: `create_container` resolves `ConnectorMap` by the knowledge base's own schema."""

    async def test_a_youtube_knowledge_base_resolves_the_youtube_provider(self, tmp_path: Path):
        settings = _application_settings(tmp_path / "knowledge_base", spec_json=_spec_json("youtube"))
        container = create_container(settings, knowledge_base=KnowledgeBaseSettings(path=tmp_path / "knowledge_base"))

        connectors = await container.get(ConnectorMap)

        assert isinstance(connectors.by_key["youtube"], YouTubeConnector)

    async def test_a_zotero_knowledge_base_still_resolves_the_zotero_repository(self, tmp_path: Path):
        settings = _application_settings(tmp_path / "knowledge_base", spec_json=_spec_json("zotero"))
        container = create_container(settings, knowledge_base=KnowledgeBaseSettings(path=tmp_path / "knowledge_base"))

        connectors = await container.get(ConnectorMap)

        assert isinstance(connectors.by_key["zotero"], SqliteZoteroConnector)

    async def test_source_provider_map_is_a_singleton(self, tmp_path: Path):
        settings = _application_settings(tmp_path / "knowledge_base", spec_json=_spec_json("youtube"))
        container = create_container(settings, knowledge_base=KnowledgeBaseSettings(path=tmp_path / "knowledge_base"))

        first = await container.get(ConnectorMap)
        second = await container.get(ConnectorMap)

        assert first is second

    async def test_an_intake_knowledge_base_registers_the_post_resolver_only_with_an_xai_key(self, tmp_path: Path):
        settings = _application_settings(tmp_path / "knowledge_base", spec_json=_spec_json("intake"), xai_key="xai-secret")
        container = create_container(settings, knowledge_base=KnowledgeBaseSettings(path=tmp_path / "knowledge_base"))

        connectors = await container.get(ConnectorMap)

        intake = connectors.by_key["intake"]
        assert isinstance(intake, SourceQueueConnector)
        assert isinstance(intake._resolvers[SourceType.POST], PostResolver)
