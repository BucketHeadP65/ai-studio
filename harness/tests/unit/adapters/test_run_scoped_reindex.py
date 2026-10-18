"""`RunScopedReindex`: a `once_per_run` knowledge base written inside a run is indexed once, when the run ends."""

from __future__ import annotations

import asyncio

from typing import TYPE_CHECKING, Literal

import pytest

from engram.authoring.contracts import AuthoringKnowledgeBase, ReindexFinding
from engram.testing.fakes.authoring import FakeReindex
from engram.testing.specs import research_spec

from harness.adapters.knowledge_base import RunScopedReindex, hold_run_writes
from tests.support.fakes.reindex import FakeEndingAwareReindex, FakeRefusingReindex

if TYPE_CHECKING:
    from pathlib import Path


def _knowledge_base(tmp_path: Path, name: str, indexing: Literal["each_write", "once_per_run"] = "once_per_run") -> AuthoringKnowledgeBase:
    """A knowledge base named `name` whose schema asks for `indexing`."""
    schema = research_spec().model_copy(update={"run_indexing": indexing})
    return AuthoringKnowledgeBase(name=name, collection=name, path=tmp_path / name, schema=schema)


def _names(inner: FakeReindex) -> list[str]:
    return [knowledge_base.name for knowledge_base in inner.knowledge_bases]


async def test_ten_writes_in_one_run_index_nothing_until_the_run_ends_and_then_once(tmp_path: Path) -> None:
    inner = FakeReindex()
    reindex = RunScopedReindex(inner)
    knowledge_base = _knowledge_base(tmp_path, "bench")

    with hold_run_writes("run-a") as held:
        results = [await reindex(knowledge_base) for _ in range(10)]
        assert _names(inner) == []
        await held.flush()

    assert {result.outcome for result in results} == {"deferred"}
    assert _names(inner) == ["bench"]


async def test_the_run_end_indexes_each_written_knowledge_base_once(tmp_path: Path) -> None:
    inner = FakeReindex()
    reindex = RunScopedReindex(inner)
    first, second = _knowledge_base(tmp_path, "first"), _knowledge_base(tmp_path, "second")

    with hold_run_writes("run-a") as held:
        for knowledge_base in (first, second, first, second, first):
            await reindex(knowledge_base)
        await held.flush()

    assert sorted(_names(inner)) == ["first", "second"]


async def test_a_write_from_a_task_the_run_spawns_is_held_and_indexed_at_the_end(tmp_path: Path) -> None:
    inner = FakeReindex()
    reindex = RunScopedReindex(inner)
    knowledge_base = _knowledge_base(tmp_path, "bench")

    with hold_run_writes("run-a") as held:
        result = await asyncio.create_task(reindex(knowledge_base))
        assert _names(inner) == []
        await held.flush()

    assert result.outcome == "deferred"
    assert _names(inner) == ["bench"]


async def test_a_write_outside_any_run_indexes_at_once(tmp_path: Path) -> None:
    inner = FakeReindex()

    result = await RunScopedReindex(inner)(_knowledge_base(tmp_path, "bench"))

    assert result.outcome == "clean"
    assert _names(inner) == ["bench"]


async def test_a_write_to_an_each_write_knowledge_base_indexes_at_once_inside_a_run(tmp_path: Path) -> None:
    inner = FakeReindex()
    reindex = RunScopedReindex(inner)

    with hold_run_writes("run-a") as held:
        result = await reindex(_knowledge_base(tmp_path, "research", "each_write"))
        assert _names(inner) == ["research"]
        await held.flush()

    assert result.outcome == "clean"
    assert _names(inner) == ["research"]


async def test_two_runs_at_once_flush_only_their_own_knowledge_bases(tmp_path: Path) -> None:
    inner = FakeReindex()
    reindex = RunScopedReindex(inner)
    first, second = _knowledge_base(tmp_path, "first"), _knowledge_base(tmp_path, "second")
    first_wrote, second_wrote, first_flushed = asyncio.Event(), asyncio.Event(), asyncio.Event()

    async def first_run() -> None:
        with hold_run_writes("run-a") as held:
            await reindex(first)
            first_wrote.set()
            await second_wrote.wait()
            await held.flush()
            first_flushed.set()

    async def second_run() -> list[str]:
        with hold_run_writes("run-b") as held:
            await first_wrote.wait()
            await reindex(second)
            second_wrote.set()
            await first_flushed.wait()
            indexed_when_the_first_ended = _names(inner)
            await held.flush()
        return indexed_when_the_first_ended

    _, indexed_when_the_first_ended = await asyncio.gather(first_run(), second_run())

    assert indexed_when_the_first_ended == ["first"]
    assert _names(inner) == ["first", "second"]


async def test_a_write_after_the_run_end_began_indexes_at_once(tmp_path: Path) -> None:
    inner = FakeReindex()
    reindex = RunScopedReindex(inner)

    with hold_run_writes("run-a") as held:
        await held.flush()
        result = await reindex(_knowledge_base(tmp_path, "bench"))

    assert result.outcome == "clean"
    assert _names(inner) == ["bench"]


async def test_one_failed_index_at_the_run_end_does_not_stop_the_others(tmp_path: Path) -> None:
    inner = FakeRefusingReindex(refused="first")
    reindex = RunScopedReindex(inner)

    with hold_run_writes("run-a") as held:
        await reindex(_knowledge_base(tmp_path, "first"))
        await reindex(_knowledge_base(tmp_path, "second"))
        await held.flush()

    assert inner.indexed == ["second"]


async def test_an_index_that_did_not_run_at_the_run_end_is_logged_as_a_warning(tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    inner = FakeReindex(outcome="not_indexed", detail="studio index for bench failed with exit 2")
    reindex = RunScopedReindex(inner)

    with caplog.at_level("INFO"), hold_run_writes("run-a") as held:
        await reindex(_knowledge_base(tmp_path, "bench"))
        await held.flush()

    warnings = [record.getMessage() for record in caplog.records if record.levelname == "WARNING"]
    assert warnings == ["run run-a could not index bench at its end: studio index for bench failed with exit 2"]
    assert not any("indexed bench" in record.getMessage() for record in caplog.records)


async def test_validation_errors_at_the_run_end_are_logged_with_their_count(tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    finding = ReindexFinding(page_id="paper.one", rule="missing-quote", message="claim quote absent from its mirror")
    inner = FakeReindex(outcome="validation_errors", findings=(finding,), findings_total=3)
    reindex = RunScopedReindex(inner)

    with caplog.at_level("INFO"), hold_run_writes("run-a") as held:
        await reindex(_knowledge_base(tmp_path, "bench"))
        await held.flush()

    warnings = [record.getMessage() for record in caplog.records if record.levelname == "WARNING"]
    assert warnings == ["run run-a indexed bench at its end with 3 validation errors"]


async def test_a_cancel_during_the_run_end_index_finishes_every_held_knowledge_base_and_then_cancels(tmp_path: Path) -> None:
    release = asyncio.Event()
    inner = FakeEndingAwareReindex(release=release)
    reindex = RunScopedReindex(inner)

    with hold_run_writes("run-a") as held:
        await reindex(_knowledge_base(tmp_path, "first"))
        await reindex(_knowledge_base(tmp_path, "second"))
        flushing = asyncio.create_task(held.flush())
        await inner.entered.wait()
        flushing.cancel()
        await asyncio.sleep(0)
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await flushing

    assert inner.indexed == ["first", "second"]
