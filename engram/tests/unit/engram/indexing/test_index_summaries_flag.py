r"""`IndexKnowledgeBase(summaries=True)` wires the disclosure-summary stage into the indexing handler.

`SummaryStage` is decoupled from the indexing pipeline's own `IndexUnitOfWork`: it reads its
pending-unit queue from its own `SummaryUnitOfWork`, so these specs drive it over `FakeSummaryUow` +
`FakeSummarizer` with no dependency on what the indexing pipeline itself loaded (`FakeKnowledgeBaseReader` is
left empty — an empty knowledge base still runs the full path on a fresh, cold-start
`FakeKnowledgeBaseUnitOfWork`). Pinned:

* `summaries=True` runs the stage after the (skipped) embed stage on the FULL path: the summarizer
  is called once per pending unit and the pending rows land through `upsert_summary`,
  `status='pending'`;
* `summaries=True` ALSO runs the stage on the INCREMENTAL path (`TestSummariesFlagOnIncrementalPath`)
  — incremental is the default on an already-indexed knowledge base, and staleness-driven regeneration
  relies on it, so a full-path-only hook would silently miss the common case;
* `summaries=False` (the default) never touches the stage on either path — behaviour is unchanged
  unless flagged;
* `summaries=True` with no `summary_stage` configured is a safe no-op, not a crash (the production
  wiring always supplies one; a handler built without it — e.g. an older test fixture — must not
  break just because the flag is set).
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from engram.indexing.contracts import (
    FilesystemKnowledgeBaseReader,
    IndexKnowledgeBase,
    IndexKnowledgeBaseHandler,
    IndexReport,
    ManifestEntry,
    SummaryStage,
    SummaryUnit,
)
from engram.indexing.domain.manifest import FileType
from engram.indexing.read_models import CleanPage, StoredFingerprint
from engram.testing.fakes.embedding import FakeEmbedding
from engram.testing.fakes.knowledge_base_reader import FakeKnowledgeBaseReader
from engram.testing.fakes.summarizer import FakeSummarizer
from engram.testing.fakes.summary_uow import FakeSummaryUow
from engram.testing.fakes.uow import FakeKnowledgeBaseUnitOfWork, FakeValidationReportSink
from engram.testing.knowledge_base_builder import KnowledgeBaseBuilder
from engram.testing.specs import research_spec

if TYPE_CHECKING:
    from pathlib import Path

# A single well-formed entity page (no claims), just enough to give a first full run one real
# `index_manifest` entry — an empty `FakeKnowledgeBaseReader` knowledge base can never satisfy the incremental
# branch's "manifest is non-empty" gate (it writes zero manifest entries), so the incremental-path
# specs need a real (if minimal) on-disk knowledge base instead.
_ENTITY = """\
---
id: ent.lightrag
type: entity
title: LightRAG
status: draft
entity_type: technique
---

## Definition

A graph-indexed retrieval technique.
"""


def _handler(*, uow: FakeKnowledgeBaseUnitOfWork, summary_stage: SummaryStage | None = None) -> IndexKnowledgeBaseHandler:
    """Build the handler over an empty `FakeKnowledgeBaseReader` + fakes, optionally wired with a `SummaryStage`."""
    return IndexKnowledgeBaseHandler(
        FakeKnowledgeBaseReader(), FakeEmbedding(), lambda: uow, FakeValidationReportSink(), spec=research_spec(), summary_stage=summary_stage
    )


def _knowledge_base_handler(knowledge_base_root: Path, *, uow: FakeKnowledgeBaseUnitOfWork, summary_stage: SummaryStage | None) -> IndexKnowledgeBaseHandler:
    """Build the handler over a real on-disk knowledge base (the incremental-path fixtures need real files)."""
    return IndexKnowledgeBaseHandler(
        FilesystemKnowledgeBaseReader(knowledge_base_root, note_folders=research_spec().note_folders),
        FakeEmbedding(),
        lambda: uow,
        FakeValidationReportSink(),
        spec=research_spec(),
        summary_stage=summary_stage,
    )


def _seed_incremental_state(uow: FakeKnowledgeBaseUnitOfWork) -> list[CleanPage]:
    """Promote a completed full run's manifest + fingerprint WRITES into the fake's READ view.

    The inverse of the cold-start trick the other specs in this file rely on: `_can_incremental`
    reads `manifest.load` / `fingerprint.get`, which read the fake's separate `stored` attributes,
    not what a prior run wrote. Copying the writes into `stored` (mirrors `test_handlers.py`'s
    `_seed_stored_state_from_full_run` + `_clean_pages_from_full_run`) makes the *next*
    `handle()` call take the incremental branch. Returns the clean-page view a real
    `handler.set_clean_page_view` would be fed.
    """
    stored: dict[str, ManifestEntry] = {}
    for batch in uow.manifest.manifest_upserts:
        for entry in batch:
            path = str(entry["path"])
            page_id = entry["page_id"]
            source_key = entry["source_key"]
            stored[path] = ManifestEntry(
                path=path,
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

    path_by_page = {page_id: path for path, entry in stored.items() if (page_id := entry.page_id) is not None}
    clean: list[CleanPage] = []
    for row in uow.pages.upserted_rows:
        page_id = str(row["id"])
        if page_id not in path_by_page:
            continue
        aliases = row["aliases"]
        source_id = row.get("source_id")
        clean.append(
            CleanPage(
                id=page_id,
                slug=str(row["slug"]),
                title=str(row["title"]),
                aliases=tuple(str(alias) for alias in aliases) if isinstance(aliases, list | tuple) else (),
                path=path_by_page[page_id],
                source_id=str(source_id) if source_id else None,
            )
        )
    return clean


class TestSummariesFlagOn:
    async def test_calls_the_summarizer_once_per_pending_unit(self):
        unit = SummaryUnit(unit_id="mu.x", unit_type="page", title="X", body="Body.", source_hash="h1")
        summary_uow = FakeSummaryUow([unit])
        summarizer = FakeSummarizer()
        stage = SummaryStage(lambda: summary_uow, summarizer)
        handler = _handler(uow=FakeKnowledgeBaseUnitOfWork(), summary_stage=stage)

        await handler.handle(IndexKnowledgeBase(collection="workdocs", embed=False, summaries=True))

        assert summarizer.calls == ["X"]

    async def test_writes_pending_rows_scoped_to_the_command_collection(self):
        unit = SummaryUnit(unit_id="mu.x", unit_type="page", title="X", body="Body.", source_hash="h1")
        summary_uow = FakeSummaryUow([unit])
        stage = SummaryStage(lambda: summary_uow, FakeSummarizer())
        handler = _handler(uow=FakeKnowledgeBaseUnitOfWork(), summary_stage=stage)

        await handler.handle(IndexKnowledgeBase(collection="workdocs", embed=False, summaries=True))

        assert len(summary_uow.upserts) == 1
        row = summary_uow.upserts[0]
        assert row["unit_id"] == "mu.x"
        assert row["collection"] == "workdocs"
        assert row["status"] == "pending"
        assert summary_uow.committed is True

    async def test_no_pending_units_writes_nothing_but_still_commits(self):
        summary_uow = FakeSummaryUow([])
        summarizer = FakeSummarizer()
        stage = SummaryStage(lambda: summary_uow, summarizer)
        handler = _handler(uow=FakeKnowledgeBaseUnitOfWork(), summary_stage=stage)

        await handler.handle(IndexKnowledgeBase(collection="workdocs", embed=False, summaries=True))

        assert summarizer.calls == []
        assert summary_uow.upserts == []
        assert summary_uow.committed is True


class TestSummariesFlagOff:
    async def test_default_compile_never_touches_the_summary_stage(self):
        unit = SummaryUnit(unit_id="mu.x", unit_type="page", title="X", body="Body.", source_hash="h1")
        summary_uow = FakeSummaryUow([unit])
        summarizer = FakeSummarizer()
        stage = SummaryStage(lambda: summary_uow, summarizer)
        handler = _handler(uow=FakeKnowledgeBaseUnitOfWork(), summary_stage=stage)

        await handler.handle(IndexKnowledgeBase(collection="workdocs", embed=False))

        assert summarizer.calls == []
        assert summary_uow.upserts == []
        assert summary_uow.committed is False


class TestSummariesFlagWithNoStageConfigured:
    async def test_is_a_safe_no_op(self):
        handler = _handler(uow=FakeKnowledgeBaseUnitOfWork(), summary_stage=None)

        report = await handler.handle(IndexKnowledgeBase(collection="workdocs", embed=False, summaries=True))

        assert isinstance(report, IndexReport)


class TestSummariesFlagOnIncrementalPath:
    """Incremental is the DEFAULT path on an already-indexed knowledge base — `--summaries` must reach it too."""

    async def test_calls_the_summarizer_on_an_incremental_run(self, tmp_path: Path):
        builder = KnowledgeBaseBuilder(tmp_path)
        builder.add_unit_document("Wiki/Entities", "lightrag", _ENTITY)
        builder.build_minimal()
        uow = FakeKnowledgeBaseUnitOfWork()
        unit = SummaryUnit(unit_id="mu.x", unit_type="page", title="X", body="Body.", source_hash="h1")
        summary_uow = FakeSummaryUow([unit])
        summarizer = FakeSummarizer()
        stage = SummaryStage(lambda: summary_uow, summarizer)
        handler = _knowledge_base_handler(tmp_path, uow=uow, summary_stage=stage)
        # Seed run: cold start (empty manifest) forces the full path; summaries off so it writes
        # nothing to the summary stage yet.
        await handler.handle(IndexKnowledgeBase(collection="workdocs", embed=False))
        clean = _seed_incremental_state(uow)
        handler.set_clean_page_view(lambda: clean)

        report = await handler.handle(IndexKnowledgeBase(collection="workdocs", embed=False, summaries=True))

        assert report.mode == "incremental"
        assert summarizer.calls == ["X"]
        assert len(summary_uow.upserts) == 1
        assert summary_uow.upserts[0]["status"] == "pending"
        assert summary_uow.upserts[0]["collection"] == "workdocs"

    async def test_summaries_false_on_an_incremental_run_does_not_touch_the_stage(self, tmp_path: Path):
        builder = KnowledgeBaseBuilder(tmp_path)
        builder.add_unit_document("Wiki/Entities", "lightrag", _ENTITY)
        builder.build_minimal()
        uow = FakeKnowledgeBaseUnitOfWork()
        unit = SummaryUnit(unit_id="mu.x", unit_type="page", title="X", body="Body.", source_hash="h1")
        summary_uow = FakeSummaryUow([unit])
        summarizer = FakeSummarizer()
        stage = SummaryStage(lambda: summary_uow, summarizer)
        handler = _knowledge_base_handler(tmp_path, uow=uow, summary_stage=stage)
        await handler.handle(IndexKnowledgeBase(collection="workdocs", embed=False))
        clean = _seed_incremental_state(uow)
        handler.set_clean_page_view(lambda: clean)

        report = await handler.handle(IndexKnowledgeBase(collection="workdocs", embed=False, summaries=False))

        assert report.mode == "incremental"
        assert summarizer.calls == []
        assert summary_uow.upserts == []
