r"""Unit specs for the `DocumentConverter` registry (`studio.knowledge_base.wiring.conversion`).

Mirrors `test_source_provider_wiring.py`'s concerns for the sibling registry:

* `document_converters_provider` builds every converter the resolved knowledge base spec's
  `sources.connectors` declares, returned as a `DocumentConverterMap` keyed by name — the `"zotero"`
  entry builds the same `HybridConverter(ArxivHtmlConverter(), AzureDocumentConverter(...))` the
  pre-registry wiring built (byte-identical), the `"youtube"` entry builds a
  `PassthroughConverter` labelled `"youtube-transcript"`, the `"intake"` entry builds a `PassthroughConverter` labelled
  `"intake"`; an undeclared or uninstalled name raises a `ValueError` naming the knowledge base and
  everything `engram.sources.registry` actually has installed;
* the real wireup graph (`create_container`) resolves `DocumentConverterMap` to the entries the
  knowledge base's own `knowledge-base-schema.json` declares.
"""

from __future__ import annotations

import json

from typing import TYPE_CHECKING

import pytest

from engram.kb.contracts import KnowledgeBaseSchema
from engram.sources.contracts import available_connectors, available_converters
from engram_connectors.conversion.contracts import HybridConverter, PassthroughConverter

from studio.knowledge_base.container import create_container
from studio.knowledge_base.settings import ApplicationSettings, DocumentIntelligenceSettings, KnowledgeBaseSettings, SourceSettings
from studio.knowledge_base.wiring.conversion import DocumentConverterMap

if TYPE_CHECKING:
    from pathlib import Path

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


def _spec(connector: str, *, slug: str = "test-knowledge-base") -> KnowledgeBaseSchema:
    return KnowledgeBaseSchema.from_json(_spec_json(connector, slug=slug))


def _application_settings(knowledge_base_root: Path, *, spec_json: str | None = None) -> ApplicationSettings:
    (knowledge_base_root / "_meta").mkdir(parents=True, exist_ok=True)
    if spec_json is not None:
        (knowledge_base_root / "knowledge-base-schema.json").write_text(spec_json, encoding="utf-8")
    return ApplicationSettings(sources=SourceSettings({"zotero": {"data_dir": str(knowledge_base_root)}}))


class TestRegistryCongruence:
    """The two entry-point groups (`engram.sources.connectors`, `engram.sources.converters`) must declare the same names.

    A knowledge base's `sources.connectors` dispatches through both independently
    (`connectors_provider`, `document_converters_provider`); a provider registered under one
    group but not the other would resolve one port and blow up on the sibling port for the exact
    same spec, loud only at first use rather than at a glance. Pinned so a future connector addition
    that forgets one group's entry point fails here instead.
    """

    def test_the_two_entry_point_groups_declare_the_same_names(self) -> None:
        assert set(available_converters()) == set(available_connectors())


class TestDocumentConvertersProvider:
    def test_zotero_spec_resolves_the_hybrid_converter(self):
        from studio.knowledge_base.wiring.conversion import document_converters_provider

        converters = document_converters_provider(_spec("zotero"), DocumentIntelligenceSettings())

        assert isinstance(converters.by_key["zotero"], HybridConverter)

    def test_youtube_spec_resolves_the_youtube_transcript_converter(self):
        from studio.knowledge_base.wiring.conversion import document_converters_provider

        converters = document_converters_provider(_spec("youtube"), DocumentIntelligenceSettings())

        assert isinstance(converters.by_key["youtube"], PassthroughConverter)

    async def test_intake_spec_resolves_the_passthrough_converter_labelled_intake(self, tmp_path: Path):
        from studio.knowledge_base.wiring.conversion import document_converters_provider

        converters = document_converters_provider(_spec("intake"), DocumentIntelligenceSettings())

        converter = converters.by_key["intake"]
        assert isinstance(converter, PassthroughConverter)
        source = tmp_path / "note.md"
        source.write_text("# Rendered by the connector\n", encoding="utf-8")
        result = await converter.to_markdown(source)
        assert result.converter == "intake"

    def test_an_uninstalled_provider_names_the_knowledge_base_and_what_is_installed(self):
        from studio.knowledge_base.wiring.conversion import document_converters_provider

        with pytest.raises(ValueError, match="test-knowledge-base") as excinfo:
            document_converters_provider(_spec("vimeo", slug="test-knowledge-base"), DocumentIntelligenceSettings())

        message = str(excinfo.value)
        assert "vimeo" in message
        assert "youtube" in message
        assert "zotero" in message
        assert "intake" in message


class TestDocumentConverterContainerWiring:
    """The real wireup graph: `create_container` resolves `DocumentConverterMap` by the knowledge base's own spec."""

    async def test_a_youtube_knowledge_base_resolves_the_youtube_transcript_converter(self, tmp_path: Path):
        settings = _application_settings(tmp_path / "knowledge_base", spec_json=_spec_json("youtube"))
        container = create_container(settings, knowledge_base=KnowledgeBaseSettings(path=tmp_path / "knowledge_base"))

        converters = await container.get(DocumentConverterMap)

        assert isinstance(converters.by_key["youtube"], PassthroughConverter)

    async def test_a_zotero_knowledge_base_still_resolves_the_hybrid_converter(self, tmp_path: Path):
        settings = _application_settings(tmp_path / "knowledge_base", spec_json=_spec_json("zotero"))
        container = create_container(settings, knowledge_base=KnowledgeBaseSettings(path=tmp_path / "knowledge_base"))

        converters = await container.get(DocumentConverterMap)

        assert isinstance(converters.by_key["zotero"], HybridConverter)
