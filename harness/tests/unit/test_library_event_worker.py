"""The worker over the workflow library's outbox: each new reusable step version arms the on-step-change schedules.

A `MemoryOutbox` stands in for `coordination.outbox` of the library's database and `FakeSchedules` for the schedules.
`test_library_event_worker.py` in the integration tier runs the same worker over Postgres.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING

from core.ai.workflows.contracts import ReusableStepSaved
from core.cqrs.contracts import ImmediateWake, MemoryOutbox

from harness.features.scheduling.contracts import StepChangeArming
from harness.features.scheduling.service import Scheduler
from harness.main import Driving, library_event_worker
from tests.support.fakes.broadcaster import FakeBroadcaster
from tests.support.fakes.schedules import FakeSchedules
from tests.unit.test_schedule_ticker import _schedule

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence
    from uuid import UUID

    from harness.features.scheduling.domain import Armed

T0 = datetime(2026, 11, 5, 12, 0, tzinfo=UTC)


def _saved(step_id: str) -> ReusableStepSaved:
    """The event a new reusable step version raises, naming `step_id`."""
    name, _, version = step_id.removeprefix("st.").rpartition(".v")
    return ReusableStepSaved(step=name, version=int(version), step_id=step_id, step_hash="sha256:0", saved_by="import")


def _driving() -> Driving:
    return Driving(wake=ImmediateWake(), name="harness:test", clock=lambda: T0)


async def test_a_step_save_written_before_the_worker_starts_arms_the_on_step_change_schedule() -> None:
    schedules = FakeSchedules()
    waiting = _schedule(cadence="on step change quiet 10m", next_run_at=None)
    await schedules.insert(waiting)
    outbox = MemoryOutbox()
    event = _saved("st.digest_source.v12")
    await outbox.append([event])
    arming = StepChangeArming(Scheduler(schedules, schedules, FakeBroadcaster()), clock=lambda: T0)

    worked = await library_event_worker(arming, outbox, _driving()).tick()

    assert worked.deliveries == 1
    assert outbox.closed == {event.event_id}
    assert outbox.deliveries == {(event.event_id, "schedules.on_step_change")}
    stored = await schedules.get(waiting.id)
    assert stored is not None
    assert (stored.next_run_at, stored.pending_step_ids) == (T0 + timedelta(minutes=10), ("st.digest_source.v12",))


async def test_two_saves_of_one_step_in_one_pass_leave_one_id_and_one_fire() -> None:
    schedules = FakeSchedules()
    waiting = _schedule(cadence="on step change quiet 10m", next_run_at=None)
    await schedules.insert(waiting)
    outbox = MemoryOutbox()
    await outbox.append([_saved("st.digest_source.v12"), _saved("st.digest_source.v12")])
    arming = StepChangeArming(Scheduler(schedules, schedules, FakeBroadcaster()), clock=lambda: T0)

    await library_event_worker(arming, outbox, _driving()).tick()

    assert await outbox.undelivered() == []
    stored = await schedules.get(waiting.id)
    assert stored is not None
    assert (stored.next_run_at, stored.pending_step_ids) == (T0 + timedelta(minutes=10), ("st.digest_source.v12",))


async def test_a_step_save_with_no_on_step_change_schedule_is_consumed_and_arms_nothing() -> None:
    schedules = FakeSchedules()
    on_index = _schedule(cadence="on index docs", next_run_at=None)
    clock = _schedule(cadence="every 30m", next_run_at=T0 + timedelta(minutes=30))
    for row in (on_index, clock):
        await schedules.insert(row)
    outbox = MemoryOutbox()
    event = _saved("st.digest_source.v12")
    await outbox.append([event])
    arming = StepChangeArming(Scheduler(schedules, schedules, FakeBroadcaster()), clock=lambda: T0)

    worker = library_event_worker(arming, outbox, _driving())
    first = await worker.tick()
    second = await worker.tick()

    assert (first.deliveries, second.deliveries) == (1, 0), "the row is closed once and never read again"
    assert schedules.received_steps == {event.event_id}
    for row in (on_index, clock):
        left = await schedules.get(row.id)
        assert left is not None
        assert left == row, "no schedule changed"


async def test_a_save_that_fails_to_arm_stays_in_the_outbox_for_the_next_pass() -> None:
    schedules = FakeSchedules()
    waiting = _schedule(cadence="on step change", next_run_at=None)
    await schedules.insert(waiting)
    outbox = MemoryOutbox()
    event = _saved("st.a.v2")
    await outbox.append([event])
    failing = StepChangeArming(Scheduler(_RefusingSchedules(), schedules, FakeBroadcaster()), clock=lambda: T0)

    first = await library_event_worker(failing, outbox, _driving()).tick()

    assert first.deliveries == 0
    assert [row.event_id for row in await outbox.undelivered()] == [event.event_id]
    arming = StepChangeArming(Scheduler(schedules, schedules, FakeBroadcaster()), clock=lambda: T0 + timedelta(minutes=1))
    second = await library_event_worker(arming, outbox, _driving()).tick()
    assert second.deliveries == 1
    stored = await schedules.get(waiting.id)
    assert stored is not None
    assert stored.pending_step_ids == ("st.a.v2",)


class _RefusingSchedules(FakeSchedules):
    """A store whose step arm fails, the way one fails while the database is away."""

    async def arm_with_steps(self, fires: Mapping[UUID, datetime], *, event_id: UUID, step_ids: Sequence[str]) -> Armed:
        del fires, event_id, step_ids
        msg = "the schedule store is away"
        raise ConnectionError(msg)
