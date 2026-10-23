"""Removing a source mirror's file, through the full and the incremental index, over a real migrated database.

A removed mirror that no surviving page cites must leave no `kb.sources_raw` row and no `kb.raw_chunks` row behind,
whether the mirror was first read by a full index or by an incremental one, and the report's `pruned` count must count
the source row as a full index counts it. A removed mirror that a surviving page still cites keeps its row. The
database tests skip when Docker is unavailable (the `migrated` fixture raises `pytest.skip`).
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from sqlalchemy import NullPool, text
from sqlalchemy.ext.asyncio import create_async_engine

from engram.indexing.contracts import (
    FilesystemKnowledgeBaseReader,
    IndexKnowledgeBase,
    IndexKnowledgeBaseHandler,
    SqlAlchemyKnowledgeBaseUnitOfWork,
)
from engram.indexing.infrastructure import orm
from engram.init.contracts import scaffold
from engram.kb.contracts import CanonicalSection, KnowledgeBaseSchema, PageTypeSpec, SourcesSpec
from engram.kb.domain.schema import EmbedStrategiesSpec
from engram.testing.fakes.embedding import FakeEmbedding
from engram.testing.fakes.uow import FakeValidationReportSink
from engram.testing.knowledge_base_builder import KnowledgeBaseBuilder

if TYPE_CHECKING:
    from pathlib import Path

    from engram.indexing.contracts import IndexReport

_COLLECTION = "removed-mirrors"


def _mirror(stem: str) -> str:
    """An ordinary mirror of two heading sections, naming its source id, provider and provider key."""
    return f"""---
source_id: {stem}
provider: intake
provider_key: {stem}
title: {stem.title()}
---

# {stem.title()} one

The first section of the {stem} mirror.

# {stem.title()} two

The second section of the {stem} mirror.
"""


def _note(stem: str, *, cites: str = "") -> str:
    """A note page, citing the mirror `cites` through `source_id` when given."""
    citation = f"source_id: {cites}\n" if cites else ""
    return f"---\nid: note.{stem}\ntype: note\ntitle: {stem.title()}\nstatus: draft\n{citation}---\n\n## Findings\n\nText.\n"


def _spec() -> KnowledgeBaseSchema:
    """Notes that may cite a mirror through `source_id`, with mirrors cut into raw chunks."""
    return KnowledgeBaseSchema(
        slug=_COLLECTION,
        page_types=(
            PageTypeSpec(page_type="note", id_prefix="note.", folder="Wiki/Notes", canonical_sections=(CanonicalSection(slug="findings"),)),
        ),
        sources=SourcesSpec(key_field="source_id"),
        embed_strategies=EmbedStrategiesSpec(active=("page-card", "section", "source-chunk")),
    )


def _knowledge_base(root: Path, spec: KnowledgeBaseSchema) -> KnowledgeBaseBuilder:
    """Write the schema, the scaffolded vocabulary, the orientation stubs and one note citing nothing."""
    builder = KnowledgeBaseBuilder(root)
    builder.add_meta_stubs()
    builder.add_knowledge_base_schema(spec)
    for relative, body in scaffold(spec).items():
        if relative.startswith("_meta/vocabulary/"):
            (root / relative).parent.mkdir(parents=True, exist_ok=True)
            (root / relative).write_text(body, encoding="utf-8")
    builder.add_unit_document("Wiki/Notes", "plain", _note("plain"))
    return builder


async def _index(knowledge_base: Path, migrated: str, spec: KnowledgeBaseSchema, *, full: bool) -> IndexReport:
    """Run one index of `knowledge_base` into the migrated database, a full rebuild when `full`."""
    engine = create_async_engine(migrated, poolclass=NullPool)
    await orm.register_pgvector(engine)
    try:
        handler = IndexKnowledgeBaseHandler(
            FilesystemKnowledgeBaseReader(knowledge_base, note_folders=spec.note_folders),
            FakeEmbedding(),
            lambda: SqlAlchemyKnowledgeBaseUnitOfWork(engine),
            FakeValidationReportSink(),
            spec=spec,
        )
        return await handler.handle(IndexKnowledgeBase(collection=_COLLECTION, embed=False, full=full))
    finally:
        await engine.dispose()


async def _rows(migrated: str, sql: str) -> list[tuple[str, ...]]:
    """The rows `sql` answers for the collection, each as a tuple of strings, sorted."""
    engine = create_async_engine(migrated, poolclass=NullPool)
    try:
        async with engine.connect() as conn:
            result = await conn.execute(text(sql), {"collection": _COLLECTION})
            return sorted(tuple(str(value) for value in row) for row in result.all())
    finally:
        await engine.dispose()


async def _source_ids(migrated: str) -> list[tuple[str, ...]]:
    return await _rows(migrated, "SELECT source_id FROM kb.sources_raw WHERE collection = :collection")


async def _chunk_sources(migrated: str) -> list[tuple[str, ...]]:
    return await _rows(migrated, "SELECT DISTINCT source_id FROM kb.raw_chunks WHERE collection = :collection")


async def _manifest_mirrors(migrated: str) -> list[tuple[str, ...]]:
    return await _rows(migrated, "SELECT path, source_key FROM kb.index_manifest WHERE collection = :collection AND kind = 'source'")


class TestRemovingAMirrorTheFullIndexRead:
    @pytest.mark.parametrize("full", [True, False], ids=["full", "incremental"])
    async def test_the_index_deletes_its_source_row_and_chunks_and_counts_the_row(self, tmp_path: Path, migrated: str, full: bool) -> None:
        spec = _spec()
        builder = _knowledge_base(tmp_path, spec)
        gone = builder.add_source_mirror("alpha", _mirror("alpha"))
        builder.add_source_mirror("beta", _mirror("beta"))
        await _index(tmp_path, migrated, spec, full=True)
        assert await _source_ids(migrated) == [("alpha",), ("beta",)]

        gone.unlink()
        report = await _index(tmp_path, migrated, spec, full=full)

        assert report.mode == ("full" if full else "incremental")
        assert await _source_ids(migrated) == [("beta",)]
        assert await _chunk_sources(migrated) == [("beta",)]
        assert report.pruned == 1


class TestRemovingAMirrorAnIncrementalIndexAdded:
    async def test_the_next_incremental_index_deletes_its_source_row_and_chunks_and_counts_the_row(self, tmp_path: Path, migrated: str) -> None:
        spec = _spec()
        builder = _knowledge_base(tmp_path, spec)
        builder.add_source_mirror("beta", _mirror("beta"))
        await _index(tmp_path, migrated, spec, full=True)
        gone = builder.add_source_mirror("alpha", _mirror("alpha"))
        added = await _index(tmp_path, migrated, spec, full=False)
        assert added.mode == "incremental"
        assert await _source_ids(migrated) == [("alpha",), ("beta",)]
        assert await _manifest_mirrors(migrated) == [("Sources/alpha.md", "alpha"), ("Sources/beta.md", "beta")]

        gone.unlink()
        report = await _index(tmp_path, migrated, spec, full=False)

        assert report.mode == "incremental"
        assert await _source_ids(migrated) == [("beta",)]
        assert await _chunk_sources(migrated) == [("beta",)]
        assert report.pruned == 1


class TestRemovingAMirrorASurvivingPageCites:
    async def test_the_incremental_index_keeps_its_source_row(self, tmp_path: Path, migrated: str) -> None:
        spec = _spec()
        builder = _knowledge_base(tmp_path, spec)
        builder.add_unit_document("Wiki/Notes", "citing", _note("citing", cites="alpha"))
        gone = builder.add_source_mirror("alpha", _mirror("alpha"))
        await _index(tmp_path, migrated, spec, full=True)

        gone.unlink()
        report = await _index(tmp_path, migrated, spec, full=False)

        assert report.mode == "incremental"
        assert await _source_ids(migrated) == [("alpha",)]
        assert report.pruned == 0
