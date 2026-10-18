"""The worker claims jobs, delivers outbox events once per subscriber, and runs periodics on time."""

from __future__ import annotations

import asyncio
import logging

from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Self

import pytest

from pydantic import BaseModel

from core.cqrs.contracts import (
    CoordinationBuffer,
    Event,
    ImmediateWake,
    Job,
    JobContext,
    JobPolicy,
    MemoryJobs,
    MemoryOutbox,
    MessageBus,
    Worker,
)
from tests.support.fakes.bus_spy import BusSpy
from tests.support.fakes.wake import BlockingWake

if TYPE_CHECKING:
    from collections.abc import Callable
    from uuid import UUID

    from core.cqrs.contracts import UnitOfWork

T0 = datetime(2026, 8, 26, 12, 0, tzinfo=UTC)


class _Clock:
    def __init__(self) -> None:
        self.now = T0

    def __call__(self) -> datetime:
        return self.now


class _Payload(BaseModel):
    knowledge_base: str


class _Happened(Event):
    knowledge_base: str = "research"


class _Recorder:
    """A handler that records what it ran and answers a scripted outcome."""

    def __init__(self, *, fail_times: int = 0) -> None:
        self.ran: list[tuple[_Payload, JobContext]] = []
        self.fail_times = fail_times

    async def __call__(self, payload: _Payload, context: JobContext) -> str:
        self.ran.append((payload, context))
        if len(self.ran) <= self.fail_times:
            msg = f"attempt {context.attempt} failed"
            raise RuntimeError(msg)
        return f"ingested {payload.knowledge_base}"


class _Transitions:
    def __init__(self) -> None:
        self.seen: list[tuple[int, str]] = []

    def transitioned(self, job: Job) -> None:
        self.seen.append((job.id, job.state))


class _LoudTransitions(_Transitions):
    def transitioned(self, job: Job) -> None:
        super().transitioned(job)
        msg = "the observer broke"
        raise RuntimeError(msg)


class _WritingUow:
    """A unit of work whose commit is observable, for the transactional path."""

    def __init__(self, log: list[str]) -> None:
        self.log = log
        self.coordination = CoordinationBuffer()
        self.committed = False

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(self, *exc: object) -> None:
        del exc
        if not self.committed:
            self.log.append("rolled back")

    async def commit(self) -> None:
        self.committed = True
        self.log.append(f"committed with {len(self.coordination.deliveries)} deliveries")

    def record(self, *events: Event) -> None:
        self.coordination.record(*events)

    def record_delivery(self, event_id: UUID, subscriber: str) -> None:
        self.coordination.record_delivery(event_id, subscriber)


class _TransactionalSpy:
    def __init__(self, log: list[str]) -> None:
        self.log = log
        self.uow_factory: Callable[[], UnitOfWork] = lambda: _WritingUow(log)

    async def __call__(self, event: Event, uow: object) -> None:
        del uow
        self.log.append(f"handled {type(event).__name__}")


class _Failing:
    async def __call__(self, event: Event) -> None:
        msg = f"cannot handle {type(event).__name__}"
        raise RuntimeError(msg)


class _RaisingWake:
    """A wake-up that fails once, then blocks forever, for exercising `run_forever`'s error path.

    Blocking on the second call (instead of failing again) keeps a regression bounded: a discarded
    first exception spins `run_forever` into an unbounded loop of instantly-failing waits, which
    `asyncio.wait_for`'s own timeout cannot reliably interrupt.
    """

    def __init__(self) -> None:
        self.calls = 0

    async def wait(self, seconds: float) -> None:
        del seconds
        self.calls += 1
        if self.calls == 1:
            msg = "wake failed"
            raise RuntimeError(msg)
        await asyncio.Event().wait()


class _OnceFailingWake:
    """A wake-up whose first call raises `OSError`, then blocks forever.

    Blocking after the first failure keeps a retry test bounded: a wake that kept raising would
    have `run_forever` retry it indefinitely.
    """

    def __init__(self) -> None:
        self.calls = 0

    async def wait(self, seconds: float) -> None:
        del seconds
        self.calls += 1
        if self.calls == 1:
            msg = "connection refused"
            raise OSError(msg)
        await asyncio.Event().wait()


@pytest.fixture
def clock() -> _Clock:
    return _Clock()


def _worker(clock: _Clock, bus: MessageBus | None = None, *, observer: _Transitions | None = None) -> tuple[Worker, MemoryJobs, MemoryOutbox]:
    jobs = MemoryJobs(clock=clock)
    bus = bus or MessageBus({}, {})
    outbox = bus.outbox
    assert isinstance(outbox, MemoryOutbox)
    worker = Worker(jobs=jobs, outbox=outbox, bus=bus, wake=ImmediateWake(), name="w1", clock=clock, observer=observer)
    return worker, jobs, outbox


class TestJobs:
    async def test_runs_a_claimed_job_and_completes_it(self, clock: _Clock):
        worker, jobs, _ = _worker(clock)
        handler = _Recorder()
        worker.serve("kb.refresh", _Payload, handler)
        answer = await jobs.enqueue("kb.refresh", "research", _Payload(knowledge_base="research"), policy=JobPolicy())
        result = await worker.tick()
        done = await jobs.read(answer.job_id)
        assert result.jobs == 1
        assert handler.ran == [(_Payload(knowledge_base="research"), JobContext(job_id=answer.job_id, kind="kb.refresh", key="research", attempt=1))]
        assert done is not None
        assert done.state == "done"
        assert done.result == "ingested research"

    async def test_a_raising_handler_is_retried_with_backoff_then_abandoned(self, clock: _Clock):
        worker, jobs, _ = _worker(clock)
        handler = _Recorder(fail_times=5)
        policy = JobPolicy(max_attempts=2, backoff_base_seconds=30.0)
        worker.serve("kb.refresh", _Payload, handler, policy=policy)
        answer = await jobs.enqueue("kb.refresh", "research", _Payload(knowledge_base="research"), policy=policy)
        await worker.tick()
        failed = await jobs.read(answer.job_id)
        assert failed is not None
        assert failed.state == "failed"
        assert failed.last_error == "RuntimeError: attempt 1 failed"
        assert failed.available_at == clock.now + timedelta(seconds=30)
        assert (await worker.tick()).jobs == 0
        clock.now += timedelta(seconds=30)
        await worker.tick()
        abandoned = await jobs.read(answer.job_id)
        assert abandoned is not None
        assert abandoned.state == "abandoned"
        assert len(handler.ran) == 2

    async def test_a_payload_that_does_not_validate_is_abandoned_without_running(self, clock: _Clock):
        worker, jobs, _ = _worker(clock)
        handler = _Recorder()
        worker.serve("kb.refresh", _Payload, handler)

        class _Wrong(BaseModel):
            other: int = 1

        answer = await jobs.enqueue("kb.refresh", "research", _Wrong(), policy=JobPolicy())
        await worker.tick()
        abandoned = await jobs.read(answer.job_id)
        assert abandoned is not None
        assert abandoned.state == "abandoned"
        assert "knowledge_base" in (abandoned.last_error or "")
        assert handler.ran == []

    async def test_a_job_of_an_unserved_kind_is_left_alone(self, clock: _Clock):
        worker, jobs, _ = _worker(clock)
        worker.serve("a", _Payload, _Recorder())
        answer = await jobs.enqueue("b", "1", _Payload(knowledge_base="x"), policy=JobPolicy())
        await worker.tick()
        left = await jobs.read(answer.job_id)
        assert left is not None
        assert left.state == "queued"

    async def test_a_reclaimed_job_past_its_attempts_is_abandoned(self, clock: _Clock):
        worker, jobs, _ = _worker(clock)
        handler = _Recorder()
        policy = JobPolicy(max_attempts=1, lease_seconds=60)
        worker.serve("a", _Payload, handler, policy=policy)
        answer = await jobs.enqueue("a", "1", _Payload(knowledge_base="x"), policy=policy)
        stuck = await jobs.claim(["a"], worker="dead-worker")
        assert stuck is not None
        clock.now += timedelta(seconds=61)
        await worker.tick()
        abandoned = await jobs.read(answer.job_id)
        assert abandoned is not None
        assert abandoned.state == "abandoned"
        assert handler.ran == []

    async def test_the_observer_sees_every_transition(self, clock: _Clock):
        observer = _Transitions()
        worker, jobs, _ = _worker(clock, observer=observer)
        worker.serve("a", _Payload, _Recorder())
        answer = await jobs.enqueue("a", "1", _Payload(knowledge_base="x"), policy=JobPolicy())
        await worker.tick()
        assert observer.seen == [(answer.job_id, "running"), (answer.job_id, "done")]

    async def test_a_raising_observer_does_not_change_the_outcome(self, clock: _Clock, caplog: pytest.LogCaptureFixture):
        observer = _LoudTransitions()
        worker, jobs, _ = _worker(clock, observer=observer)
        worker.serve("a", _Payload, _Recorder())
        answer = await jobs.enqueue("a", "1", _Payload(knowledge_base="x"), policy=JobPolicy())
        await worker.tick()
        job = await jobs.read(answer.job_id)
        assert job is not None
        assert job.state == "done"
        assert observer.seen == [(answer.job_id, "running"), (answer.job_id, "done")]
        assert "the observer broke" in caplog.text

    async def test_the_heartbeat_renews_the_lease_while_a_job_runs(self, clock: _Clock):
        # The heartbeat sleeps on a real timer, not the fake clock, so this test waits in real
        # time for it to fire: the fake clock only stamps `locked_at` at the moment a beat runs.
        worker, jobs, _ = _worker(clock)
        beats: list[datetime] = []

        class _Slow:
            async def __call__(self, payload: _Payload, context: JobContext) -> str:
                del payload, context
                for _ in range(2):
                    await asyncio.sleep(0.5)
                    clock.now += timedelta(seconds=1)
                    job = await jobs.read(1)
                    assert job is not None
                    assert job.locked_at is not None
                    beats.append(job.locked_at)
                return "slow"

        worker.serve("a", _Payload, _Slow(), policy=JobPolicy(lease_seconds=1))
        await jobs.enqueue("a", "1", _Payload(knowledge_base="x"), policy=JobPolicy(lease_seconds=1))
        await worker.tick()
        assert beats[-1] > beats[0]


class TestDelivery:
    async def test_delivers_each_event_to_each_subscriber_once_and_closes(self, clock: _Clock):
        spy = BusSpy()
        bus = MessageBus({}, {_Happened: [spy]})
        worker, _, outbox = _worker(clock, bus)
        event = _Happened()
        await outbox.append([event])
        assert await worker.deliver_once() == 1
        assert await worker.deliver_once() == 0
        assert spy.received == [event]
        assert outbox.closed == {event.event_id}
        assert (event.event_id, "BusSpy") in outbox.deliveries

    async def test_a_recorded_delivery_is_not_repeated(self, clock: _Clock):
        spy = BusSpy()
        bus = MessageBus({}, {_Happened: [spy]})
        worker, _, outbox = _worker(clock, bus)
        event = _Happened()
        await outbox.append([event])
        await outbox.record_delivery(event.event_id, "BusSpy")
        await worker.deliver_once()
        assert spy.received == []
        assert outbox.closed == {event.event_id}

    async def test_a_failing_subscriber_is_isolated_and_retried_next_pass(self, clock: _Clock):
        spy = BusSpy()
        bus = MessageBus({}, {_Happened: [_Failing(), spy]})
        worker, _, outbox = _worker(clock, bus)
        event = _Happened()
        await outbox.append([event])
        assert await worker.deliver_once() == 0
        assert spy.received == [event]
        assert outbox.closed == set()
        assert await worker.deliver_once() == 0
        assert spy.received == [event]

    async def test_a_transactional_subscriber_commits_its_delivery_with_its_effect(self, clock: _Clock):
        log: list[str] = []
        bus = MessageBus({}, {_Happened: [_TransactionalSpy(log)]})
        worker, _, outbox = _worker(clock, bus)
        await outbox.append([_Happened()])
        await worker.deliver_once()
        assert log == ["handled _Happened", "committed with 1 deliveries"]
        assert outbox.deliveries == set()

    async def test_an_event_nobody_subscribes_to_is_closed(self, clock: _Clock, caplog: pytest.LogCaptureFixture):
        bus = MessageBus({}, {_Happened: []})
        worker, _, outbox = _worker(clock, bus)
        event = _Happened()
        await outbox.append([event])
        with caplog.at_level(logging.DEBUG):
            assert await worker.deliver_once() == 1
        assert outbox.closed == {event.event_id}
        assert all(record.levelno < logging.WARNING for record in caplog.records)
        assert "_Happened" in caplog.text

    async def test_an_unknown_wire_name_is_closed_with_a_warning(self, clock: _Clock, caplog: pytest.LogCaptureFixture):
        bus = MessageBus({}, {_Happened: [BusSpy()]})
        worker, _, outbox = _worker(clock, bus)
        renamed = _Happened()
        await outbox.append([renamed])
        outbox.rows[0] = outbox.rows[0].model_copy(update={"event_type": "Vanished"})
        await worker.deliver_once()
        assert outbox.closed == {renamed.event_id}
        assert "Vanished" in caplog.text

    async def test_a_payload_that_does_not_validate_is_skipped_and_later_rows_still_deliver(
        self, clock: _Clock, caplog: pytest.LogCaptureFixture
    ):
        spy = BusSpy()
        bus = MessageBus({}, {_Happened: [spy]})
        worker, _, outbox = _worker(clock, bus)
        bad, good = _Happened(knowledge_base="bad"), _Happened(knowledge_base="good")
        await outbox.append([bad, good])
        outbox.rows[0] = outbox.rows[0].model_copy(update={"payload": {**outbox.rows[0].payload, "knowledge_base": None}})
        assert await worker.deliver_once() == 1
        assert spy.received == [good]
        assert outbox.closed == {good.event_id}
        assert str(bad.event_id) in caplog.text
        assert await worker.deliver_once() == 0
        assert outbox.closed == {good.event_id}


class TestPeriodics:
    async def test_runs_on_the_cadence(self, clock: _Clock):
        worker, _, _ = _worker(clock)
        runs: list[datetime] = []

        async def tick() -> None:
            runs.append(clock.now)

        worker.every(15, tick, name="tick")
        assert (await worker.tick()).periodics == 1
        assert (await worker.tick()).periodics == 0
        clock.now += timedelta(seconds=15)
        assert (await worker.tick()).periodics == 1
        assert runs == [T0, T0 + timedelta(seconds=15)]

    async def test_a_raising_periodic_does_not_stop_the_pass(self, clock: _Clock, caplog: pytest.LogCaptureFixture):
        worker, _, _ = _worker(clock)
        ran: list[str] = []

        async def bad() -> None:
            msg = "no"
            raise RuntimeError(msg)

        async def good() -> None:
            ran.append("good")

        worker.every(1, bad, name="bad")
        worker.every(1, good, name="good")
        await worker.tick()
        assert ran == ["good"]
        assert "bad" in caplog.text


class TestRunForever:
    async def test_cancelling_an_idle_worker_unwinds_its_wake_and_stop_waits(self) -> None:
        wake = BlockingWake()
        worker = Worker(jobs=MemoryJobs(), outbox=MemoryOutbox(), bus=MessageBus({}, {}), wake=wake, name="cancel")
        before = asyncio.all_tasks()
        running = asyncio.create_task(worker.run_forever(stop=asyncio.Event()))
        try:
            async with asyncio.timeout(2):
                await wake.entered.wait()
            running.cancel()
            with pytest.raises(asyncio.CancelledError):
                await running
            assert wake.exited.is_set()
            assert asyncio.all_tasks() - before == set()
        finally:
            remaining = asyncio.all_tasks() - before
            for task in remaining:
                task.cancel()
            await asyncio.gather(*remaining, return_exceptions=True)

    async def test_waits_for_the_earliest_periodic_and_stops(self, clock: _Clock):
        worker, _, _ = _worker(clock)
        wake = ImmediateWake()
        worker = Worker(jobs=MemoryJobs(clock=clock), outbox=MemoryOutbox(), bus=MessageBus({}, {}), wake=wake, name="w1", clock=clock, poll_seconds=30)
        stop = asyncio.Event()
        passes = 0

        async def tick() -> None:
            nonlocal passes
            passes += 1
            clock.now += timedelta(seconds=15)
            if passes == 3:
                stop.set()

        worker.every(15, tick, name="tick")
        await asyncio.wait_for(worker.run_forever(stop=stop), timeout=2)
        assert passes == 3
        assert wake.waits
        assert all(w <= 15 for w in wake.waits)

    async def test_a_failing_wake_propagates_and_leaves_no_pending_task(self, clock: _Clock):
        worker = Worker(jobs=MemoryJobs(clock=clock), outbox=MemoryOutbox(), bus=MessageBus({}, {}), wake=_RaisingWake(), name="w1", clock=clock, poll_seconds=30)
        stop = asyncio.Event()
        before = asyncio.all_tasks()
        with pytest.raises(RuntimeError, match="wake failed"):
            await asyncio.wait_for(worker.run_forever(stop=stop), timeout=2)
        assert asyncio.all_tasks() - before == set()

    async def test_an_oserror_from_the_wake_is_logged_and_the_loop_goes_on(self, clock: _Clock, caplog: pytest.LogCaptureFixture):
        wake = _OnceFailingWake()
        worker = Worker(jobs=MemoryJobs(clock=clock), outbox=MemoryOutbox(), bus=MessageBus({}, {}), wake=wake, name="w1", clock=clock, poll_seconds=0.01)
        stop = asyncio.Event()
        passes = 0

        async def tick() -> None:
            nonlocal passes
            passes += 1
            clock.now += timedelta(seconds=15)
            if passes == 2:
                stop.set()

        worker.every(15, tick, name="tick")
        await asyncio.wait_for(worker.run_forever(stop=stop), timeout=2)
        assert passes == 2
        assert wake.calls == 2
        assert "wake wait failed" in caplog.text
