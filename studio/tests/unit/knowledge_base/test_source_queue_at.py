"""A knowledge base's source queue is claimed by its connectors only while its sources are on."""

from __future__ import annotations

from typing import TYPE_CHECKING

from engram.kb.contracts import CanonicalSection, KnowledgeBaseSchema, PageTypeSpec, SourcesSpec
from engram.kb.domain.schema import RetrievalSpec

from studio.knowledge_base.source_queue import source_queue_at

if TYPE_CHECKING:
    from pathlib import Path

WATCH_URL = "https://www.youtube.com/watch?v=abc123XYZ_-"


def _schema(*, enabled: bool) -> KnowledgeBaseSchema:
    return KnowledgeBaseSchema(
        slug="inbox",
        page_types=(PageTypeSpec(page_type="note", id_prefix="note.", canonical_sections=(CanonicalSection(slug="overview"),)),),
        sources=SourcesSpec(
            enabled=enabled,
            connectors=("youtube", "intake"),
            key_field="zotero_key",
            digest_link_field="digest_of",
            hash_field="source_hash",
            digest_types={"paper": "note"},
        ),
        retrieval=RetrievalSpec(collection="inbox"),
    )


def test_a_knowledge_base_with_sources_on_keys_a_url_by_its_connector(tmp_path: Path) -> None:
    entry = source_queue_at(tmp_path, _schema(enabled=True)).add(WATCH_URL)

    assert entry.key == "yt-abc123XYZ_-"


def test_a_knowledge_base_with_sources_off_keys_every_url_as_an_article(tmp_path: Path) -> None:
    entry = source_queue_at(tmp_path, _schema(enabled=False)).add(WATCH_URL)

    assert entry.key.startswith("url-")


def test_a_knowledge_base_whose_schema_cannot_be_read_has_no_claimants(tmp_path: Path) -> None:
    entry = source_queue_at(tmp_path, None).add(WATCH_URL)

    assert entry.key.startswith("url-")
