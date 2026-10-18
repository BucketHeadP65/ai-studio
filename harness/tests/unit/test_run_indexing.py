"""Run indexing on the in-process engine: a run indexes each `once_per_run` knowledge base it wrote once, however it ends.

The index runs before the run's ending is recorded, so whatever reacts to the ending finds the
knowledge base indexed. A run that fails or is paused indexes what it wrote too. A pause or stop that
lands during that index waits for it, and the run's ending is still recorded as it happened.
"""

from __future__ import annotations

import asyncio

from typing import TYPE_CHECKING, Literal

import pytest

from core.ai.workflows.contracts import FromNode, FromParameter, ParameterSpec, RunEvidence, RunFinished, RunNode, StepRegistry, Workflow
from engram.authoring.contracts import AuthoringKnowledgeBase
from engram.testing.specs import research_spec
from pydantic_ai.models.test import TestModel
from pydantic_ai.tools import RunContext
from pydantic_ai.usage import RunUsage

from harness.adapters.knowledge_base import RunScopedReindex
from harness.features.running import service
from harness.features.running.infrastructure import run_events
from harness.features.running.infrastructure.run_log import load_waiting
from harness.features.running.records import RunCollaborators, RunRecords
from harness.features.running.tools import run_workflow
from harness.session import run_log_path
from harness.settings import RunSettings
from tests.support.fakes.broadcaster import FakeBroadcaster
from tests.support.fakes.reindex import FakeEndingAwareReindex
from tests.support.fakes.run_end import FakeYieldingRunEndObserver
from tests.support.runtime import a_runtime
from tests.support.state import KEPT_DAYS, a_state, records_under

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable
    from pathlib import Path

    from harness.session import Runtime

WORKFLOW_ID = "wf.write"
"""The workflow every run here is filed under."""


def _knowledge_base(tmp_path: Path, indexing: Literal["each_write", "once_per_run"] = "once_per_run") -> AuthoringKnowledgeBase:
    """The knowledge base `bench`, whose schema asks for `indexing`."""
    schema = research_spec().model_copy(update={"run_indexing": indexing})
    return AuthoringKnowledgeBase(name="bench", collection="bench", path=tmp_path / "bench", schema=schema)


def _writing_spec() -> Workflow:
    """One gateless step, `write_pages`, that writes into a knowledge base."""
    return Workflow(
        name="write-pages",
        version=1,
        description="write pages",
        parameters={"quarter": ParameterSpec(description="fiscal quarter", json_schema={"type": "string"})},
        nodes=(RunNode(id="write_pages", work="write_pages", inputs={"quarter": FromParameter(parameter="quarter")}),),
        output=FromNode(node="write_pages"),
    )


def _session(tmp_path: Path, write_pages: Callable[[str], Awaitable[str]], inner: FakeEndingAwareReindex) -> Runtime:
    registry = StepRegistry()
    registry.register(write_pages)
    session = a_runtime(registry=registry, state=a_state(tmp_path))
    inner.session = session
    return session


def _launch(session: Runtime, records: RunRecords) -> None:
    service.launch(
        session, records=records, topics=FakeBroadcaster(), spec=_writing_spec(), workflow_id=WORKFLOW_ID, params={"quarter": "Q2"}
    )


async def _ended(session: Runtime) -> None:
    task = session.active.task
    assert task is not None
    await task


async def _cancel_requested(task: asyncio.Task[None]) -> None:
    """Return once someone has asked `task` to cancel."""
    for _ in range(1000):
        if task.cancelling():
            return
        await asyncio.sleep(0)
    msg = "nothing asked the run to cancel"
    raise AssertionError(msg)


async def _wait_out_leftover_tasks() -> None:
    """Wait out the settled run's outcome turn, which runs in a task of its own, so nothing outlives the test."""
    if leftover := asyncio.all_tasks() - {asyncio.current_task()}:
        _, pending = await asyncio.wait(leftover, timeout=5)
        assert not pending


def _finishes(session: Runtime) -> list[RunFinished]:
    recorded = run_events.events_for(session.state, session.active.run_id or "")
    return [entry.event for entry in recorded if isinstance(entry.event, RunFinished)]


async def test_a_completed_run_indexes_its_knowledge_base_once_before_its_ending_is_recorded(tmp_path: Path) -> None:
    inner = FakeEndingAwareReindex()
    reindex = RunScopedReindex(inner)
    knowledge_base = _knowledge_base(tmp_path)

    async def write_pages(quarter: str) -> str:
        for _ in range(10):
            await reindex(knowledge_base)
        return quarter

    session = _session(tmp_path, write_pages, inner)
    _launch(session, records_under(tmp_path))
    await _ended(session)

    assert inner.indexed == ["bench"]
    assert inner.ended_before_index == [False]
    assert _finishes(session) == [RunFinished(run_id=session.active.run_id or "", workflow_id=WORKFLOW_ID, status="completed")]


async def test_a_run_that_raises_still_indexes_what_it_wrote(tmp_path: Path) -> None:
    inner = FakeEndingAwareReindex()
    reindex = RunScopedReindex(inner)
    knowledge_base = _knowledge_base(tmp_path)

    async def write_pages(quarter: str) -> str:
        for _ in range(3):
            await reindex(knowledge_base)
        msg = f"the model dropped the connection in {quarter}"
        raise RuntimeError(msg)

    session = _session(tmp_path, write_pages, inner)
    records = records_under(tmp_path)
    _launch(session, records)
    await _ended(session)

    assert inner.indexed == ["bench"]
    evidence = [event for event in await records.evidence.events_for(WORKFLOW_ID) if isinstance(event, RunEvidence)]
    assert [record.outcome for record in evidence] == ["failed"]


async def test_a_paused_run_indexes_what_it_wrote(tmp_path: Path) -> None:
    inner = FakeEndingAwareReindex()
    reindex = RunScopedReindex(inner)
    knowledge_base = _knowledge_base(tmp_path)
    wrote = asyncio.Event()
    hold = asyncio.Event()

    async def write_pages(quarter: str) -> str:
        await reindex(knowledge_base)
        wrote.set()
        await hold.wait()
        return quarter

    session = _session(tmp_path, write_pages, inner)
    _launch(session, records_under(tmp_path))
    await asyncio.wait_for(wrote.wait(), timeout=5)
    assert inner.indexed == []

    assert await service.pause(session)

    assert inner.indexed == ["bench"]


@pytest.mark.parametrize("writes", [1, 4])
async def test_an_each_write_knowledge_base_indexes_every_write_as_before(tmp_path: Path, writes: int) -> None:
    inner = FakeEndingAwareReindex()
    reindex = RunScopedReindex(inner)
    knowledge_base = _knowledge_base(tmp_path, "each_write")

    async def write_pages(quarter: str) -> str:
        for _ in range(writes):
            await reindex(knowledge_base)
        return quarter

    session = _session(tmp_path, write_pages, inner)
    _launch(session, records_under(tmp_path))
    await _ended(session)

    assert inner.indexed == ["bench"] * writes


async def test_a_run_started_from_chat_indexes_its_knowledge_base_once_before_its_ending_is_recorded(tmp_path: Path) -> None:
    inner = FakeEndingAwareReindex()
    reindex = RunScopedReindex(inner)
    knowledge_base = _knowledge_base(tmp_path)

    async def write_pages(quarter: str) -> str:
        for _ in range(10):
            await reindex(knowledge_base)
        return quarter

    session = _session(tmp_path, write_pages, inner)
    session.conversation.last_workflow = _writing_spec()
    run = RunCollaborators(records=records_under(tmp_path), topics=FakeBroadcaster(), run_settings=RunSettings())

    await run_workflow(RunContext(deps=session, model=TestModel(), usage=RunUsage()), "draft", {"quarter": "Q2"}, run=run)
    settlement = session.active.settlement
    assert settlement is not None
    await settlement
    await _wait_out_leftover_tasks()

    assert inner.indexed == ["bench"]
    assert inner.ended_before_index == [False]


def _records_with_a_yielding_observer(tmp_path: Path) -> RunRecords:
    """Every record a run writes under `tmp_path`, with an evidence log that tells an observer which yields, as production's does."""
    return RunRecords.under(a_state(tmp_path), retention_days=KEPT_DAYS, observer=FakeYieldingRunEndObserver())


def _writing_step(reindex: RunScopedReindex, knowledge_base: AuthoringKnowledgeBase, ending: str) -> Callable[[str], Awaitable[str]]:
    """The `write_pages` step: one write into `knowledge_base`, then a return, or a raise when `ending` is `failed`."""

    async def write_pages(quarter: str) -> str:
        await reindex(knowledge_base)
        if ending == "failed":
            msg = f"the model dropped the connection in {quarter}"
            raise RuntimeError(msg)
        return quarter

    return write_pages


async def _cancel_during_the_end_index(session: Runtime, records: RunRecords, inner: FakeEndingAwareReindex, release: asyncio.Event, cancel: str) -> bool:
    """Pause or stop `session`'s run while its end index is held, then let the index finish, and answer what the pause or stop said."""
    await asyncio.wait_for(inner.entered.wait(), timeout=5)
    task = session.active.task
    assert task is not None
    cancelling = asyncio.create_task(service.pause(session) if cancel == "pause" else service.stop(session, records=records))
    await _cancel_requested(task)
    release.set()
    return await cancelling


async def _assert_one_record_of(session: Runtime, records: RunRecords, workflow_id: str, ending: str) -> None:
    evidence = [event for event in await records.evidence.events_for(workflow_id) if isinstance(event, RunEvidence)]
    assert [record.outcome for record in evidence] == [ending]
    assert [record.outcome for record in records.archive.recent(10)] == [ending]
    assert _finishes(session) == [RunFinished(run_id=session.active.run_id or "", workflow_id=workflow_id, status=ending)]
    assert load_waiting(run_log_path(session)) is None
    assert service.run_phase(session) is None
    assert session.active.result.startswith("Finished." if ending == "completed" else "The run stopped with an error:")


@pytest.mark.parametrize("ending", ["completed", "failed"])
@pytest.mark.parametrize("cancel", ["pause", "stop"])
async def test_a_pause_or_stop_during_the_end_index_finds_the_run_already_ended_and_leaves_one_record(
    tmp_path: Path, cancel: str, ending: str
) -> None:
    release = asyncio.Event()
    inner = FakeEndingAwareReindex(release=release)
    session = _session(tmp_path, _writing_step(RunScopedReindex(inner), _knowledge_base(tmp_path), ending), inner)
    records = _records_with_a_yielding_observer(tmp_path)
    _launch(session, records)

    assert await _cancel_during_the_end_index(session, records, inner, release, cancel) is False

    await _assert_one_record_of(session, records, WORKFLOW_ID, ending)
    assert inner.indexed == ["bench"]


@pytest.mark.parametrize("ending", ["completed", "failed"])
@pytest.mark.parametrize("cancel", ["pause", "stop"])
async def test_a_pause_or_stop_during_the_end_index_of_a_run_started_from_chat_leaves_one_record(
    tmp_path: Path, cancel: str, ending: str
) -> None:
    release = asyncio.Event()
    inner = FakeEndingAwareReindex(release=release)
    session = _session(tmp_path, _writing_step(RunScopedReindex(inner), _knowledge_base(tmp_path), ending), inner)
    session.conversation.last_workflow = _writing_spec()
    records = _records_with_a_yielding_observer(tmp_path)
    run = RunCollaborators(records=records, topics=FakeBroadcaster(), run_settings=RunSettings())
    await run_workflow(RunContext(deps=session, model=TestModel(), usage=RunUsage()), "draft", {"quarter": "Q2"}, run=run)

    assert await _cancel_during_the_end_index(session, records, inner, release, cancel) is False

    settlement = session.active.settlement
    assert settlement is not None
    assert settlement.done(), "the pause or stop returns only once the chat run has recorded its own ending"
    await _wait_out_leftover_tasks()
    await _assert_one_record_of(session, records, "draft", ending)
