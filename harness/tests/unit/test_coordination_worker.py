"""Independent background loops for timers, rules, attribution and scans.

The jobs live in the coordination schema of the sessions database in the real deployment; here a
`MemoryJobs` stands in for it. The outbox and bus stay in memory in both: this app owns no bus
and nothing subscribes. The timer loop runs dispatch, reconciliation and scheduling while each
inspection kind owns a separate worker. Tests drive single passes or hold model work on events.
"""

from __future__ import annotations

import asyncio

from dataclasses import dataclass, field, replace
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING
from uuid import UUID

import pytest

from core.ai.history.contracts import EventAtPositionRow
from core.ai.inspection.contracts import (
    SCAN_RUN_KIND,
    AttributeLead,
    AttributeLeadHandler,
    Dispatch,
    InspectRunHandler,
    JobLanes,
    ScanRun,
    Sweep,
    Watch,
)
from core.ai.transcripts.contracts import TranscriptLoaders
from core.cqrs.contracts import ImmediateWake, MemoryJobs

from harness import main
from harness.features.running.records import RunCollaborators
from harness.features.scheduling.domain import Armed
from harness.features.scheduling.service import Scheduler
from harness.main import ARCHIVE_SWEEP_SECONDS, IDLE_SWEEP_SECONDS, SCHEDULE_TICK_SECONDS, Driving, Inspecting, coordination_worker
from harness.session import SessionPool
from harness.settings import InspectionSettings, RunSettings
from tests.support.fakes.broadcaster import FakeBroadcaster
from tests.support.fakes.inspection import FakeInspectionStore, FakeRecordEvents, FakeRunsOfConversation
from tests.support.fakes.inspection_worker import BlockingReconciliationEvents, FakeWaitingScan
from tests.support.state import records_under

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence
    from pathlib import Path

    from core.ai.transcripts.contracts import Transcript
    from core.cqrs.contracts import JobContext

    from harness.features.scheduling.domain import Schedule, ScheduleUpdate
    from harness.session import Runtime

T0 = datetime(2026, 8, 26, 12, 0, tzinfo=UTC)


class _Clock:
    def __init__(self) -> None:
        self.now = T0

    def __call__(self) -> datetime:
        return self.now


class _Pool(SessionPool):
    """A session pool that only records that it was swept."""

    def __init__(self) -> None:
        self.evictions = 0

    def evict_idle(self, *, older_than_minutes: int = 30) -> None:
        del older_than_minutes
        self.evictions += 1

    def held_sessions(self) -> tuple[Runtime, ...]:
        return ()


class _Store:
    """A schedule store with nothing due. The other `Schedules` methods are never called here."""

    def __init__(self) -> None:
        self.due_calls = 0

    async def insert(self, schedule: Schedule) -> None:
        del schedule

    async def get(self, schedule_id: UUID) -> Schedule | None:
        del schedule_id
        return None

    async def list_all(self) -> list[Schedule]:
        return []

    async def update(self, schedule_id: UUID, changes: ScheduleUpdate) -> bool:
        del schedule_id, changes
        return False

    async def delete(self, schedule_id: UUID) -> bool:
        del schedule_id
        return False

    async def due(self, now: datetime) -> list[Schedule]:
        del now
        self.due_calls += 1
        return []

    async def stamp(
        self,
        schedule_id: UUID,
        *,
        last_run_at: datetime,
        last_outcome: str,
        next_run_at: datetime | None,
        session_id: str,
        cleared_page_ids: Sequence[str],
    ) -> bool:
        del schedule_id, last_run_at, last_outcome, next_run_at, session_id, cleared_page_ids
        return False

    async def arm(self, schedule_ids: Sequence[UUID], *, at: datetime, event_id: UUID | None = None) -> Armed:
        del schedule_ids, at, event_id
        return Armed()

    async def arm_with_pages(self, fires: Mapping[UUID, datetime], *, event_id: UUID, page_ids: Sequence[str]) -> Armed:
        del fires, event_id, page_ids
        return Armed()


@pytest.fixture
def clock() -> _Clock:
    return _Clock()


def _inert_inspecting() -> Inspecting:
    """An `Inspecting` with no watches, on cadences well past every other periodic this loop runs.

    Nothing it dispatches or sweeps ever selects a run, so `TestPeriodics` only has to account for the two extra
    periodics the loop always registers, never for a command the inspection issues.
    """
    jobs = MemoryJobs()
    store = FakeInspectionStore()
    events = FakeRecordEvents()
    loaders = TranscriptLoaders(by_key={})
    lanes = JobLanes(jobs)
    settings = InspectionSettings(dispatch_seconds=1000, sweep_seconds=2000)
    return Inspecting(
        jobs=jobs,
        inspect_run=InspectRunHandler(watches={}, facts=settings.facts(), loaders=loaders, store=store, queue=lanes),
        attribute_lead=AttributeLeadHandler(
            store=store, loaders=loaders, agents=(), conversation=FakeRunsOfConversation(), questions=store
        ),
        scan_run=_RecordingScanRun(answer="scanned"),
        dispatch=Dispatch(watches=(), events=events, cursors=store, lanes=lanes),
        sweep=Sweep(watches=(), events=events, lanes=lanes),
        settings=settings,
    )


class TestPeriodics:
    async def test_every_periodic_runs_on_the_first_pass(self, clock: _Clock, tmp_path: Path) -> None:
        pool, store = _Pool(), _Store()
        worker = coordination_worker(
            pool,
            Scheduler(store, store, FakeBroadcaster()),
            RunCollaborators(records=records_under(tmp_path), topics=FakeBroadcaster(), run_settings=RunSettings()),
            Driving(wake=ImmediateWake(), name="test", clock=clock),
            _inert_inspecting(),
        )
        assert (await worker.tick()).periodics == 4
        assert pool.evictions == 1
        assert store.due_calls == 1

    async def test_the_schedules_tick_is_the_soonest_to_come_round_again(self, clock: _Clock, tmp_path: Path) -> None:
        pool, store = _Pool(), _Store()
        worker = coordination_worker(
            pool,
            Scheduler(store, store, FakeBroadcaster()),
            RunCollaborators(records=records_under(tmp_path), topics=FakeBroadcaster(), run_settings=RunSettings()),
            Driving(wake=ImmediateWake(), name="test", clock=clock),
            _inert_inspecting(),
        )
        await worker.tick()
        assert (await worker.tick()).periodics == 0
        clock.now += timedelta(seconds=SCHEDULE_TICK_SECONDS)
        assert (await worker.tick()).periodics == 1
        assert store.due_calls == 2
        assert pool.evictions == 1

    async def test_the_idle_sweep_waits_its_own_cadence(self, clock: _Clock, tmp_path: Path) -> None:
        pool, store = _Pool(), _Store()
        worker = coordination_worker(
            pool,
            Scheduler(store, store, FakeBroadcaster()),
            RunCollaborators(records=records_under(tmp_path), topics=FakeBroadcaster(), run_settings=RunSettings()),
            Driving(wake=ImmediateWake(), name="test", clock=clock),
            _inert_inspecting(),
        )
        await worker.tick()
        clock.now += timedelta(seconds=IDLE_SWEEP_SECONDS)
        await worker.tick()
        assert pool.evictions == 2

    def test_the_three_cadences_are_the_ones_the_loops_used(self) -> None:
        assert (SCHEDULE_TICK_SECONDS, IDLE_SWEEP_SECONDS, ARCHIVE_SWEEP_SECONDS) == (15, 5 * 60, 24 * 60 * 60)


class _RecordWithoutRuns:
    """A record loader whose record holds no run."""

    async def __call__(self, locator: str) -> Transcript:
        msg = f"no run {locator!r} in the record"
        raise LookupError(msg)


def an_inspecting(jobs: MemoryJobs, events: FakeRecordEvents, store: FakeInspectionStore) -> Inspecting:
    settings = InspectionSettings(watches=(Watch(name="every-run", rules=("duplicate_call",)),), dispatch_seconds=1, sweep_seconds=60)
    loaders = TranscriptLoaders(by_key={"record": _RecordWithoutRuns()})
    lanes = JobLanes(jobs)
    return Inspecting(
        jobs=jobs,
        inspect_run=InspectRunHandler(
            watches={"every-run": settings.watches[0]}, facts=settings.facts(), loaders=loaders, store=store, queue=lanes
        ),
        attribute_lead=AttributeLeadHandler(
            store=store, loaders=loaders, agents=(), conversation=FakeRunsOfConversation(), questions=store
        ),
        scan_run=_RecordingScanRun(answer="scanned"),
        dispatch=Dispatch(watches=settings.watches, events=events, cursors=store, lanes=lanes),
        sweep=Sweep(watches=settings.watches, events=events, lanes=lanes),
        settings=settings,
    )


async def test_the_loop_runs_the_dispatch_and_serves_an_inspect_run_it_issued(tmp_path: Path) -> None:
    jobs = MemoryJobs()
    events = FakeRecordEvents(
        [EventAtPositionRow(id=1, run_id="run-1", kind="run_completed", step_index=0, at=T0, agent_name="a", conversation_id=UUID(int=1))]
    )
    store = FakeInspectionStore()
    collaborators = RunCollaborators(records=records_under(tmp_path), topics=FakeBroadcaster(), run_settings=RunSettings())
    clock = _Clock()
    worker = coordination_worker(
        _Pool(), Scheduler(_Store(), _Store(), FakeBroadcaster()), collaborators, Driving(wake=ImmediateWake(), name="t", clock=clock), an_inspecting(jobs, events, store)
    )

    first = await worker.tick()
    position_after_first = store.positions["inspection.dispatch"]
    events.events.append(
        EventAtPositionRow(id=2, run_id="run-2", kind="run_completed", step_index=0, at=T0, agent_name="a", conversation_id=UUID(int=2))
    )
    clock.now += timedelta(seconds=1)  # the dispatch's own cadence, so its periodic is due again on this pass
    second = await worker.tick()

    assert first.periodics >= 4
    assert position_after_first == 1
    assert second.jobs == 0
    assert store.positions["inspection.dispatch"] == 2
    (job,) = [job for job in jobs.jobs.values() if job.kind == "inspect_run"]
    assert job.key == "every-run:record:run-2"
    rules, _, _ = main.inspection_workers(an_inspecting(jobs, events, store), Driving(wake=ImmediateWake(), name="rules", clock=clock))
    third = await rules.tick()
    assert third.jobs == 1
    assert jobs.jobs[job.id].result == "record:run-2: run gone"


@dataclass
class _RecordingAttributeLead:
    """Stands in for `AttributeLeadHandler`: answers a fixed string, remembers every payload it saw."""

    answer: str
    seen: list[AttributeLead] = field(default_factory=list)

    async def __call__(self, payload: AttributeLead, context: JobContext) -> str:
        del context
        self.seen.append(payload)
        return self.answer


async def test_the_loop_serves_an_attribute_lead() -> None:
    jobs = MemoryJobs()
    store = FakeInspectionStore()
    events = FakeRecordEvents()
    handler = _RecordingAttributeLead(answer="lead 7: decided")
    settings = InspectionSettings(dispatch_seconds=1000, sweep_seconds=2000)
    loaders = TranscriptLoaders(by_key={})
    lanes = JobLanes(jobs)
    inspecting = Inspecting(
        jobs=jobs,
        inspect_run=InspectRunHandler(watches={}, facts=settings.facts(), loaders=loaders, store=store, queue=lanes),
        attribute_lead=handler,
        scan_run=_RecordingScanRun(answer="scanned"),
        dispatch=Dispatch(watches=(), events=events, cursors=store, lanes=lanes),
        sweep=Sweep(watches=(), events=events, lanes=lanes),
        settings=settings,
    )
    _, worker, _ = main.inspection_workers(inspecting, Driving(wake=ImmediateWake(), name="t", clock=_Clock()))

    await lanes.enqueue([7])
    result = await worker.tick()

    assert result.jobs == 1
    assert [payload.lead_id for payload in handler.seen] == [7]
    (job,) = list(jobs.jobs.values())
    assert job.result == "lead 7: decided"


@dataclass
class _RecordingScanRun:
    """Stands in for `ScanRunHandler`: answers a fixed string, remembers every payload it saw."""

    answer: str
    seen: list[ScanRun] = field(default_factory=list)

    async def __call__(self, payload: ScanRun, context: JobContext) -> str:
        del context
        self.seen.append(payload)
        return self.answer


async def test_the_loop_serves_a_scan_run() -> None:
    jobs = MemoryJobs()
    store = FakeInspectionStore()
    events = FakeRecordEvents()
    handler = _RecordingScanRun(answer="record:run-1: scanned, 2 seen, 2 findings")
    settings = InspectionSettings(dispatch_seconds=1000, sweep_seconds=2000)
    loaders = TranscriptLoaders(by_key={})
    lanes = JobLanes(jobs)
    inspecting = Inspecting(
        jobs=jobs,
        inspect_run=InspectRunHandler(watches={}, facts=settings.facts(), loaders=loaders, store=store, queue=lanes),
        attribute_lead=AttributeLeadHandler(
            store=store, loaders=loaders, agents=(), conversation=FakeRunsOfConversation(), questions=store
        ),
        scan_run=handler,
        dispatch=Dispatch(watches=(), events=events, cursors=store, lanes=lanes),
        sweep=Sweep(watches=(), events=events, lanes=lanes),
        settings=settings,
    )
    _, _, worker = main.inspection_workers(inspecting, Driving(wake=ImmediateWake(), name="t", clock=_Clock()))

    await lanes.scan_run("record:run-1", "every-run")
    result = await worker.tick()

    assert result.jobs == 1
    assert [payload.source for payload in handler.seen] == ["record:run-1"]
    (job,) = list(jobs.jobs.values())
    assert job.kind == SCAN_RUN_KIND
    assert job.key == "scan:every-run:record:run-1"
    assert job.result == "record:run-1: scanned, 2 seen, 2 findings"


async def test_a_waiting_scan_cannot_block_rules_dispatch_or_scheduled_workflow_timers(tmp_path: Path) -> None:
    jobs = MemoryJobs()
    events = FakeRecordEvents()
    store = FakeInspectionStore()
    scan = FakeWaitingScan()
    inspecting = replace(an_inspecting(jobs, events, store), scan_run=scan, attribute_lead=_RecordingAttributeLead(answer="decided"))
    clock = _Clock()
    driving = Driving(wake=ImmediateWake(), name="test", clock=clock)
    rules, attribution, scans = main.inspection_workers(inspecting, driving)
    schedule = _Store()
    timers = coordination_worker(
        _Pool(),
        Scheduler(schedule, schedule, FakeBroadcaster()),
        RunCollaborators(records=records_under(tmp_path), topics=FakeBroadcaster(), run_settings=RunSettings()),
        driving,
        inspecting,
    )
    lanes = JobLanes(jobs)
    await lanes.scan_run("record:scan", "every-run")
    waiting = asyncio.create_task(scans.tick())
    try:
        async with asyncio.timeout(2):
            await scan.entered.wait()
            assert (await timers.tick()).jobs == 0
            events.events.append(
                EventAtPositionRow(
                    id=1, run_id="rules", kind="run_completed", step_index=0, at=T0, agent_name="a", conversation_id=UUID(int=1)
                )
            )
            clock.now += timedelta(seconds=SCHEDULE_TICK_SECONDS)
            await timers.tick()
            assert (await rules.tick()).jobs == 1
            await lanes.enqueue([7])
            assert (await attribution.tick()).jobs == 1
        assert schedule.due_calls == 2
        assert store.positions["inspection.dispatch"] == 1
        assert not waiting.done()
        inspected = [job for job in jobs.jobs.values() if job.kind == "inspect_run"]
        assert inspected[0].state == "done"
        assert inspected[0].result == "record:rules: run gone"
    finally:
        waiting.cancel()
        await asyncio.gather(waiting, return_exceptions=True)
    assert scan.stopped.is_set()


async def test_the_inspection_sweep_runs_on_its_own_cadence() -> None:
    """The sweep periodic is gated by `settings.sweep_seconds`, not merely present at every pass."""
    jobs = MemoryJobs()
    watch = Watch(name="every-run", rules=("duplicate_call",), when="sweep", sweep_hours=24)
    settings = InspectionSettings(watches=(watch,), dispatch_seconds=1000, sweep_seconds=60)
    events = FakeRecordEvents(
        [EventAtPositionRow(id=1, run_id="run-1", kind="run_completed", step_index=0, at=T0, agent_name="a", conversation_id=UUID(int=1))]
    )
    store = FakeInspectionStore()
    loaders = TranscriptLoaders(by_key={})
    lanes = JobLanes(jobs)
    clock = _Clock()
    inspecting = Inspecting(
        jobs=jobs,
        inspect_run=InspectRunHandler(watches={"every-run": watch}, facts=settings.facts(), loaders=loaders, store=store, queue=lanes),
        attribute_lead=AttributeLeadHandler(
            store=store, loaders=loaders, agents=(), conversation=FakeRunsOfConversation(), questions=store
        ),
        scan_run=_RecordingScanRun(answer="scanned"),
        dispatch=Dispatch(watches=(watch,), events=events, cursors=store, lanes=lanes),
        sweep=Sweep(watches=(watch,), events=events, lanes=lanes, clock=lambda: clock.now),
        settings=settings,
    )
    worker = main.reconciliation_worker(inspecting, Driving(wake=ImmediateWake(), name="t", clock=clock))

    await worker.tick()
    assert len(jobs.jobs) == 1, "the first pass always runs every periodic, so the sweep already issued run-1"

    events.events.append(
        EventAtPositionRow(id=2, run_id="run-2", kind="run_completed", step_index=0, at=T0, agent_name="a", conversation_id=UUID(int=2))
    )
    assert (await worker.tick()).periodics == 0, "the sweep is not due again yet, so run-2 stays unissued"
    assert len(jobs.jobs) == 1

    clock.now += timedelta(seconds=settings.sweep_seconds)
    await worker.tick()

    assert len(jobs.jobs) == 2, "the sweep's own cadence came round, and it issued run-2"


async def test_a_blocked_large_reconciliation_cannot_delay_schedules_or_dispatch(tmp_path: Path) -> None:
    events = BlockingReconciliationEvents(
        [
            EventAtPositionRow(
                id=index, run_id=f"run-{index}", kind="run_completed", step_index=0, at=T0, agent_name="a", conversation_id=UUID(int=index)
            )
            for index in range(1, 1002)
        ]
    )
    jobs, store, schedule, clock = MemoryJobs(), FakeInspectionStore(), _Store(), _Clock()
    inspecting = an_inspecting(jobs, events, store)
    inspecting = replace(inspecting, sweep=replace(inspecting.sweep, clock=clock))
    driving = Driving(wake=ImmediateWake(), name="independent", clock=clock)
    timers = coordination_worker(
        _Pool(),
        Scheduler(schedule, schedule, FakeBroadcaster()),
        RunCollaborators(records=records_under(tmp_path), topics=FakeBroadcaster(), run_settings=RunSettings()),
        driving,
        inspecting,
    )
    reconcile = main.reconciliation_worker(inspecting, driving)
    waiting = asyncio.create_task(reconcile.tick())
    try:
        async with asyncio.timeout(2):
            await events.entered.wait()
            await timers.tick()
            events.events.append(
                EventAtPositionRow(
                    id=1002, run_id="new-run", kind="run_completed", step_index=0, at=T0, agent_name="a", conversation_id=UUID(int=1002)
                )
            )
            clock.now += timedelta(seconds=SCHEDULE_TICK_SECONDS)
            await timers.tick()
        assert schedule.due_calls == 2
        assert store.positions["inspection.dispatch"] == 1002
        assert not waiting.done()
        events.release.set()
        async with asyncio.timeout(2):
            await waiting
        assert any(job.key == "every-run:record:run-1001" for job in jobs.jobs.values())
    finally:
        waiting.cancel()
        await asyncio.gather(waiting, return_exceptions=True)
