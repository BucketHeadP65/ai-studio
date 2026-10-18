"""Integration tests for the stored cut plans and the chunk planner's place in a real index, on a freshly migrated database.

Skips the whole module when Docker is unavailable.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from sqlalchemy import NullPool, text
from sqlalchemy.ext.asyncio import create_async_engine

from engram.indexing.contracts import (
    ChunkSizes,
    CutPlanStage,
    FilesystemKnowledgeBaseReader,
    IndexKnowledgeBase,
    IndexKnowledgeBaseHandler,
    SqlAlchemyKnowledgeBaseUnitOfWork,
    content_hash,
    cut_plan_identity,
)
from engram.indexing.infrastructure import orm
from engram.indexing.infrastructure.cut_plans import SqlAlchemyCutPlans
from engram.testing.fakes.chunk_planner import FakeChunkPlanner
from engram.testing.fakes.embedding import FakeEmbedding
from engram.testing.fakes.uow import FakeValidationReportSink
from engram.testing.knowledge_base_builder import KnowledgeBaseBuilder
from engram.testing.specs import research_spec

if TYPE_CHECKING:
    from pathlib import Path

_SIZES = ChunkSizes(target_tokens=40, max_tokens=80)

_PARAGRAPHS = [f"Paragraph {index} records the tide at the harbour wall at dawn." for index in range(12)]

_BODY = "\n\n".join(_PARAGRAPHS)


async def test_a_stored_plan_is_read_back_only_for_its_knowledge_base_and_planner(migrated: str) -> None:
    engine = create_async_engine(migrated, poolclass=NullPool)
    plans = SqlAlchemyCutPlans(engine)
    try:
        await plans.insert("tides", "example-model/prompt-1", {"h1": (5, 9), "h2": ()})

        assert await plans.cut_plans("tides", "example-model/prompt-1", ["h1", "h2", "h3"]) == {"h1": (5, 9), "h2": ()}
        assert await plans.cut_plans("winds", "example-model/prompt-1", ["h1"]) == {}
        assert await plans.cut_plans("tides", "example-model/prompt-2", ["h1"]) == {}
    finally:
        await engine.dispose()


async def test_a_plan_already_stored_is_kept(migrated: str) -> None:
    engine = create_async_engine(migrated, poolclass=NullPool)
    plans = SqlAlchemyCutPlans(engine)
    try:
        await plans.insert("tides", "example-model/prompt-1", {"h1": (5, 9)})
        await plans.insert("tides", "example-model/prompt-1", {"h1": (2,), "h2": (3,)})

        assert await plans.cut_plans("tides", "example-model/prompt-1", ["h1", "h2"]) == {"h1": (5, 9), "h2": (3,)}
    finally:
        await engine.dispose()


async def test_a_second_full_index_reuses_the_stored_plan_and_writes_the_same_chunks(migrated: str, tmp_path: Path) -> None:
    KnowledgeBaseBuilder(tmp_path).build_minimal()
    KnowledgeBaseBuilder(tmp_path).add_source_mirror(
        "tide-log", f"---\nsource_key: tide-log\nprovider: example-library\nsource_hash: h-tide\n---\n{_BODY}\n"
    )
    engine = create_async_engine(migrated, poolclass=NullPool)
    await orm.register_pgvector(engine)
    planner = FakeChunkPlanner((5, 9), identity="example-model/prompt-1")
    stage = CutPlanStage(planner, SqlAlchemyCutPlans(engine), SqlAlchemyCutPlans(engine))
    handler = IndexKnowledgeBaseHandler(
        FilesystemKnowledgeBaseReader(tmp_path),
        FakeEmbedding(),
        lambda: SqlAlchemyKnowledgeBaseUnitOfWork(engine),
        FakeValidationReportSink(),
        spec=research_spec(),
        chunk_sizes=_SIZES,
        cut_plan_stage=stage,
    )
    try:
        bodies: list[list[str]] = []
        for _ in range(2):
            await handler.handle(IndexKnowledgeBase(collection="research", full=True, embed=False))
            async with engine.connect() as conn:
                rows = await conn.execute(text("SELECT body FROM kb.raw_chunks WHERE collection = 'research' AND source_id = 'tide-log' ORDER BY seq"))
                bodies.append([str(row.body) for row in rows])
        async with engine.connect() as conn:
            stored = (await conn.execute(text("SELECT text_hash, planner, starts FROM kb.cut_plans WHERE collection = 'research'"))).all()

        assert bodies[0] == bodies[1] == ["\n\n".join(_PARAGRAPHS[0:4]), "\n\n".join(_PARAGRAPHS[4:8]), "\n\n".join(_PARAGRAPHS[8:12])]
        assert [tuple(row) for row in stored] == [(content_hash(_BODY), cut_plan_identity("example-model/prompt-1"), [5, 9])]
        assert len(planner.calls) == 1
    finally:
        await engine.dispose()
