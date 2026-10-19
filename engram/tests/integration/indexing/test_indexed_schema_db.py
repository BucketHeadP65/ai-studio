r"""Integration tests for `kb.index_fingerprint.spec` / `api.indexed_schema` against a live database.

On the `migrated` fixture (a freshly `upgrade head`-migrated throwaway database): `IndexFingerprints.set`
writes the validated `KnowledgeBaseSchema` an index ran with onto the fingerprint row, and `load_indexed_schema`
reads it back through `api.indexed_schema` over the read-only `kb_reader` role (`reader_url`) — the
same restricted connection the served knowledge base actually reads through, so this proves the grant, not only
the column. Skips the whole module when Docker is unavailable (the `migrated` fixture raises `pytest.skip`).
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from sqlalchemy import NullPool, text
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine

from engram.indexing.contracts import (
    FilesystemKnowledgeBaseReader,
    IndexKnowledgeBase,
    IndexKnowledgeBaseHandler,
    SqlAlchemyKnowledgeBaseUnitOfWork,
)
from engram.indexing.infrastructure import orm
from engram.indexing.infrastructure.repository import IndexFingerprints
from engram.kb.contracts import KnowledgeBaseSchemaMissingError, load_indexed_schema
from engram.retrieval.infrastructure.gateway import ReadOnlySqlGateway
from engram.testing.fakes.embedding import FakeEmbedding
from engram.testing.fakes.uow import FakeValidationReportSink
from engram.testing.knowledge_base_builder import GOLDEN_LIGHTRAG_PAPER, KnowledgeBaseBuilder
from engram.testing.specs import research_spec

if TYPE_CHECKING:
    from pathlib import Path

    from engram.kb.contracts import KnowledgeBaseSchema


async def _write_fingerprint(migrated: str, *, collection: str, spec: KnowledgeBaseSchema) -> None:
    """Write one fingerprint row for `collection`, admin-authenticated, mirroring a real index's write."""
    engine = create_async_engine(migrated, poolclass=NullPool)
    await orm.register_pgvector(engine)
    try:
        async with AsyncSession(engine, expire_on_commit=False) as db:
            await IndexFingerprints(db).set(
                collection=collection,
                indexer_version="1",
                spec_hash="spec-hash",
                vocabulary_hash="vocabulary-hash",
                embed_model="test-embed",
                knowledge_base_root="",
                spec=spec,
            )
            await db.commit()
    finally:
        await engine.dispose()


class TestCompiledSpecRoundTrip:
    async def test_the_compile_written_spec_reads_back_equal_through_the_reader_role(self, migrated: str, reader_url: str) -> None:
        spec = research_spec()
        await _write_fingerprint(migrated, collection="research", spec=spec)

        engine = create_async_engine(reader_url, poolclass=NullPool)
        try:
            loaded = await load_indexed_schema(ReadOnlySqlGateway(engine), "research")
        finally:
            await engine.dispose()

        assert loaded == spec

    async def test_a_collection_that_never_compiled_raises_missing_knowledge_base_spec(self, reader_url: str) -> None:
        engine = create_async_engine(reader_url, poolclass=NullPool)
        try:
            with pytest.raises(KnowledgeBaseSchemaMissingError) as caught:
                await load_indexed_schema(ReadOnlySqlGateway(engine), "never-indexed")
        finally:
            await engine.dispose()

        assert "never-indexed" in str(caught.value)

    async def test_two_collections_keep_distinct_specs(self, migrated: str, reader_url: str) -> None:
        research = research_spec()
        other = research_spec().model_copy(update={"slug": "workdocs"})
        await _write_fingerprint(migrated, collection="research", spec=research)
        await _write_fingerprint(migrated, collection="workdocs", spec=other)

        engine = create_async_engine(reader_url, poolclass=NullPool)
        try:
            loaded_research = await load_indexed_schema(ReadOnlySqlGateway(engine), "research")
            loaded_other = await load_indexed_schema(ReadOnlySqlGateway(engine), "workdocs")
        finally:
            await engine.dispose()

        assert loaded_research == research
        assert loaded_other == other


def _tiny_knowledge_base(tmp_path: Path) -> Path:
    """A knowledge base with one real unit document, so the manifest is non-empty.

    `run_incremental`'s own `_can_incremental` gate requires a non-empty manifest (a fileless
    knowledge base has nothing an unchanged second index could have left dirty), so this needs at least
    one on-disk page — unlike `test_switch_stage_db.py`'s `_tiny_knowledge_base`, which never asserts on
    `report.mode` and can stay fileless.
    """
    builder = KnowledgeBaseBuilder(tmp_path)
    builder.add_unit_document("Papers", "lightrag-2025", GOLDEN_LIGHTRAG_PAPER.read_text(encoding="utf-8"))
    builder.build_minimal()
    return tmp_path


async def _null_the_spec_column(migrated: str, *, collection: str) -> None:
    """NULL `index_fingerprint.spec` for `collection`, standing in for a pre-0043 row."""
    engine = create_async_engine(migrated, poolclass=NullPool)
    try:
        async with AsyncSession(engine, expire_on_commit=False) as db:
            await db.execute(text("UPDATE kb.index_fingerprint SET spec = NULL WHERE collection = :collection"), {"collection": collection})
            await db.commit()
    finally:
        await engine.dispose()


class TestIncrementalCompileFillsInTheSpecColumn:
    """M-1: a live collection must not wait on a full rebuild to get `spec` filled in.

    Two real indexes against the same on-disk knowledge_base: the first is a full rebuild (no prior
    fingerprint), the second — nothing on disk changed — takes the incremental path
    (`run_incremental`, `IndexFingerprints.touch`, not `set`). `spec` is NULLed between the two
    to stand in for a row a page write created: if `touch` did not
    also re-stamp `spec`, the column would stay NULL forever on a collection that never runs a full
    rebuild again.
    """

    async def test_the_second_incremental_compile_fills_in_a_nulled_spec_column(self, tmp_path: Path, migrated: str, reader_url: str) -> None:
        knowledge_base = _tiny_knowledge_base(tmp_path)
        spec = research_spec()
        engine = create_async_engine(migrated, poolclass=NullPool)
        await orm.register_pgvector(engine)
        try:
            handler = IndexKnowledgeBaseHandler(
                FilesystemKnowledgeBaseReader(knowledge_base),
                FakeEmbedding(),
                lambda: SqlAlchemyKnowledgeBaseUnitOfWork(engine),
                FakeValidationReportSink(),
                spec=spec,
            )
            report = await handler.handle(IndexKnowledgeBase(collection="spectest", embed=False))
            assert report.mode == "full"

            await _null_the_spec_column(migrated, collection="spectest")

            report = await handler.handle(IndexKnowledgeBase(collection="spectest", embed=False))
            assert report.mode == "incremental"
        finally:
            await engine.dispose()

        reader_engine = create_async_engine(reader_url, poolclass=NullPool)
        try:
            loaded = await load_indexed_schema(ReadOnlySqlGateway(reader_engine), "spectest")
        finally:
            await reader_engine.dispose()

        assert loaded == spec
