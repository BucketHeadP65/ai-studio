"""The coordination records and the retry arithmetic."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import uuid4

from core.cqrs.contracts import CoordinationBuffer, Event, JobPolicy, UnitOfWork
from core.testing.fakes import FakeUnitOfWork

NOW = datetime(2026, 8, 26, 12, 0, tzinfo=UTC)


class _Happened(Event):
    pass


class TestJobPolicy:
    def test_backoff_doubles_from_the_base(self):
        policy = JobPolicy(max_attempts=5, backoff_base_seconds=30.0, backoff_cap_seconds=900.0)
        assert policy.retry_at(1, NOW) == NOW + timedelta(seconds=30)
        assert policy.retry_at(2, NOW) == NOW + timedelta(seconds=60)
        assert policy.retry_at(3, NOW) == NOW + timedelta(seconds=120)

    def test_backoff_is_capped(self):
        policy = JobPolicy(max_attempts=20, backoff_base_seconds=30.0, backoff_cap_seconds=100.0)
        assert policy.retry_at(10, NOW) == NOW + timedelta(seconds=100)

    def test_spent_attempts_mean_no_retry(self):
        policy = JobPolicy(max_attempts=3)
        assert policy.retry_at(3, NOW) is None
        assert policy.retry_at(4, NOW) is None

    def test_the_rows_max_attempts_wins_over_the_policy(self):
        policy = JobPolicy(max_attempts=3)
        assert policy.retry_at(3, NOW, max_attempts=5) is not None


class TestCoordinationBuffer:
    def test_records_events_and_deliveries_until_cleared(self):
        buffer = CoordinationBuffer()
        event = _Happened()
        buffer.record(event)
        buffer.record_delivery(event.event_id, "qa")
        assert buffer.events == [event]
        assert buffer.deliveries == [(event.event_id, "qa")]
        buffer.clear()
        assert buffer.events == []
        assert buffer.deliveries == []


class TestFakeUnitOfWorkBuffers:
    def test_satisfies_the_port_and_buffers_records(self):
        uow = FakeUnitOfWork()
        assert isinstance(uow, UnitOfWork)
        event = _Happened()
        uow.record(event)
        uow.record_delivery(uuid4(), "qa")
        assert uow.coordination.events == [event]
        assert len(uow.coordination.deliveries) == 1
