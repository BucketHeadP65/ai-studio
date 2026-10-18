"""`read_source` over a real, migrated Postgres, fed by a real index: a split is judged by heading sections, not chunks.

A mirror whose heading sections are each longer than the maximum chunk size is cut into more chunks than it has heading
sections. `section_count` must still count its heading sections, which is what one chunk per heading counted before, so
`split_recommended` gives the same answer as before for the same threshold. Skips when Docker is unavailable.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from sqlalchemy import NullPool
from sqlalchemy.ext.asyncio import create_async_engine

from engram.authoring.contracts import AuthoringKnowledgeBase, AuthoringTools
from engram.indexing.contracts import (
    FilesystemKnowledgeBaseReader,
    IndexKnowledgeBase,
    IndexKnowledgeBaseHandler,
    SqlAlchemyKnowledgeBaseUnitOfWork,
)
from engram.indexing.infrastructure import orm
from engram.retrieval.infrastructure.gateway import ReadOnlySqlGateway
from engram.sources.contracts import SourceQueue, SourceType, claimants_for
from engram.testing.fakes.authoring import FakeReindex
from engram.testing.fakes.connector import FakeConnector
from engram.testing.fakes.embedding import FakeEmbedding
from engram.testing.fakes.uow import FakeValidationReportSink
from engram.testing.knowledge_base_builder import KnowledgeBaseBuilder
from engram.testing.specs import research_spec

if TYPE_CHECKING:
    from pathlib import Path

    from engram.kb.contracts import KnowledgeBaseSchema

_COLLECTION = "research"

_CONNECTORS = {"youtube": FakeConnector(name="youtube", source_type_from=SourceType)}
"""A fake standing in for the mirrors' connector, whose item types fold to engram's own source types."""

_HEADINGS = ("1 Setting", "2 Method", "2.1 Gauges", "3 Results")


def _section_body(index: int) -> str:
    """About 1,500 tokens of paragraphs, so one heading section passes the maximum chunk size."""
    return "\n\n".join(
        " ".join(f"Reading {index}.{paragraph}.{place} came from the north station at dawn." for place in range(10))
        for paragraph in range(12)
    )


def _headed_mirror() -> str:
    sections = "".join(f"## {heading}\n\n{_section_body(index)}\n\n" for index, heading in enumerate(_HEADINGS))
    return (
        "---\nsource_key: yt-headed\nsource_hash: h-headed\nprovider: youtube\nitem_type: video\ntitle: Station readings\n---\n"
        f"Station readings, an opening line.\n\n{sections}"
    )


def _headless_mirror() -> str:
    paragraphs = "\n\n".join(_section_body(index) for index in range(2))
    return f"---\nsource_key: yt-headless\nsource_hash: h-headless\nprovider: youtube\nitem_type: video\ntitle: Tide log\n---\n{paragraphs}\n"


def _spec(threshold: int) -> KnowledgeBaseSchema:
    base = research_spec()
    return base.model_copy(update={"sources": base.sources.model_copy(update={"digest_sections_over": threshold, "digest_part_field": "part_of"})})


async def _index(root: Path, db_url: str) -> None:
    spec = research_spec()
    engine = create_async_engine(db_url, poolclass=NullPool)
    await orm.register_pgvector(engine)
    try:
        reader = FilesystemKnowledgeBaseReader(root, "Sources", spec.note_folders, note_files=spec.note_files)
        handler = IndexKnowledgeBaseHandler(
            reader, FakeEmbedding(), lambda: SqlAlchemyKnowledgeBaseUnitOfWork(engine), FakeValidationReportSink(), spec=spec
        )
        await handler.handle(IndexKnowledgeBase(collection=_COLLECTION, embed=False))
    finally:
        await engine.dispose()


async def _read_source(db_url: str, root: Path, source_id: str, *, threshold: int) -> dict[str, object]:
    engine = create_async_engine(db_url, poolclass=NullPool)
    await orm.register_pgvector(engine)
    try:
        knowledge_base = AuthoringKnowledgeBase(name="research", collection=_COLLECTION, path=root, schema=_spec(threshold))
        tools = AuthoringTools(
            knowledge_bases={"research": knowledge_base},
            reindex=FakeReindex(),
            admin=ReadOnlySqlGateway(engine),
            queue_for=lambda t: SourceQueue(t.path / "_meta" / "source-queue.yaml", connectors=claimants_for(t.schema.sources.connectors, source=_CONNECTORS)),
            connectors=_CONNECTORS,
        )
        return await tools.read_source("research", source_id)
    finally:
        await engine.dispose()


async def _indexed(tmp_path: Path, db_url: str) -> Path:
    builder = KnowledgeBaseBuilder(tmp_path)
    builder.build_minimal()
    builder.add_source_mirror("station-readings", _headed_mirror())
    builder.add_source_mirror("tide-log", _headless_mirror())
    await _index(tmp_path, db_url)
    return tmp_path


class TestReadSourceSectionsAgainstARealIndex:
    async def test_a_headed_source_counts_its_heading_sections_not_its_chunks(self, migrated: str, tmp_path: Path) -> None:
        root = await _indexed(tmp_path, migrated)

        mirror = await _read_source(migrated, root, "yt-headed", threshold=40)

        heading_sections = 1 + len(_HEADINGS)
        assert mirror["section_count"] == heading_sections
        chunk_count = mirror["chunk_count"]
        assert isinstance(chunk_count, int)
        assert chunk_count > heading_sections

    async def test_split_recommended_answers_as_before_for_a_headed_source(self, migrated: str, tmp_path: Path) -> None:
        root = await _indexed(tmp_path, migrated)
        heading_sections = 1 + len(_HEADINGS)

        under = await _read_source(migrated, root, "yt-headed", threshold=heading_sections)
        over = await _read_source(migrated, root, "yt-headed", threshold=heading_sections - 1)

        assert under["split_recommended"] is False
        assert over["split_recommended"] is True

    async def test_a_headless_source_is_one_heading_section_however_many_chunks_it_fills(self, migrated: str, tmp_path: Path) -> None:
        root = await _indexed(tmp_path, migrated)

        mirror = await _read_source(migrated, root, "yt-headless", threshold=1)

        chunk_count = mirror["chunk_count"]
        assert isinstance(chunk_count, int)
        assert chunk_count > 1
        assert mirror["section_count"] == 1
        assert mirror["split_recommended"] is False
