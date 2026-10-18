"""Unit tests that the index cuts text with no heading where the chunk planner says, with stored cut plans and filling as the fallback."""

from __future__ import annotations

import asyncio

from dataclasses import replace
from typing import TYPE_CHECKING

from engram.indexing.contracts import (
    DEFAULT_PLANNER_CEILING_TOKENS,
    ChunkSizes,
    CutPlanCounts,
    CutPlanStage,
    FilesystemKnowledgeBaseReader,
    IndexKnowledgeBase,
    IndexKnowledgeBaseHandler,
    ManifestEntry,
    MirrorDoc,
    StoredFingerprint,
    content_hash,
    cut_plan_identity,
    indexer_version,
)
from engram.indexing.domain.manifest import FileType
from engram.indexing.domain.mirror_chunks import chunk_mirror
from engram.indexing.service.row_builder import RowBuilder
from engram.kb.contracts import SectionSummarySpec
from engram.kb.domain.schema import EmbedStrategiesSpec
from engram.testing.fakes.chunk_planner import FakeChunkPlanner, FakeCutPlans
from engram.testing.fakes.embedding import FakeEmbedding
from engram.testing.fakes.uow import FakeKnowledgeBaseUnitOfWork, FakeValidationReportSink
from engram.testing.knowledge_base_builder import KnowledgeBaseBuilder
from engram.testing.specs import research_spec

if TYPE_CHECKING:
    from pathlib import Path

    from core.ai.retrieval.contracts import Row

    from engram.kb.contracts import KnowledgeBaseSchema

_SIZES = ChunkSizes(target_tokens=40, max_tokens=80)

_PARAGRAPHS = [f"Paragraph {index} records the tide at the harbour wall at dawn." for index in range(12)]

_BODY = "\n\n".join(_PARAGRAPHS)

_PLANNED = ["\n\n".join(_PARAGRAPHS[0:4]), "\n\n".join(_PARAGRAPHS[4:8]), "\n\n".join(_PARAGRAPHS[8:12])]


def _mirror_text(source_key: str, body: str = _BODY) -> str:
    return f"---\nsource_key: {source_key}\nprovider: example-library\nsource_hash: h-{source_key}\n---\n{body}\n"


def _filled(body: str = _BODY) -> list[str]:
    return [chunk.body for chunk in chunk_mirror(body, _SIZES)]


def _stage(
    planner: FakeChunkPlanner,
    plans: FakeCutPlans,
    *,
    ceiling_tokens: int = DEFAULT_PLANNER_CEILING_TOKENS,
    deadline_seconds: float = 5.0,
    concurrency: int = 4,
) -> CutPlanStage:
    return CutPlanStage(planner, plans, plans, ceiling_tokens=ceiling_tokens, deadline_seconds=deadline_seconds, concurrency=concurrency)


def _handler(root: Path, uow: FakeKnowledgeBaseUnitOfWork, stage: CutPlanStage | None, spec: KnowledgeBaseSchema | None = None) -> IndexKnowledgeBaseHandler:
    return IndexKnowledgeBaseHandler(
        FilesystemKnowledgeBaseReader(root),
        FakeEmbedding(),
        lambda: uow,
        FakeValidationReportSink(),
        spec=spec or research_spec(),
        chunk_sizes=_SIZES,
        cut_plan_stage=stage,
    )


def _knowledge_base(root: Path, *source_keys: str) -> None:
    KnowledgeBaseBuilder(root).build_minimal()
    for source_key in source_keys:
        KnowledgeBaseBuilder(root).add_source_mirror(source_key, _mirror_text(source_key))


def _chunk_bodies(uow: FakeKnowledgeBaseUnitOfWork, source_id: str) -> list[str]:
    rows: list[Row] = [row for row in uow.sources.chunks[-1] if row["source_id"] == source_id]
    return [str(row["body"]) for row in rows]


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


def _row_builder() -> RowBuilder:
    spec = research_spec()
    return RowBuilder(
        "research",
        section_summary_spec=SectionSummarySpec(),
        source_key_field=spec.sources.key_field,
        source_hash_field=spec.sources.hash_field,
        date_field="created",
        chunk_sizes=_SIZES,
    )


def test_the_row_builder_cuts_a_mirror_by_its_cut_plans_in_step_with_its_embed_inputs() -> None:
    mirror = replace(MirrorDoc.from_text("Sources/tide-log.md", _mirror_text("tide-log"), "research"), cut_plans={content_hash(_BODY): (5, 9)})
    builder = _row_builder()

    builder.add_source(mirror)

    assert [row["body"] for row in builder.chunks] == _PLANNED
    assert [row["id"] for row in builder.chunks] == list(builder.embed_inputs)


async def test_a_full_index_cuts_a_mirror_where_the_chunk_planner_says_as_exact_source_text(tmp_path: Path) -> None:
    _knowledge_base(tmp_path, "tide-log")
    planner = FakeChunkPlanner((5, 9))
    uow = FakeKnowledgeBaseUnitOfWork()

    report = await _handler(tmp_path, uow, _stage(planner, FakeCutPlans())).handle(IndexKnowledgeBase(collection="research", embed=False))

    assert report.mode == "full"
    assert _chunk_bodies(uow, "tide-log") == _PLANNED
    assert planner.calls == [_PARAGRAPHS]


async def test_an_answer_that_is_not_strictly_increasing_paragraph_numbers_falls_back_to_filling(tmp_path: Path) -> None:
    _knowledge_base(tmp_path, "tide-log")
    plans = FakeCutPlans()
    uow = FakeKnowledgeBaseUnitOfWork()

    await _handler(tmp_path, uow, _stage(FakeChunkPlanner((9, 5)), plans)).handle(IndexKnowledgeBase(collection="research", embed=False))

    assert _chunk_bodies(uow, "tide-log") == _filled()
    assert plans.stored == {}


async def test_an_accepted_cut_plan_is_stored_under_the_knowledge_base_and_the_planner_identity(tmp_path: Path) -> None:
    _knowledge_base(tmp_path, "tide-log")
    planner = FakeChunkPlanner((5, 9), identity="example-model/prompt-1")
    plans = FakeCutPlans()

    await _handler(tmp_path, FakeKnowledgeBaseUnitOfWork(), _stage(planner, plans)).handle(IndexKnowledgeBase(collection="research", embed=False))

    assert plans.stored == {("research", cut_plan_identity("example-model/prompt-1"), content_hash(_BODY)): (5, 9)}


async def test_a_stored_cut_plan_is_reused_without_calling_the_planner(tmp_path: Path) -> None:
    _knowledge_base(tmp_path, "tide-log")
    planner = FakeChunkPlanner((2, 3), identity="example-model/prompt-1")
    plans = FakeCutPlans()
    plans.stored[("research", cut_plan_identity("example-model/prompt-1"), content_hash(_BODY))] = (5, 9)
    uow = FakeKnowledgeBaseUnitOfWork()

    await _handler(tmp_path, uow, _stage(planner, plans)).handle(IndexKnowledgeBase(collection="research", embed=False))

    assert planner.calls == []
    assert _chunk_bodies(uow, "tide-log") == _PLANNED


async def test_indexing_the_same_text_again_calls_the_planner_once(tmp_path: Path) -> None:
    _knowledge_base(tmp_path, "tide-log")
    planner = FakeChunkPlanner((5, 9))
    plans = FakeCutPlans()

    for _ in range(2):
        uow = FakeKnowledgeBaseUnitOfWork()
        await _handler(tmp_path, uow, _stage(planner, plans)).handle(IndexKnowledgeBase(collection="research", full=True, embed=False))
        assert _chunk_bodies(uow, "tide-log") == _PLANNED

    assert len(planner.calls) == 1


async def test_a_stored_cut_plan_of_another_planner_is_not_reused(tmp_path: Path) -> None:
    _knowledge_base(tmp_path, "tide-log")
    planner = FakeChunkPlanner((5, 9), identity="example-model/prompt-2")
    plans = FakeCutPlans()
    plans.stored[("research", cut_plan_identity("example-model/prompt-1"), content_hash(_BODY))] = (2, 3)
    uow = FakeKnowledgeBaseUnitOfWork()

    await _handler(tmp_path, uow, _stage(planner, plans)).handle(IndexKnowledgeBase(collection="research", embed=False))

    assert len(planner.calls) == 1
    assert _chunk_bodies(uow, "tide-log") == _PLANNED


async def test_text_over_the_planner_ceiling_never_reaches_the_planner(tmp_path: Path) -> None:
    _knowledge_base(tmp_path, "tide-log")
    planner = FakeChunkPlanner((5, 9))
    uow = FakeKnowledgeBaseUnitOfWork()

    await _handler(tmp_path, uow, _stage(planner, FakeCutPlans(), ceiling_tokens=100)).handle(IndexKnowledgeBase(collection="research", embed=False))

    assert planner.calls == []
    assert _chunk_bodies(uow, "tide-log") == _filled()


async def test_a_planner_that_raises_leaves_the_index_complete(tmp_path: Path) -> None:
    _knowledge_base(tmp_path, "tide-log", "wind-log")
    planner = FakeChunkPlanner((5, 9), error=RuntimeError("model unavailable"))
    plans = FakeCutPlans()
    uow = FakeKnowledgeBaseUnitOfWork()

    report = await _handler(tmp_path, uow, _stage(planner, plans)).handle(IndexKnowledgeBase(collection="research", embed=False))

    assert report.mode == "full"
    assert _chunk_bodies(uow, "tide-log") == _filled()
    assert _chunk_bodies(uow, "wind-log") == _filled()
    assert plans.stored == {}
    assert uow.fingerprint.fingerprint is not None


async def test_a_late_planner_leaves_the_index_complete(tmp_path: Path) -> None:
    _knowledge_base(tmp_path, "tide-log")
    planner = FakeChunkPlanner((5, 9), never_answers=True)
    plans = FakeCutPlans()
    uow = FakeKnowledgeBaseUnitOfWork()
    stage = _stage(planner, plans, deadline_seconds=0.01)

    report = await asyncio.wait_for(_handler(tmp_path, uow, stage).handle(IndexKnowledgeBase(collection="research", embed=False)), timeout=5)

    assert report.mode == "full"
    assert _chunk_bodies(uow, "tide-log") == _filled()
    assert plans.stored == {}


async def test_no_more_runs_than_the_concurrency_are_shown_to_the_planner_at_once(tmp_path: Path) -> None:
    keys = [f"log-{index}" for index in range(6)]
    KnowledgeBaseBuilder(tmp_path).build_minimal()
    for key in keys:
        body = "\n\n".join(f"{paragraph} The {key} copy." for paragraph in _PARAGRAPHS)
        KnowledgeBaseBuilder(tmp_path).add_source_mirror(key, _mirror_text(key, body))
    planner = FakeChunkPlanner((5, 9), pause_seconds=0.01)

    await _handler(tmp_path, FakeKnowledgeBaseUnitOfWork(), _stage(planner, FakeCutPlans(), concurrency=2)).handle(
        IndexKnowledgeBase(collection="research", embed=False)
    )

    assert len(planner.calls) == len(keys)
    assert planner.most_at_once == 2


async def test_a_knowledge_base_that_stores_no_chunks_calls_no_planner(tmp_path: Path) -> None:
    _knowledge_base(tmp_path, "tide-log")
    planner = FakeChunkPlanner((5, 9))
    spec = research_spec().model_copy(update={"embed_strategies": EmbedStrategiesSpec(active=("page-card", "section"))})

    await _handler(tmp_path, FakeKnowledgeBaseUnitOfWork(), _stage(planner, FakeCutPlans()), spec).handle(
        IndexKnowledgeBase(collection="research", embed=False)
    )

    assert planner.calls == []


async def test_a_knowledge_base_that_stores_no_chunks_still_stores_the_version_of_its_chunk_planner(tmp_path: Path) -> None:
    _knowledge_base(tmp_path, "tide-log")
    planner = FakeChunkPlanner((5, 9), identity="example-model/prompt-1")
    spec = research_spec().model_copy(update={"embed_strategies": EmbedStrategiesSpec(active=("page-card", "section"))})
    uow = FakeKnowledgeBaseUnitOfWork()

    await _handler(tmp_path, uow, _stage(planner, FakeCutPlans()), spec).handle(IndexKnowledgeBase(collection="research", embed=False))

    written = uow.fingerprint.fingerprint
    assert written is not None
    assert written["indexer_version"] == indexer_version(_SIZES, planner=cut_plan_identity("example-model/prompt-1"))


async def test_without_a_chunk_planner_the_text_is_filled(tmp_path: Path) -> None:
    _knowledge_base(tmp_path, "tide-log")
    uow = FakeKnowledgeBaseUnitOfWork()

    await _handler(tmp_path, uow, None).handle(IndexKnowledgeBase(collection="research", embed=False))

    assert _chunk_bodies(uow, "tide-log") == _filled()


async def test_an_incremental_index_cuts_a_new_mirror_where_the_chunk_planner_says(tmp_path: Path) -> None:
    _knowledge_base(tmp_path, "tide-log")
    planner = FakeChunkPlanner((5, 9))
    uow = FakeKnowledgeBaseUnitOfWork()
    handler = _handler(tmp_path, uow, _stage(planner, FakeCutPlans()))
    await handler.handle(IndexKnowledgeBase(collection="research", embed=False))
    _seed_stored_state(uow)
    handler.set_clean_page_view(list)
    body = "\n\n".join(paragraph.replace("tide", "wind") for paragraph in _PARAGRAPHS)
    KnowledgeBaseBuilder(tmp_path).add_source_mirror("wind-log", _mirror_text("wind-log", body))

    report = await handler.handle(IndexKnowledgeBase(collection="research", embed=False))

    assert report.mode == "incremental"
    wind = [paragraph.replace("tide", "wind") for paragraph in _PARAGRAPHS]
    assert _chunk_bodies(uow, "wind-log") == ["\n\n".join(wind[0:4]), "\n\n".join(wind[4:8]), "\n\n".join(wind[8:12])]
    assert len(planner.calls) == 2


async def test_the_report_counts_the_reused_and_the_chosen_cut_plans(tmp_path: Path) -> None:
    _knowledge_base(tmp_path, "tide-log")
    wind = "\n\n".join(paragraph.replace("tide", "wind") for paragraph in _PARAGRAPHS)
    KnowledgeBaseBuilder(tmp_path).add_source_mirror("wind-log", _mirror_text("wind-log", wind))
    plans = FakeCutPlans()
    plans.stored[("research", cut_plan_identity("example-model/prompt-1"), content_hash(_BODY))] = (5, 9)
    planner = FakeChunkPlanner((5, 9), identity="example-model/prompt-1")

    report = await _handler(tmp_path, FakeKnowledgeBaseUnitOfWork(), _stage(planner, plans)).handle(IndexKnowledgeBase(collection="research", embed=False))

    assert report.cut_plans == CutPlanCounts(reused=1, chosen=1, filled=0)


async def test_the_report_counts_the_heading_sections_filled_after_a_failed_planner_call(tmp_path: Path) -> None:
    _knowledge_base(tmp_path, "tide-log", "wind-log")
    planner = FakeChunkPlanner((5, 9), error=RuntimeError("model unavailable"))

    report = await _handler(tmp_path, FakeKnowledgeBaseUnitOfWork(), _stage(planner, FakeCutPlans())).handle(
        IndexKnowledgeBase(collection="research", embed=False)
    )

    assert report.cut_plans == CutPlanCounts(reused=0, chosen=0, filled=1)


async def test_the_report_counts_a_refused_answer_as_filled(tmp_path: Path) -> None:
    _knowledge_base(tmp_path, "tide-log")

    report = await _handler(tmp_path, FakeKnowledgeBaseUnitOfWork(), _stage(FakeChunkPlanner((9, 5)), FakeCutPlans())).handle(
        IndexKnowledgeBase(collection="research", embed=False)
    )

    assert report.cut_plans == CutPlanCounts(reused=0, chosen=0, filled=1)


async def test_an_incremental_report_counts_the_cut_plans_of_its_changed_mirrors(tmp_path: Path) -> None:
    _knowledge_base(tmp_path, "tide-log")
    uow = FakeKnowledgeBaseUnitOfWork()
    handler = _handler(tmp_path, uow, _stage(FakeChunkPlanner((5, 9)), FakeCutPlans()))
    await handler.handle(IndexKnowledgeBase(collection="research", embed=False))
    _seed_stored_state(uow)
    handler.set_clean_page_view(list)
    wind = "\n\n".join(paragraph.replace("tide", "wind") for paragraph in _PARAGRAPHS)
    KnowledgeBaseBuilder(tmp_path).add_source_mirror("wind-log", _mirror_text("wind-log", wind))

    report = await handler.handle(IndexKnowledgeBase(collection="research", embed=False))

    assert report.mode == "incremental"
    assert report.cut_plans == CutPlanCounts(reused=0, chosen=1, filled=0)


async def test_an_index_with_no_chunk_planner_reports_no_cut_plans(tmp_path: Path) -> None:
    _knowledge_base(tmp_path, "tide-log")

    report = await _handler(tmp_path, FakeKnowledgeBaseUnitOfWork(), None).handle(IndexKnowledgeBase(collection="research", embed=False))

    assert report.cut_plans == CutPlanCounts()


def test_the_stored_indexer_version_names_the_chunk_planner_and_its_ceiling() -> None:
    plain = indexer_version(_SIZES)

    assert indexer_version(_SIZES, planner=None) == plain
    assert indexer_version(_SIZES, planner="example-model/prompt-1") != plain
    assert indexer_version(_SIZES, planner="example-model/prompt-1") != indexer_version(_SIZES, planner="example-model/prompt-2")
    assert indexer_version(_SIZES, planner="example-model/prompt-1") != indexer_version(
        _SIZES, planner="example-model/prompt-1", planner_ceiling_tokens=DEFAULT_PLANNER_CEILING_TOKENS // 2
    )
    assert indexer_version(_SIZES, planner="example-model/prompt-1").startswith(plain)


async def test_the_handler_stores_the_indexer_version_of_its_chunk_planner(tmp_path: Path) -> None:
    _knowledge_base(tmp_path, "tide-log")
    planner = FakeChunkPlanner((5, 9), identity="example-model/prompt-1")
    uow = FakeKnowledgeBaseUnitOfWork()

    await _handler(tmp_path, uow, _stage(planner, FakeCutPlans(), ceiling_tokens=9_000)).handle(IndexKnowledgeBase(collection="research", embed=False))

    written = uow.fingerprint.fingerprint
    assert written is not None
    assert written["indexer_version"] == indexer_version(_SIZES, planner=cut_plan_identity("example-model/prompt-1"), planner_ceiling_tokens=9_000)


async def test_a_chunk_planner_change_makes_the_next_plain_index_a_full_one(tmp_path: Path) -> None:
    _knowledge_base(tmp_path, "tide-log")
    plans = FakeCutPlans()
    uow = FakeKnowledgeBaseUnitOfWork()
    await _handler(tmp_path, uow, _stage(FakeChunkPlanner((5, 9), identity="example-model/prompt-1"), plans)).handle(
        IndexKnowledgeBase(collection="research", embed=False)
    )
    _seed_stored_state(uow)
    handler = _handler(tmp_path, uow, _stage(FakeChunkPlanner((4, 7, 10), identity="example-model/prompt-2"), plans))
    handler.set_clean_page_view(list)

    report = await handler.handle(IndexKnowledgeBase(collection="research", embed=False))

    assert report.mode == "full"
    assert _chunk_bodies(uow, "tide-log") == ["\n\n".join(_PARAGRAPHS[first : first + 3]) for first in (0, 3, 6, 9)]


async def test_an_unchanged_chunk_planner_keeps_the_next_plain_index_incremental(tmp_path: Path) -> None:
    _knowledge_base(tmp_path, "tide-log")
    plans = FakeCutPlans()
    uow = FakeKnowledgeBaseUnitOfWork()
    handler = _handler(tmp_path, uow, _stage(FakeChunkPlanner((5, 9)), plans))
    await handler.handle(IndexKnowledgeBase(collection="research", embed=False))
    _seed_stored_state(uow)
    handler.set_clean_page_view(list)

    report = await handler.handle(IndexKnowledgeBase(collection="research", embed=False))

    assert report.mode == "incremental"
