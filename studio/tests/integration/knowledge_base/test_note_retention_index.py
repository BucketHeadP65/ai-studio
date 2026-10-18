"""Keeping a note and then reading it back out of a real database.

The unit tier proves what retention decides. This proves the other half: that what it writes is a
page the indexer actually indexes, over a disposable database and the real indexing path rather
than a stand-in for it.

The second half removes a section from the kept note and indexes again, which is the case an
incremental index has to get right: the page stays, and the section that is no longer in the file
is no longer in the index either.

Skips when Docker is unavailable, through the `migrated` fixture's own guard.
"""

from __future__ import annotations

import json

from typing import TYPE_CHECKING

import pytest

from core.files import atomic_write
from engram.authoring.contracts import ReindexResult
from engram.indexing.contracts import FilesystemKnowledgeBaseReader, IndexKnowledgeBase, IndexKnowledgeBaseHandler
from engram.indexing.infrastructure import orm
from engram.kb.contracts import KnowledgeBaseSchema, load_knowledge_base_schema
from engram.testing.fakes.embedding import FakeEmbedding
from engram.testing.knowledge_base_builder import KnowledgeBaseBuilder
from sqlalchemy import NullPool, text
from sqlalchemy.ext.asyncio import create_async_engine

from studio.knowledge_base.indexing.infrastructure.validation_report_sink import FileValidationReportSink
from studio.knowledge_base.retention.contracts import NotePreview, NoteRetention, RetainNoteRequest
from studio.knowledge_base.retention.domain import RetentionState
from studio.knowledge_base.retention.infrastructure.artifact_resolver import HarnessArtifactResolver
from studio.knowledge_base.retention.infrastructure.knowledge_bases import knowledge_base_reader
from studio.knowledge_base.retention.infrastructure.receipts import RetentionReceipts
from studio.knowledge_base.wiring.indexing import KnowledgeBaseUnitOfWorkFactory
from tests.support.fakes.harness_artifacts import FakeHarness
from tests.support.http_app import write_settings_json

if TYPE_CHECKING:
    from collections.abc import AsyncIterator
    from pathlib import Path

    from engram.authoring.contracts import AuthoringKnowledgeBase
    from pydantic import JsonValue
    from sqlalchemy.ext.asyncio import AsyncEngine

KNOWLEDGE_BASE = "kept-notes"
COLLECTION = "kept-notes"
ARTIFACT = "retention_draft"
REMOVED_HEADING = "Sources"

NOTE_DRAFT: dict[str, JsonValue] = {
    "page_type": "concept",
    "title": "What the run concluded",
    "summary": "One reading of what the evidence supports.",
    "frontmatter": {"status": "draft", "created": "2026-09-13"},
    "sections": [
        {"heading": "Definition", "body": "The conclusion holds under the stated conditions."},
        {"heading": REMOVED_HEADING, "body": "The two papers read for this."},
    ],
}


def _spec() -> KnowledgeBaseSchema:
    """A schema declaring the one page type a kept note lands as, and this test's own collection."""
    return KnowledgeBaseSchema.model_validate(
        {
            "slug": KNOWLEDGE_BASE,
            "page_types": [
                {
                    "page_type": "concept",
                    "id_prefix": "c-",
                    "folder": "Wiki/Concepts",
                    "canonical_sections": ["definition", "sources"],
                }
            ],
            "retrieval": {"collection": COLLECTION},
        }
    )


def _knowledge_base(root: Path) -> Path:
    """A knowledge base on disk the indexer can read: vocabulary, the orientation stubs, and its own schema."""
    builder = KnowledgeBaseBuilder(root)
    builder.add_vocabulary()
    builder.add_meta_stubs()
    builder.add_knowledge_base_schema(_spec())
    return root


class _RealReindex:
    """`Reindex` over the real indexing handler against one disposable database.

    The same handler the application's own index command drives, with a fake embedder so no
    embedding service is called. Everything else, the reader, the unit of work, the prune, is the
    production path.
    """

    def __init__(self, engine: AsyncEngine) -> None:
        """Hold the engine every index writes through."""
        self._engine = engine
        self.runs = 0

    async def __call__(self, knowledge_base: AuthoringKnowledgeBase) -> ReindexResult:
        """Index `knowledge_base` into this test's database and report what the index found."""
        self.runs += 1
        spec = load_knowledge_base_schema(knowledge_base.path)
        handler = IndexKnowledgeBaseHandler(
            FilesystemKnowledgeBaseReader(knowledge_base.path, note_folders=spec.note_folders, note_files=spec.note_files),
            FakeEmbedding(),
            KnowledgeBaseUnitOfWorkFactory(self._engine),
            FileValidationReportSink(knowledge_base.path),
            spec=spec,
        )
        report = await handler.handle(IndexKnowledgeBase(collection=knowledge_base.collection, collection_named=True))
        outcome = "clean" if report.validation.n_errors == 0 else "validation_errors"
        return ReindexResult(outcome=outcome, detail=f"indexed {knowledge_base.path}", findings_total=report.validation.n_errors)


@pytest.fixture
async def engine(migrated: str) -> AsyncIterator[AsyncEngine]:
    """An engine over this test's own freshly migrated database."""
    built = create_async_engine(migrated, poolclass=NullPool)
    await orm.register_pgvector(built)
    try:
        yield built
    finally:
        await built.dispose()


async def _page_titles(engine: AsyncEngine) -> dict[str, str]:
    """Every non-orientation page this collection holds, by id."""
    async with engine.connect() as conn:
        rows = await conn.execute(
            text("SELECT id, title FROM kb.pages WHERE collection = :c AND id NOT LIKE 'meta.%'"), {"c": COLLECTION}
        )
        return {row[0]: row[1] for row in rows}


async def _section_headings(engine: AsyncEngine, page_id: str) -> set[str]:
    """Every section heading the index holds for one page."""
    async with engine.connect() as conn:
        rows = await conn.execute(
            text("SELECT heading FROM kb.sections WHERE collection = :c AND page_id = :p"), {"c": COLLECTION, "p": page_id}
        )
        return {row[0] for row in rows}


class TestAKeptNoteIsReadable:
    """A note kept through the service is a page the index answers with."""

    async def test_keeping_a_note_writes_a_page_the_index_then_holds(self, tmp_path: Path, engine: AsyncEngine) -> None:
        root = _knowledge_base(tmp_path / KNOWLEDGE_BASE)
        settings_path = write_settings_json(
            tmp_path / "application_settings.json", {KNOWLEDGE_BASE: {"path": str(root), "description": ""}}
        )
        harness = FakeHarness(values={ARTIFACT: dict(NOTE_DRAFT)})
        reindex = _RealReindex(engine)
        retention = NoteRetention(
            HarnessArtifactResolver("http://harness.invalid", transport=harness.transport()),
            knowledge_base_reader(settings_path),
            reindex,
            RetentionReceipts,
        )

        preview = await retention.preview(KNOWLEDGE_BASE, harness.ref(ARTIFACT))
        kept = await retention.retain(KNOWLEDGE_BASE, _request(preview, harness))

        assert kept.outcome == "saved"
        assert (root / preview.path).read_text(encoding="utf-8") == preview.markdown
        assert await _page_titles(engine) == {kept.note_id: "What the run concluded"}
        assert await _section_headings(engine, kept.note_id) == {"Definition", REMOVED_HEADING}

    async def test_removing_a_section_removes_it_from_the_index_and_keeps_the_page(self, tmp_path: Path, engine: AsyncEngine) -> None:
        root = _knowledge_base(tmp_path / KNOWLEDGE_BASE)
        settings_path = write_settings_json(
            tmp_path / "application_settings.json", {KNOWLEDGE_BASE: {"path": str(root), "description": ""}}
        )
        harness = FakeHarness(values={ARTIFACT: dict(NOTE_DRAFT)})
        reindex = _RealReindex(engine)
        retention = NoteRetention(
            HarnessArtifactResolver("http://harness.invalid", transport=harness.transport()),
            knowledge_base_reader(settings_path),
            reindex,
            RetentionReceipts,
        )
        preview = await retention.preview(KNOWLEDGE_BASE, harness.ref(ARTIFACT))
        kept = await retention.retain(KNOWLEDGE_BASE, _request(preview, harness))
        assert await _section_headings(engine, kept.note_id) == {"Definition", REMOVED_HEADING}

        landed = root / preview.path
        atomic_write(landed, _without_the_sources_section(preview.markdown))
        await reindex(knowledge_base_reader(settings_path)(KNOWLEDGE_BASE))

        assert await _page_titles(engine) == {kept.note_id: "What the run concluded"}
        assert await _section_headings(engine, kept.note_id) == {"Definition"}

    async def test_the_receipt_survives_the_index_and_names_the_read_ref(self, tmp_path: Path, engine: AsyncEngine) -> None:
        root = _knowledge_base(tmp_path / KNOWLEDGE_BASE)
        settings_path = write_settings_json(
            tmp_path / "application_settings.json", {KNOWLEDGE_BASE: {"path": str(root), "description": ""}}
        )
        harness = FakeHarness(values={ARTIFACT: dict(NOTE_DRAFT)})
        retention = NoteRetention(
            HarnessArtifactResolver("http://harness.invalid", transport=harness.transport()),
            knowledge_base_reader(settings_path),
            _RealReindex(engine),
            RetentionReceipts,
        )
        preview = await retention.preview(KNOWLEDGE_BASE, harness.ref(ARTIFACT))
        kept = await retention.retain(KNOWLEDGE_BASE, _request(preview, harness))

        receipt = json.loads(next((root / ".engram" / "retention").glob("*.json")).read_text(encoding="utf-8"))
        assert receipt["state"] == RetentionState.INDEXED.value
        assert receipt["read_ref"] == kept.read_ref == f"page:{kept.note_id}"
        assert receipt["target"] == preview.path


def _request(preview: NotePreview, harness: FakeHarness) -> RetainNoteRequest:
    """The keep request for one preview, built the way the route's own body arrives."""
    return RetainNoteRequest.model_validate(
        {
            "idempotency_key": "key-1",
            "origin": harness.ref(ARTIFACT).model_dump(mode="json"),
            "draft": preview.draft.model_dump(mode="json"),
            "schema_digest": preview.schema_digest,
            "preview_digest": preview.preview_digest,
        }
    )


def _without_the_sources_section(markdown: str) -> str:
    """`markdown` with its last section dropped, the way an owner deleting one would leave the file."""
    head, marker, _ = markdown.partition(f"## {REMOVED_HEADING}")
    assert marker, markdown
    return head.rstrip() + "\n"
