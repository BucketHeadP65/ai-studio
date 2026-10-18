r"""`compounding_work` pages rows over a real, migrated Postgres, fed by a real index.

A paper whose claim quotes words its mirror never held is indexed with a stored validation error. The row for that page then carries its source, its file and the error's rule name, whether the page is read by id or by the entry's own window.

Skips when Docker is unavailable (the `migrated` fixture raises `pytest.skip`).
"""

from __future__ import annotations

from datetime import UTC, datetime
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
from engram.kb.contracts import DerivationEntry
from engram.kb.domain.schema import CompoundingSpec
from engram.retrieval.infrastructure.gateway import ReadOnlySqlGateway
from engram.sources.contracts import SourceQueue, claimants_for
from engram.testing.fakes.authoring import FakeReindex
from engram.testing.fakes.embedding import FakeEmbedding
from engram.testing.fakes.uow import FakeValidationReportSink
from engram.testing.knowledge_base_builder import KnowledgeBaseBuilder
from engram.testing.specs import research_spec

if TYPE_CHECKING:
    from datetime import date
    from pathlib import Path

    from engram.kb.contracts import KnowledgeBaseSchema

_COLLECTION = "research"

_MIRROR = """\
---
source_key: arxiv-7
provider: intake
title: Seven
---

# Seven

The real sentence.
"""

_FACETS = """---
id: meta.vocabulary-facets
type: meta
title: "Vocabulary: facets"
summary: The claim types and paper types these fixtures use.
status: verified
---

## `kind` on claim blocks (single value)

| Value | Meaning |
|---|---|
| `observation` | Something observed |

## `paper_type` on papers (single value)

| Value | Meaning |
|---|---|
| `benchmark` | A benchmark |
"""


def _paper(created: date) -> str:
    """A raw paper page whose one claim quotes words the mirror lacks."""
    return f"""---
id: paper.seven
type: paper
title: Seven
summary: A paper about seven.
status: active
created: {created.isoformat()}
paper_type: benchmark
year: 2024
zotero_key: arxiv-7
source_hash: h7
digest_of: arxiv-7
---

## TL;DR

Seven.

## Findings

- Invented. ^c1
    - kind:: observation
    - loc:: §1
    - quote:: "words nobody wrote"
"""


def _spec() -> KnowledgeBaseSchema:
    spec = research_spec()
    entry = DerivationEntry.model_validate(
        {"name": "flag-ideas", "source": {"plane": "pages", "page_types": ["paper"], "within_hours": 48}, "to": ["flag"], "cap_per_run": 5}
    )
    return spec.model_copy(
        update={
            "sources": spec.sources.model_copy(update={"quote_verify": True}),
            "compounding": CompoundingSpec(entries=(entry,)),
        }
    )


def _build_knowledge_base(tmp_path: Path) -> Path:
    """A fixture knowledge base: one mirror and one paper written as a raw file, which the write-time check would refuse."""
    builder = KnowledgeBaseBuilder(tmp_path)
    builder.build_minimal()
    (tmp_path / "_meta" / "vocabulary").mkdir(parents=True, exist_ok=True)
    (tmp_path / "_meta" / "vocabulary" / "facets.md").write_text(_FACETS, encoding="utf-8")
    builder.add_source_mirror("seven", _MIRROR)
    builder.add_unit_document("Papers", "seven", _paper(datetime.now(tz=UTC).date()))
    return tmp_path


async def _compile(knowledge_base: Path, db_url: str) -> None:
    """Run the real `IndexKnowledgeBaseHandler` (real reader, real UoW, fake embedder) over `knowledge_base`."""
    spec = _spec()
    engine = create_async_engine(db_url, poolclass=NullPool)
    await orm.register_pgvector(engine)
    try:
        reader = FilesystemKnowledgeBaseReader(knowledge_base, "Sources", spec.note_folders, note_files=spec.note_files)
        handler = IndexKnowledgeBaseHandler(
            reader, FakeEmbedding(), lambda: SqlAlchemyKnowledgeBaseUnitOfWork(engine), FakeValidationReportSink(), spec=spec
        )
        await handler.handle(IndexKnowledgeBase(collection=_COLLECTION, embed=True))
    finally:
        await engine.dispose()


async def _work(db_url: str, target_path: Path, page_ids: list[str] | None) -> list[dict[str, object]]:
    """Run `compounding_work` for `flag-ideas` through the production admin gateway over `db_url`."""
    engine = create_async_engine(db_url, poolclass=NullPool)
    await orm.register_pgvector(engine)
    try:
        knowledge_base = AuthoringKnowledgeBase(name="research", collection=_COLLECTION, path=target_path, schema=_spec())
        doors = AuthoringTools(
            knowledge_bases={"research": knowledge_base},
            reindex=FakeReindex(),
            admin=ReadOnlySqlGateway(engine),
            queue_for=lambda t: SourceQueue(t.path / "_meta" / "source-queue.yaml", connectors=claimants_for(t.schema.sources.connectors, source={})),
            connectors={},
        )
        result = await doors.compounding_work("research", "flag-ideas", page_ids=page_ids)
        rows = result["rows"]
        assert isinstance(rows, list)
        return rows
    finally:
        await engine.dispose()


class TestCompoundingWorkRowsAgainstARealCompile:
    async def test_a_named_page_carries_its_source_its_file_and_its_findings(self, migrated: str, tmp_path: Path) -> None:
        knowledge_base = _build_knowledge_base(tmp_path)

        await _compile(knowledge_base, migrated)
        rows = await _work(migrated, knowledge_base, ["paper.seven"])

        assert len(rows) == 1
        row = rows[0]
        assert row["findings"] == ["quote-not-in-source"]
        assert row["source_id"] == "arxiv-7"
        assert isinstance(row["path"], str)
        assert row["path"].endswith("seven.md")

    async def test_the_entrys_own_window_reads_the_same_page(self, migrated: str, tmp_path: Path) -> None:
        knowledge_base = _build_knowledge_base(tmp_path)

        await _compile(knowledge_base, migrated)
        rows = await _work(migrated, knowledge_base, None)

        assert [row["page_id"] for row in rows] == ["paper.seven"]
