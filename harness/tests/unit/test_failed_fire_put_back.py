"""An on-step-change fire that stopped with a provider error puts its changed steps back on its schedule, once per step."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from core.ai.workflows.contracts import Outcome, RunEvidence

from harness.domain.contracts import FailedFireSteps
from harness.features.scheduling.domain import Schedule
from harness.features.scheduling.service import Scheduler
from tests.support.fakes.broadcaster import FakeBroadcaster
from tests.support.fakes.schedules import FakeSchedules

T0 = datetime(2026, 11, 6, 7, 52, tzinfo=UTC)
FIRED = "st.draft_decision_brief.v2,st.review_decision_brief.v2"
FIRED_IDS = ("st.draft_decision_brief.v2", "st.review_decision_brief.v2")


def _schedule(**overrides: object) -> Schedule:
    defaults: dict[str, object] = {"workflow_id": "wf.step-maintenance.v1", "cadence": "on step change quiet 10m", "next_run_at": None}
    defaults.update(overrides)
    return Schedule(**defaults)  # type: ignore[arg-type]


def _ended(runner: str, *, provider_error: bool = True, changed: str = FIRED, at: datetime = T0) -> RunEvidence:
    return RunEvidence(
        workflow_id="wf.step-maintenance.v1",
        spec_hash="abc",
        outcome="failed",
        result=Outcome(status="unknown", summary="The primary work never took an input, so nothing about it can be established."),
        gates_auto=(),
        gates_orchestrated=(),
        provider_error=provider_error,
        params={"changed_step_ids": changed},
        runner=runner,
        at=at,
    )


async def _stored(schedules: FakeSchedules, schedule: Schedule) -> Schedule:
    stored = await schedules.get(schedule.id)
    assert stored is not None
    return stored


async def test_a_fire_stopped_by_a_provider_error_puts_its_steps_back_and_fires_again_after_the_quiet_period() -> None:
    schedules = FakeSchedules()
    waiting = _schedule()
    await schedules.insert(waiting)
    topics = FakeBroadcaster()

    answer = await Scheduler(schedules, schedules, topics).put_back_steps(_ended(f"app:schedule:{waiting.id}"))

    assert answer == FailedFireSteps(put_back=FIRED_IDS, by_hand=(), why=None)
    stored = await _stored(schedules, waiting)
    assert stored.pending_step_ids == FIRED_IDS
    assert stored.next_run_at == T0 + timedelta(minutes=10), "the quiet period counts from the run's end"
    assert topics.published == ["schedules"]


async def test_the_fire_that_took_them_again_and_failed_again_puts_nothing_back() -> None:
    schedules = FakeSchedules()
    waiting = _schedule()
    await schedules.insert(waiting)
    scheduler = Scheduler(schedules, schedules, FakeBroadcaster())
    await scheduler.put_back_steps(_ended(f"app:schedule:{waiting.id}"))
    refired = T0 + timedelta(minutes=10)
    await schedules.stamp(
        waiting.id, last_run_at=refired, last_outcome="launched", next_run_at=None, session_id="s", cleared_page_ids=(), cleared_step_ids=FIRED_IDS
    )

    answer = await scheduler.put_back_steps(_ended(f"app:schedule:{waiting.id}", at=refired + timedelta(minutes=1)))

    assert answer == FailedFireSteps(put_back=(), by_hand=FIRED_IDS, why="put_back_once")
    stored = await _stored(schedules, waiting)
    assert (stored.pending_step_ids, stored.next_run_at) == ((), None), "the second failure leaves the schedule resting"


async def test_a_refire_that_also_took_a_new_step_puts_back_only_the_new_one() -> None:
    schedules = FakeSchedules()
    waiting = _schedule()
    await schedules.insert(waiting)
    scheduler = Scheduler(schedules, schedules, FakeBroadcaster())
    await scheduler.put_back_steps(_ended(f"app:schedule:{waiting.id}", changed="st.a.v2"))
    await schedules.stamp(
        waiting.id, last_run_at=T0, last_outcome="launched", next_run_at=None, session_id="s", cleared_page_ids=(), cleared_step_ids=("st.a.v2",)
    )

    answer = await scheduler.put_back_steps(_ended(f"app:schedule:{waiting.id}", changed="st.a.v2,st.c.v1"))

    assert answer == FailedFireSteps(put_back=("st.c.v1",), by_hand=("st.a.v2",), why="put_back_once")
    assert (await _stored(schedules, waiting)).pending_step_ids == ("st.c.v1",)


async def test_a_failure_inside_the_run_puts_nothing_back() -> None:
    schedules = FakeSchedules()
    waiting = _schedule()
    await schedules.insert(waiting)
    topics = FakeBroadcaster()

    answer = await Scheduler(schedules, schedules, topics).put_back_steps(_ended(f"app:schedule:{waiting.id}", provider_error=False))

    assert answer == FailedFireSteps(put_back=(), by_hand=FIRED_IDS, why="other_failure")
    stored = await _stored(schedules, waiting)
    assert (stored.pending_step_ids, stored.next_run_at) == ((), None)
    assert topics.published == []
    assert schedules.put_back == set(), "a failure that put nothing back leaves its steps free to be put back by a later provider error"


async def test_a_run_started_by_hand_changes_no_schedule() -> None:
    schedules = FakeSchedules()
    waiting = _schedule()
    await schedules.insert(waiting)

    for runner in ("app:harness", "app:chat", "app:studio", str(waiting.id)):
        assert await Scheduler(schedules, schedules, FakeBroadcaster()).put_back_steps(_ended(runner)) is None

    stored = await _stored(schedules, waiting)
    assert (stored.pending_step_ids, stored.next_run_at) == ((), None)


async def test_steps_that_arrived_during_the_run_stay_beside_those_put_back() -> None:
    schedules = FakeSchedules()
    arrived_at = T0 - timedelta(minutes=1)
    waiting = _schedule(pending_step_ids=("st.other.v3", "st.review_decision_brief.v2"), next_run_at=arrived_at + timedelta(minutes=10))
    await schedules.insert(waiting)

    await Scheduler(schedules, schedules, FakeBroadcaster()).put_back_steps(_ended(f"app:schedule:{waiting.id}"))

    stored = await _stored(schedules, waiting)
    assert stored.pending_step_ids == ("st.draft_decision_brief.v2", "st.other.v3", "st.review_decision_brief.v2"), "each id once"
    assert stored.next_run_at == T0 + timedelta(minutes=10)


async def test_a_paused_schedule_takes_nothing_back() -> None:
    schedules = FakeSchedules()
    paused = _schedule(enabled=False)
    await schedules.insert(paused)

    answer = await Scheduler(schedules, schedules, FakeBroadcaster()).put_back_steps(_ended(f"app:schedule:{paused.id}"))

    assert answer == FailedFireSteps(put_back=(), by_hand=FIRED_IDS, why="schedule_paused")
    assert (await _stored(schedules, paused)).pending_step_ids == ()


async def test_a_fire_of_another_cadence_or_of_no_schedule_is_none_of_its_business() -> None:
    schedules = FakeSchedules()
    clock = _schedule(cadence="every 30m", arguments={"changed_step_ids": FIRED}, next_run_at=T0 + timedelta(minutes=30))
    await schedules.insert(clock)
    scheduler = Scheduler(schedules, schedules, FakeBroadcaster())

    assert await scheduler.put_back_steps(_ended(f"app:schedule:{clock.id}")) is None
    assert await scheduler.put_back_steps(_ended("app:schedule:9f1c0a52-0000-4000-8000-000000000000")) is None
    assert await scheduler.put_back_steps(_ended("app:schedule:not-a-schedule")) is None
    assert (await _stored(schedules, clock)).pending_step_ids == ()
