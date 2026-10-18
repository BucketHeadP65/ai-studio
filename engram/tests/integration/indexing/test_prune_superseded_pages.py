"""Removing a page another page supersedes, through the full and the incremental index, over a real migrated database.

`kb.pages.superseded_by` names a page through a foreign key with no cascade. A kept page whose `superseded_by` link
names a removed page keeps its row with `superseded_by` unset, and the index reports the link as dangling. A link
written by file name or by title stores the id of the page it names, and the pages repository's prune and delete
unset `superseded_by` on every kept page naming a page they remove. The key is checked at commit, so a link to a page
written in a later insert batch of the same index loads. The database tests skip when Docker is unavailable (the
`migrated` fixture raises `pytest.skip`).
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from core.db import MAX_BIND_PARAMETERS
from sqlalchemy import NullPool, text
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine

from engram.indexing.contracts import (
    FilesystemKnowledgeBaseReader,
    IndexKnowledgeBase,
    IndexKnowledgeBaseHandler,
    SqlAlchemyKnowledgeBaseUnitOfWork,
)
from engram.indexing.domain.models import IndexedPage
from engram.indexing.infrastructure import orm
from engram.indexing.infrastructure.repository import Pages
from engram.init.contracts import scaffold
from engram.kb.contracts import KnowledgeBaseSchema, PageTypeSpec
from engram.testing.fakes.embedding import FakeEmbedding
from engram.testing.fakes.uow import FakeValidationReportSink
from engram.testing.infrastructure.knowledge_base_db import run_downgrade, run_upgrade_head
from engram.testing.knowledge_base_builder import KnowledgeBaseBuilder

if TYPE_CHECKING:
    from pathlib import Path

    from engram.indexing.contracts import IndexReport

_COLLECTION = "superseded-pages"

# The pages one insert statement carries: every page row binds one parameter per `IndexedPage` field.
_PAGES_PER_BATCH = MAX_BIND_PARAMETERS // len(IndexedPage.model_fields)


def _topic(stem: str, *, superseded_by: str | None = None) -> str:
    """A topic page, superseded by the page `superseded_by` names when given."""
    link = f'superseded_by: "[[{superseded_by}]]"\n' if superseded_by is not None else ""
    return f"---\nid: topic.{stem}\ntype: topic\ntitle: {stem.title()} Topic\nstatus: draft\n{link}---\n"


def _spec() -> KnowledgeBaseSchema:
    """Topic pages and nothing else."""
    return KnowledgeBaseSchema(slug=_COLLECTION, page_types=(PageTypeSpec(page_type="topic", id_prefix="topic."),))


def _scaffold(root: Path, spec: KnowledgeBaseSchema) -> KnowledgeBaseBuilder:
    """Write the schema, the scaffolded vocabulary and the orientation stubs, answering the builder for the pages."""
    builder = KnowledgeBaseBuilder(root)
    builder.add_meta_stubs()
    builder.add_knowledge_base_schema(spec)
    for relative, body in scaffold(spec).items():
        if relative.startswith("_meta/vocabulary/"):
            (root / relative).parent.mkdir(parents=True, exist_ok=True)
            (root / relative).write_text(body, encoding="utf-8")
    return builder


def _knowledge_base(root: Path, spec: KnowledgeBaseSchema, *, alpha_superseded_by: str) -> dict[str, Path]:
    """Write `alpha`, superseded by the page `alpha_superseded_by` names, and `beta`, answering each page's file by id."""
    builder = _scaffold(root, spec)
    return {
        "topic.alpha": builder.add_unit_document("topic", "alpha", _topic("alpha", superseded_by=alpha_superseded_by)),
        "topic.beta": builder.add_unit_document("topic", "beta", _topic("beta")),
    }


async def _index(knowledge_base: Path, migrated: str, spec: KnowledgeBaseSchema, *, full: bool) -> IndexReport:
    """Run one index of `knowledge_base` into the migrated database, a full rebuild when `full`."""
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
        return await handler.handle(IndexKnowledgeBase(collection=_COLLECTION, embed=False, full=full))
    finally:
        await engine.dispose()


async def _pages(migrated: str) -> list[tuple[str, str]]:
    """Every topic page of the collection with its `superseded_by`, empty when unset, sorted by id."""
    engine = create_async_engine(migrated, poolclass=NullPool)
    try:
        async with engine.connect() as conn:
            result = await conn.execute(
                text(
                    "SELECT id, coalesce(superseded_by, '') FROM kb.pages WHERE collection = :collection AND id LIKE 'topic.%' ORDER BY id"
                ),
                {"collection": _COLLECTION},
            )
            return [(str(row[0]), str(row[1])) for row in result.all()]
    finally:
        await engine.dispose()


def _dangling(report: IndexReport) -> list[tuple[str, str]]:
    """The page id and message of every `dangling-link` warning the run reported."""
    return [(issue.page_id, issue.message) for issue in report.validation.warnings if issue.rule == "dangling-link"]


class TestRemovingTheSupersedingTarget:
    @pytest.mark.parametrize("full", [True, False], ids=["full", "incremental"])
    async def test_the_index_removes_the_target_and_unsets_the_kept_page_s_superseded_by(
        self, tmp_path: Path, migrated: str, full: bool
    ) -> None:
        spec = _spec()
        files = _knowledge_base(tmp_path, spec, alpha_superseded_by="topic.beta")
        await _index(tmp_path, migrated, spec, full=True)
        assert await _pages(migrated) == [("topic.alpha", "topic.beta"), ("topic.beta", "")]

        files["topic.beta"].unlink()
        report = await _index(tmp_path, migrated, spec, full=full)

        assert report.mode == ("full" if full else "incremental")
        assert await _pages(migrated) == [("topic.alpha", "")]
        assert _dangling(report) == [("topic.alpha", "unresolved superseded_by link 'topic.beta'")]


class TestASupersededByLinkByFileNameOrTitle:
    @pytest.mark.parametrize("written", ["beta", "Beta Topic"], ids=["file-name", "title"])
    async def test_superseded_by_stores_the_id_of_the_page_the_link_names(self, tmp_path: Path, migrated: str, written: str) -> None:
        spec = _spec()
        _knowledge_base(tmp_path, spec, alpha_superseded_by=written)

        await _index(tmp_path, migrated, spec, full=True)

        assert await _pages(migrated) == [("topic.alpha", "topic.beta"), ("topic.beta", "")]


class TestASupersededByLinkToAPageNotYetWritten:
    async def test_superseded_by_stays_unset_until_an_incremental_index_adds_the_page(self, tmp_path: Path, migrated: str) -> None:
        spec = _spec()
        _knowledge_base(tmp_path, spec, alpha_superseded_by="gamma")
        first = await _index(tmp_path, migrated, spec, full=True)
        assert await _pages(migrated) == [("topic.alpha", ""), ("topic.beta", "")]
        assert _dangling(first) == [("topic.alpha", "unresolved superseded_by link 'gamma'")]

        KnowledgeBaseBuilder(tmp_path).add_unit_document("topic", "gamma", _topic("gamma"))
        report = await _index(tmp_path, migrated, spec, full=False)

        assert report.mode == "incremental"
        assert await _pages(migrated) == [("topic.alpha", "topic.gamma"), ("topic.beta", ""), ("topic.gamma", "")]


class TestASupersededByLinkAcrossInsertBatches:
    async def test_a_link_to_a_page_first_written_in_a_later_batch_loads(self, tmp_path: Path, migrated: str) -> None:
        spec = _spec()
        builder = _scaffold(tmp_path, spec)
        # Pages load in path order, so `early` lands in the first insert batch and `late` in the second.
        builder.add_unit_document("topic", "early", _topic("early", superseded_by="late"))
        for number in range(_PAGES_PER_BATCH):
            builder.add_unit_document("topic", f"filler-{number:05d}", _topic(f"filler-{number:05d}"))
        builder.add_unit_document("topic", "late", _topic("late"))

        await _index(tmp_path, migrated, spec, full=True)

        pages = dict(await _pages(migrated))
        assert len(pages) == _PAGES_PER_BATCH + 2
        assert pages["topic.early"] == "topic.late"


async def _superseded_by_key(migrated: str) -> tuple[bool, bool]:
    """Whether the `superseded_by` key is deferrable and whether it is checked at commit by default."""
    engine = create_async_engine(migrated, poolclass=NullPool)
    try:
        async with engine.connect() as conn:
            result = await conn.execute(
                text(
                    "SELECT condeferrable, condeferred FROM pg_constraint"
                    " WHERE conname = 'pages_collection_superseded_by_fkey' AND conrelid = 'kb.pages'::regclass"
                )
            )
            deferrable, deferred = result.one()
            return bool(deferrable), bool(deferred)
    finally:
        await engine.dispose()


class TestTheSupersededByKey:
    async def test_head_checks_the_key_at_commit(self, migrated: str) -> None:
        assert await _superseded_by_key(migrated) == (True, True)

    async def test_the_downgrade_restores_the_key_checked_at_each_statement(self, migrated: str) -> None:
        run_downgrade(migrated, "0066")
        assert await _superseded_by_key(migrated) == (False, False)

        run_upgrade_head(migrated)
        assert await _superseded_by_key(migrated) == (True, True)


def _page_row(page_id: str, *, superseded_by: str | None = None) -> dict[str, object]:
    """A `kb.pages` upsert row honouring every NOT NULL column."""
    return {
        "id": page_id,
        "collection": _COLLECTION,
        "page_type": "topic",
        "slug": page_id.split(".", 1)[-1],
        "title": page_id,
        "status": "draft",
        "superseded_by": superseded_by,
        "card_text": f"topic: {page_id}",
        "body_hash": f"h-{page_id}",
    }


async def _seed_and_remove(migrated: str, *, prune: bool) -> list[tuple[str, str]]:
    """Store `topic.beta` and `topic.alpha` superseded by it, remove `topic.beta` through the repository, and answer the pages."""
    engine = create_async_engine(migrated, poolclass=NullPool)
    await orm.register_pgvector(engine)
    try:
        async with AsyncSession(engine, expire_on_commit=False) as db:
            pages = Pages(db)
            await pages.upsert_many([_page_row("topic.beta")])
            await pages.upsert_many([_page_row("topic.alpha", superseded_by="topic.beta")])
            if prune:
                await pages.prune_missing({"topic.alpha"}, collection=_COLLECTION)
            else:
                await pages.delete_by_ids({"topic.beta"}, collection=_COLLECTION)
            await db.commit()
    finally:
        await engine.dispose()
    return await _pages(migrated)


class TestThePagesRepositoryRemovingASupersedingTarget:
    @pytest.mark.parametrize("prune", [True, False], ids=["prune", "delete"])
    async def test_the_kept_page_stays_with_superseded_by_unset(self, migrated: str, prune: bool) -> None:
        assert await _seed_and_remove(migrated, prune=prune) == [("topic.alpha", "")]
