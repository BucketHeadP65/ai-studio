"""A workflow run's ending arms the schedules waiting for its workflow, and a failure there never undoes the ending."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING

import pytest

from core.ai.workflows.contracts import Approval, Outcome, RunEvidence, StepRegistry

from harness.container import create_container
from harness.features.running.records import ObservedEvidenceLog, RunRecords
from harness.features.scheduling.domain import Schedule
from harness.features.scheduling.service import RunEndArming, Scheduler
from harness.session import SessionPool
from harness.settings import HarnessSettings
from tests.support.cards import base_card, base_workflow
from tests.support.container import answering_from_memory, rooted_at
from tests.support.fakes.broadcaster import FakeBroadcaster
from tests.support.fakes.schedules import FakeSchedules
from tests.support.fakes.sessions import APool
from tests.unit.test_schedule_ticker import FakeWorkflowStore, _runtime

if TYPE_CHECKING:
    from pathlib import Path

    from core.ai.workflows.contracts import OutcomeStatus

GATHER = "wf.gather-notes.v1"
SUMMARIZE = "wf.summarize-notes.v1"
T0 = datetime(2026, 10, 6, 12, 0, tzinfo=UTC)


def _ended(workflow_id: str, status: OutcomeStatus | None, base: str = "notes") -> RunEvidence:
    return RunEvidence(
        workflow_id=workflow_id,
        spec_hash="abc",
        outcome="completed",
        result=None if status is None else Outcome(status=status, summary="what the run did"),
        params={"base": base},
    )


def _store() -> FakeWorkflowStore:
    return FakeWorkflowStore(
        specs={GATHER: base_workflow("gather-notes"), SUMMARIZE: base_workflow("summarize-notes")},
        cards={GATHER: base_card(GATHER), SUMMARIZE: base_card(SUMMARIZE)},
    )


class RecordingObserver:
    def __init__(self) -> None:
        self.seen: list[RunEvidence] = []

    async def run_ended(self, evidence: RunEvidence) -> None:
        self.seen.append(evidence)


class RaisingObserver:
    async def run_ended(self, evidence: RunEvidence) -> None:
        del evidence
        msg = "the schedule store is down"
        raise RuntimeError(msg)


async def test_the_log_writes_the_ending_then_tells_the_observer(tmp_path: Path) -> None:
    observer = RecordingObserver()
    log = ObservedEvidenceLog(tmp_path / "evidence.jsonl", observer)
    ending = _ended(GATHER, "succeeded")

    await log.append(ending)

    assert observer.seen == [ending]
    assert list(await log.events_for(GATHER)) == [ending]


async def test_an_observer_that_raises_leaves_the_ending_written_and_raises_nothing(tmp_path: Path) -> None:
    log = ObservedEvidenceLog(tmp_path / "evidence.jsonl", RaisingObserver())
    ending = _ended(GATHER, "succeeded")

    await log.append(ending)

    assert list(await log.events_for(GATHER)) == [ending]


async def test_an_approval_is_written_and_tells_nobody(tmp_path: Path) -> None:
    observer = RecordingObserver()
    log = ObservedEvidenceLog(tmp_path / "evidence.jsonl", observer)

    await log.append(Approval(workflow_id=GATHER, spec_hash="abc", by="owner"))

    assert observer.seen == []


async def test_a_run_that_succeeded_arms_the_schedule_waiting_for_its_workflow_over_the_same_knowledge_base() -> None:
    schedules = FakeSchedules()
    waiting = Schedule(workflow_id=SUMMARIZE, arguments={"base": "notes"}, cadence="after gather-notes")
    elsewhere = Schedule(workflow_id=SUMMARIZE, arguments={"base": "other"}, cadence="after gather-notes")
    another_workflow = Schedule(workflow_id=SUMMARIZE, arguments={"base": "notes"}, cadence="after something-else")
    paused = Schedule(workflow_id=SUMMARIZE, arguments={"base": "notes"}, cadence="after gather-notes", enabled=False)
    on_refresh = Schedule(workflow_id=SUMMARIZE, arguments={"base": "notes"}, cadence="on refresh")
    for row in (waiting, elsewhere, another_workflow, paused, on_refresh):
        await schedules.insert(row)

    armed = await Scheduler(schedules, schedules, FakeBroadcaster()).arm_after_run(_store(), _ended(GATHER, "succeeded"), now=T0)

    assert armed == (waiting.id,)
    stored = await schedules.get(waiting.id)
    assert stored is not None
    assert stored.next_run_at == T0


@pytest.mark.parametrize(("status", "arms"), [("succeeded", True), ("partial", True), ("noop", False), ("failed", False), ("unknown", False), (None, False)])
async def test_only_a_run_that_did_its_work_arms(status: OutcomeStatus | None, arms: bool) -> None:
    schedules = FakeSchedules()
    waiting = Schedule(workflow_id=SUMMARIZE, arguments={"base": "notes"}, cadence="after gather-notes")
    await schedules.insert(waiting)

    armed = await Scheduler(schedules, schedules, FakeBroadcaster()).arm_after_run(_store(), _ended(GATHER, status), now=T0)

    assert (armed == (waiting.id,)) is arms


async def test_a_run_of_a_workflow_the_library_does_not_serve_arms_nothing() -> None:
    schedules = FakeSchedules()
    await schedules.insert(Schedule(workflow_id=SUMMARIZE, arguments={"base": "notes"}, cadence="after gather-notes"))

    armed = await Scheduler(schedules, schedules, FakeBroadcaster()).arm_after_run(_store(), _ended("draft", "succeeded"), now=T0)

    assert armed == ()


async def test_the_process_session_supplies_the_library_when_a_run_ends() -> None:
    store = _store()
    pool = SessionPool(_runtime(StepRegistry(), store))
    schedules = FakeSchedules()
    waiting = Schedule(workflow_id=SUMMARIZE, arguments={"base": "notes"}, cadence="after gather-notes")
    await schedules.insert(waiting)

    await RunEndArming(Scheduler(schedules, schedules, FakeBroadcaster()), APool(pool)).run_ended(_ended(GATHER, "succeeded"))

    stored = await schedules.get(waiting.id)
    assert stored is not None
    assert stored.next_run_at is not None


async def test_the_container_writes_run_evidence_through_the_observed_log(tmp_path: Path) -> None:
    container = create_container(rooted_at(HarnessSettings(), tmp_path))
    try:
        with answering_from_memory(container):
            records = await container.get(RunRecords)
    finally:
        await container.close()

    assert isinstance(records.evidence, ObservedEvidenceLog)
