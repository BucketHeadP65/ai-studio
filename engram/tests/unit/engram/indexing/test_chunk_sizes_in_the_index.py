"""Unit tests that the index cuts every mirror by the chunk sizes it was given, on the full and the incremental path."""

from __future__ import annotations

from dataclasses import replace
from typing import TYPE_CHECKING

from engram.indexing.contracts import (
    INDEXER_VERSION,
    ChunkSizes,
    FilesystemKnowledgeBaseReader,
    IndexKnowledgeBase,
    IndexKnowledgeBaseHandler,
    ManifestEntry,
    MirrorDoc,
    StoredFingerprint,
    indexer_version,
)
from engram.indexing.domain.manifest import FileType
from engram.indexing.domain.mirror_chunks import chunk_mirror
from engram.indexing.service.row_builder import RowBuilder
from engram.kb.contracts import SectionSummarySpec
from engram.testing.fakes.embedding import FakeEmbedding
from engram.testing.fakes.uow import FakeKnowledgeBaseUnitOfWork, FakeValidationReportSink
from engram.testing.knowledge_base_builder import KnowledgeBaseBuilder
from engram.testing.specs import research_spec

if TYPE_CHECKING:
    from pathlib import Path

    from core.ai.retrieval.contracts import Row

_SIZES = ChunkSizes(target_tokens=40, max_tokens=80)


def _mirror_text(source_key: str) -> str:
    """A mirror with no heading whose paragraphs pass the small sizes together."""
    paragraphs = "\n\n".join(f"Paragraph {index} of {source_key} records the tide at the harbour wall." for index in range(12))
    return f"---\nsource_key: {source_key}\nprovider: example-library\nsource_hash: h-{source_key}\n---\n{paragraphs}\n"


def _handler(root: Path, uow: FakeKnowledgeBaseUnitOfWork, sizes: ChunkSizes = _SIZES) -> IndexKnowledgeBaseHandler:
    return IndexKnowledgeBaseHandler(
        FilesystemKnowledgeBaseReader(root), FakeEmbedding(), lambda: uow, FakeValidationReportSink(), spec=research_spec(), chunk_sizes=sizes
    )


def _chunk_rows(uow: FakeKnowledgeBaseUnitOfWork, source_id: str) -> list[Row]:
    return [row for batch in uow.sources.chunks for row in batch if row["source_id"] == source_id]


def _seed_stored_state(uow: FakeKnowledgeBaseUnitOfWork) -> None:
    """Make the last run's written manifest and fingerprint the ones the next run reads back."""
    stored: dict[str, ManifestEntry] = {}
    for batch in uow.manifest.manifest_upserts:
        for entry in batch:
            source_key = entry["source_key"]
            page_id = entry["page_id"]
            stored[str(entry["path"])] = ManifestEntry(
                path=str(entry["path"]),
                kind=FileType(str(entry["kind"])),
                content_hash=str(entry["content_hash"]),
                identity_hash=str(entry["identity_hash"]),
                page_id=str(page_id) if page_id is not None else None,
                source_key=str(source_key) if source_key is not None else None,
            )
    uow.manifest.stored = stored
    written = uow.fingerprint.fingerprint
    assert written is not None
    uow.fingerprint.stored = StoredFingerprint(**{key: str(value) for key, value in written.items() if key not in {"spec", "collection"}})


def test_the_row_builder_cuts_a_mirror_by_its_chunk_sizes() -> None:
    spec = research_spec()
    builder = RowBuilder(
        "research",
        section_summary_spec=SectionSummarySpec(),
        source_key_field=spec.sources.key_field,
        source_hash_field=spec.sources.hash_field,
        date_field="created",
        chunk_sizes=_SIZES,
    )
    text = _mirror_text("tide-log")

    builder.add_source(MirrorDoc.from_text("Sources/tide-log.md", text, "research"))

    expected = list(chunk_mirror(text, _SIZES))
    assert len(expected) > 1
    assert [row["body"] for row in builder.chunks] == [chunk.body for chunk in expected]
    assert len(builder.embed_inputs) == len(expected)


async def test_a_full_index_cuts_every_mirror_by_the_handlers_chunk_sizes(tmp_path: Path) -> None:
    KnowledgeBaseBuilder(tmp_path).build_minimal()
    text = _mirror_text("tide-log")
    KnowledgeBaseBuilder(tmp_path).add_source_mirror("tide-log", text)
    uow = FakeKnowledgeBaseUnitOfWork()

    report = await _handler(tmp_path, uow).handle(IndexKnowledgeBase(collection="research", embed=False))

    assert report.mode == "full"
    assert [row["body"] for row in _chunk_rows(uow, "tide-log")] == [chunk.body for chunk in chunk_mirror(text, _SIZES)]


async def test_an_incremental_index_cuts_a_new_mirror_by_the_handlers_chunk_sizes(tmp_path: Path) -> None:
    KnowledgeBaseBuilder(tmp_path).build_minimal()
    KnowledgeBaseBuilder(tmp_path).add_source_mirror("tide-log", _mirror_text("tide-log"))
    uow = FakeKnowledgeBaseUnitOfWork()
    handler = _handler(tmp_path, uow)
    await handler.handle(IndexKnowledgeBase(collection="research", embed=False))
    _seed_stored_state(uow)
    handler.set_clean_page_view(list)
    text = _mirror_text("wind-log")
    KnowledgeBaseBuilder(tmp_path).add_source_mirror("wind-log", text)

    report = await handler.handle(IndexKnowledgeBase(collection="research", embed=False))

    assert report.mode == "incremental"
    assert [row["body"] for row in _chunk_rows(uow, "wind-log")] == [chunk.body for chunk in chunk_mirror(text, _SIZES)]


async def test_an_index_written_before_mirrors_were_cut_to_chunk_sizes_runs_full(tmp_path: Path) -> None:
    KnowledgeBaseBuilder(tmp_path).build_minimal()
    KnowledgeBaseBuilder(tmp_path).add_source_mirror("tide-log", _mirror_text("tide-log"))
    uow = FakeKnowledgeBaseUnitOfWork()
    handler = _handler(tmp_path, uow)
    await handler.handle(IndexKnowledgeBase(collection="research", embed=False))
    _seed_stored_state(uow)
    handler.set_clean_page_view(list)
    stored = uow.fingerprint.stored
    assert isinstance(stored, StoredFingerprint)
    uow.fingerprint.stored = replace(stored, indexer_version="5")

    report = await handler.handle(IndexKnowledgeBase(collection="research", embed=False))

    assert report.mode == "full"


async def test_a_chunk_size_change_makes_the_next_plain_index_a_full_one(tmp_path: Path) -> None:
    KnowledgeBaseBuilder(tmp_path).build_minimal()
    text = _mirror_text("tide-log")
    KnowledgeBaseBuilder(tmp_path).add_source_mirror("tide-log", text)
    uow = FakeKnowledgeBaseUnitOfWork()
    await _handler(tmp_path, uow).handle(IndexKnowledgeBase(collection="research", embed=False))
    _seed_stored_state(uow)
    smaller = ChunkSizes(target_tokens=30, max_tokens=60)
    handler = _handler(tmp_path, uow, smaller)
    handler.set_clean_page_view(list)

    report = await handler.handle(IndexKnowledgeBase(collection="research", embed=False))

    assert report.mode == "full"
    assert [row["body"] for row in uow.sources.chunks[-1] if row["source_id"] == "tide-log"] == [
        chunk.body for chunk in chunk_mirror(text, smaller)
    ]


def test_the_stored_indexer_version_changes_with_the_chunk_sizes_and_keeps_the_indexer_version() -> None:
    other = ChunkSizes(target_tokens=30, max_tokens=60)

    assert indexer_version(_SIZES) == indexer_version(ChunkSizes(target_tokens=40, max_tokens=80))
    assert indexer_version(_SIZES) != indexer_version(other)
    assert indexer_version(_SIZES).startswith(INDEXER_VERSION)
