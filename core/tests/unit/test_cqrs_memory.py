"""The in-memory adapters keep the same rules the Postgres ones enforce with indexes and locks."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from pydantic import BaseModel

from core.cqrs.contracts import Event, ImmediateWake, JobPolicy, MemoryJobs, MemoryOutbox

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


@pytest.fixture
def clock() -> _Clock:
    return _Clock()


@pytest.fixture
def store(clock: _Clock) -> MemoryJobs:
    return MemoryJobs(clock=clock)


class TestEnqueue:
    async def test_creates_a_queued_job(self, store: MemoryJobs):
        answer = await store.enqueue("kb.refresh", "research", _Payload(knowledge_base="research"), policy=JobPolicy())
        job = await store.read(answer.job_id)
        assert answer.created is True
        assert job is not None
        assert job.state == "queued"
        assert job.payload == {"knowledge_base": "research"}

    async def test_a_live_key_answers_the_existing_job(self, store: MemoryJobs):
        first = await store.enqueue("kb.refresh", "research", _Payload(knowledge_base="research"), policy=JobPolicy())
        second = await store.enqueue("kb.refresh", "research", _Payload(knowledge_base="research"), policy=JobPolicy())
        assert second == second.model_copy(update={"job_id": first.job_id, "created": False})

    async def test_an_empty_key_never_collides(self, store: MemoryJobs):
        first = await store.enqueue("sweep", "", _Payload(knowledge_base=""), policy=JobPolicy())
        second = await store.enqueue("sweep", "", _Payload(knowledge_base=""), policy=JobPolicy())
        assert first.job_id != second.job_id
        assert second.created is True

    async def test_a_finished_key_may_be_queued_again(self, store: MemoryJobs):
        first = await store.enqueue("kb.refresh", "research", _Payload(knowledge_base="research"), policy=JobPolicy())
        claimed = await store.claim(["kb.refresh"], worker="w1")
        assert claimed is not None
        await store.complete(claimed.id, result="ok")
        second = await store.enqueue("kb.refresh", "research", _Payload(knowledge_base="research"), policy=JobPolicy())
        assert second.created is True
        assert second.job_id != first.job_id


class TestClaim:
    async def test_claims_in_available_order_and_leases(self, store: MemoryJobs, clock: _Clock):
        await store.enqueue("a", "1", _Payload(knowledge_base="x"), policy=JobPolicy())
        clock.now = T0 + timedelta(seconds=1)
        await store.enqueue("a", "2", _Payload(knowledge_base="y"), policy=JobPolicy())
        first = await store.claim(["a"], worker="w1")
        second = await store.claim(["a"], worker="w1")
        assert first is not None
        assert first.key == "1"
        assert first.state == "running"
        assert first.attempts == 1
        assert first.locked_by == "w1"
        assert first.locked_at == clock.now
        assert second is not None
        assert second.key == "2"
        assert await store.claim(["a"], worker="w1") is None

    async def test_only_the_served_kinds_are_claimed(self, store: MemoryJobs):
        await store.enqueue("a", "1", _Payload(knowledge_base="x"), policy=JobPolicy())
        assert await store.claim(["b"], worker="w1") is None

    async def test_a_future_available_at_is_not_claimable_yet(self, store: MemoryJobs, clock: _Clock):
        answer = await store.enqueue("a", "1", _Payload(knowledge_base="x"), policy=JobPolicy())
        claimed = await store.claim(["a"], worker="w1")
        assert claimed is not None
        await store.fail(claimed.id, error="boom", retry_at=clock.now + timedelta(seconds=30))
        assert (await store.read(answer.job_id)) is not None
        assert await store.claim(["a"], worker="w1") is None
        clock.now += timedelta(seconds=30)
        retried = await store.claim(["a"], worker="w1")
        assert retried is not None
        assert retried.attempts == 2

    async def test_a_runner_whose_lease_expired_cannot_finish_the_job_another_runner_holds(self, store: MemoryJobs, clock: _Clock):
        await store.enqueue("a", "1", _Payload(knowledge_base="x"), policy=JobPolicy(lease_seconds=60))
        stale = await store.claim(["a"], worker="w1")
        assert stale is not None
        clock.now += timedelta(seconds=61)
        holder = await store.claim(["a"], worker="w2")
        assert holder is not None

        await store.complete(stale.id, result="late", claimed_by="w1")
        await store.fail(stale.id, error="late", retry_at=None, claimed_by="w1")

        current = await store.read(stale.id)
        assert current is not None
        assert (current.state, current.locked_by, current.result) == ("running", "w2", None)
        await store.complete(holder.id, result="done", claimed_by="w2")
        finished = await store.read(holder.id)
        assert finished is not None
        assert finished.state == "done"

    async def test_an_expired_lease_is_reclaimed(self, store: MemoryJobs, clock: _Clock):
        await store.enqueue("a", "1", _Payload(knowledge_base="x"), policy=JobPolicy(lease_seconds=60))
        first = await store.claim(["a"], worker="w1")
        assert first is not None
        assert await store.claim(["a"], worker="w2") is None
        clock.now += timedelta(seconds=61)
        taken_over = await store.claim(["a"], worker="w2")
        assert taken_over is not None
        assert taken_over.id == first.id
        assert taken_over.locked_by == "w2"
        assert taken_over.attempts == 2

    async def test_a_heartbeat_keeps_the_lease(self, store: MemoryJobs, clock: _Clock):
        await store.enqueue("a", "1", _Payload(knowledge_base="x"), policy=JobPolicy(lease_seconds=60))
        first = await store.claim(["a"], worker="w1")
        assert first is not None
        clock.now += timedelta(seconds=50)
        await store.heartbeat(first.id)
        clock.now += timedelta(seconds=50)
        assert await store.claim(["a"], worker="w2") is None


class TestFinish:
    async def test_complete_marks_done_with_a_result(self, store: MemoryJobs, clock: _Clock):
        await store.enqueue("a", "1", _Payload(knowledge_base="x"), policy=JobPolicy())
        job = await store.claim(["a"], worker="w1")
        assert job is not None
        await store.complete(job.id, result="3 pages")
        done = await store.read(job.id)
        assert done is not None
        assert done.state == "done"
        assert done.result == "3 pages"
        assert done.finished_at == clock.now
        assert done.locked_by is None

    async def test_fail_without_a_retry_parks(self, store: MemoryJobs):
        await store.enqueue("a", "1", _Payload(knowledge_base="x"), policy=JobPolicy())
        job = await store.claim(["a"], worker="w1")
        assert job is not None
        await store.fail(job.id, error="ValueError: no", retry_at=None)
        abandoned = await store.read(job.id)
        assert abandoned is not None
        assert abandoned.state == "abandoned"
        assert abandoned.last_error == "ValueError: no"
        assert await store.claim(["a"], worker="w1") is None

    async def test_the_ledger_is_newest_first(self, store: MemoryJobs):
        first = await store.enqueue("a", "1", _Payload(knowledge_base="x"), policy=JobPolicy())
        job = await store.claim(["a"], worker="w1")
        assert job is not None
        await store.complete(job.id, result="ok")
        second = await store.enqueue("a", "1", _Payload(knowledge_base="x"), policy=JobPolicy())
        assert [j.id for j in await store.recent_jobs("a", "1")] == [second.job_id, first.job_id]


class TestMemoryOutbox:
    async def test_append_stores_undelivered_rows_in_order(self):
        outbox = MemoryOutbox()
        first, second = _Happened(knowledge_base="a"), _Happened(knowledge_base="b")
        await outbox.append([first, second])
        rows = await outbox.undelivered()
        assert [r.event_id for r in rows] == [first.event_id, second.event_id]
        assert rows[0].event_type == "_Happened"
        assert rows[0].payload["knowledge_base"] == "a"

    async def test_deliveries_are_recorded_once_and_closing_hides_the_row(self):
        outbox = MemoryOutbox()
        event = _Happened()
        await outbox.append([event])
        assert await outbox.delivered(event.event_id, "qa") is False
        await outbox.record_delivery(event.event_id, "qa")
        await outbox.record_delivery(event.event_id, "qa")
        assert await outbox.delivered(event.event_id, "qa") is True
        await outbox.close(event.event_id)
        assert await outbox.undelivered() == []
        assert outbox.closed == {event.event_id}


async def test_the_immediate_wake_returns_at_once_and_records_the_timeout():
    wake = ImmediateWake()
    await wake.wait(30.0)
    assert wake.waits == [30.0]
