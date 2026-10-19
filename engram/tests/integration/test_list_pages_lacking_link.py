"""`list_pages` with `lacking_link` over a real, migrated Postgres, fed by a real index.

Two papers are indexed: one links the other through `builds_on`, the other links nothing. Asked for the pages lacking
`builds_on`, the listing keeps only the page without the edge. Left empty, it lists both.

Skips when Docker is unavailable (the `migrated` fixture raises `pytest.skip`).
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
from engram.sources.contracts import SourceQueue, claimants_for
from engram.testing.fakes.authoring import FakeReindex
from engram.testing.fakes.embedding import FakeEmbedding
from engram.testing.fakes.uow import FakeValidationReportSink
from engram.testing.knowledge_base_builder import KnowledgeBaseBuilder
from engram.testing.specs import research_spec

if TYPE_CHECKING:
    from pathlib import Path

_COLLECTION = "research"

_FACETS = """---
id: meta.vocabulary-facets
type: meta
title: "Vocabulary: facets"
summary: The paper types these fixtures use.
status: verified
---

## `paper_type` on papers (single value)

| Value | Meaning |
|---|---|
| `benchmark` | A benchmark |
"""


def _paper(slug: str, builds_on: str) -> str:
    """A raw paper page, linking `builds_on` when it is not empty."""
    link = f'builds_on: ["[[{builds_on}]]"]\n' if builds_on else ""
    return f"""---
id: paper.{slug}
type: paper
title: {slug.title()}
summary: A paper about {slug}.
status: active
paper_type: benchmark
year: 2024
zotero_key: key-{slug}
source_hash: h-{slug}
{link}---

## TL;DR

{slug.title()}.
"""


def _build_knowledge_base(tmp_path: Path) -> Path:
    """A fixture knowledge base: `paper.later` builds on `paper.earlier`, which links nothing."""
    builder = KnowledgeBaseBuilder(tmp_path)
    builder.build_minimal()
    (tmp_path / "_meta" / "vocabulary").mkdir(parents=True, exist_ok=True)
    (tmp_path / "_meta" / "vocabulary" / "facets.md").write_text(_FACETS, encoding="utf-8")
    builder.add_unit_document("Wiki/Papers", "earlier", _paper("earlier", ""))
    builder.add_unit_document("Wiki/Papers", "later", _paper("later", "paper.earlier"))
    return tmp_path


async def _compile(knowledge_base: Path, db_url: str) -> None:
    """Run the real `IndexKnowledgeBaseHandler` (real reader, real UoW, fake embedder) over `knowledge_base`."""
    spec = research_spec()
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


async def _listed(db_url: str, target_path: Path, lacking_link: str) -> list[str]:
    """The page ids `list_pages` answers for the papers, through the production admin gateway over `db_url`."""
    engine = create_async_engine(db_url, poolclass=NullPool)
    await orm.register_pgvector(engine)
    try:
        knowledge_base = AuthoringKnowledgeBase(name="research", collection=_COLLECTION, path=target_path, schema=research_spec())
        doors = AuthoringTools(
            knowledge_bases={"research": knowledge_base},
            reindex=FakeReindex(),
            admin=ReadOnlySqlGateway(engine),
            queue_for=lambda t: SourceQueue(t.path / "_meta" / "source-queue.yaml", connectors=claimants_for(t.schema.sources.connectors, source={})),
            connectors={},
        )
        rows = await doors.list_pages("research", "paper", lacking_link=lacking_link)
        return [row["page_id"] for row in rows]
    finally:
        await engine.dispose()


class TestListPagesLackingALinkAgainstARealCompile:
    async def test_the_page_with_the_edge_is_left_out_and_the_page_without_it_is_kept(self, migrated: str, tmp_path: Path) -> None:
        knowledge_base = _build_knowledge_base(tmp_path)

        await _compile(knowledge_base, migrated)

        assert await _listed(migrated, knowledge_base, "builds_on") == ["paper.earlier"]

    async def test_an_empty_relation_lists_every_page_of_the_type(self, migrated: str, tmp_path: Path) -> None:
        knowledge_base = _build_knowledge_base(tmp_path)

        await _compile(knowledge_base, migrated)

        assert await _listed(migrated, knowledge_base, "") == ["paper.earlier", "paper.later"]

    async def test_an_edge_of_another_relation_does_not_count(self, migrated: str, tmp_path: Path) -> None:
        knowledge_base = _build_knowledge_base(tmp_path)

        await _compile(knowledge_base, migrated)

        assert await _listed(migrated, knowledge_base, "evaluates") == ["paper.earlier", "paper.later"]
