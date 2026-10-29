"""Schedules whose cadence waits for an event: they rest, an arm makes them due, and the fire rests them again."""

from __future__ import annotations

import asyncio

from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING
from uuid import uuid4

import pytest

from harness.features.running.records import RunCollaborators
from harness.features.scheduling.domain import Armed
from harness.features.scheduling.service import MISSED_OUTCOME, Scheduler
from harness.session import SessionPool
from harness.settings import RunSettings
from tests.support.fakes.broadcaster import FakeBroadcaster
from tests.support.fakes.schedules import FakeSchedules
from tests.support.state import records_under
from tests.unit.test_schedule_ticker import FakeWorkflowStore, _one_node_spec, _register_slow_step, _runtime, _schedule

if TYPE_CHECKING:
    from pathlib import Path

    from core.ai.workflows.contracts import StepRegistry

T0 = datetime(2026, 8, 24, 12, 0, tzinfo=UTC)


async def test_arm_gives_each_resting_enabled_schedule_its_next_fire() -> None:
    schedules = FakeSchedules()
    resting = _schedule(cadence="on refresh", next_run_at=None)
    paused = _schedule(cadence="on refresh", next_run_at=None, enabled=False)
    await schedules.insert(resting)
    await schedules.insert(paused)

    armed = await schedules.arm([resting.id, paused.id, uuid4()], at=T0)

    assert armed.repeat is False
    assert armed.schedule_ids == (resting.id,)
    stored = await schedules.get(resting.id)
    assert stored is not None
    assert stored.next_run_at == T0
    left = await schedules.get(paused.id)
    assert left is not None
    assert left.next_run_at is None, "a paused schedule is never armed"


async def test_arm_keeps_an_earlier_arm() -> None:
    schedules = FakeSchedules()
    armed_before = _schedule(cadence="on refresh", next_run_at=T0)
    await schedules.insert(armed_before)

    armed = await schedules.arm([armed_before.id], at=T0 + timedelta(minutes=5))

    assert armed.schedule_ids == (armed_before.id,)
    stored = await schedules.get(armed_before.id)
    assert stored is not None
    assert stored.next_run_at == T0


async def test_an_event_already_recorded_arms_nothing() -> None:
    schedules = FakeSchedules()
    resting = _schedule(cadence="on refresh", next_run_at=None)
    await schedules.insert(resting)
    event_id = uuid4()

    first = await schedules.arm([resting.id], at=T0, event_id=event_id)
    await schedules.stamp(resting.id, last_run_at=T0, last_outcome="launched", next_run_at=None, session_id="", cleared_page_ids=())
    second = await schedules.arm([resting.id], at=T0 + timedelta(minutes=1), event_id=event_id)

    assert first.schedule_ids == (resting.id,)
    assert (second.repeat, second.schedule_ids) == (True, ())
    stored = await schedules.get(resting.id)
    assert stored is not None
    assert stored.next_run_at is None, "the repeat did not arm the schedule that already fired"


async def test_the_fake_arms_with_pages_as_the_store_does() -> None:
    schedules = FakeSchedules()
    waiting = _schedule(cadence="on index notes", next_run_at=None)
    armed_before = _schedule(cadence="on index notes", next_run_at=T0, pending_page_ids=("note.a",))
    paused = _schedule(cadence="on index notes", next_run_at=None, enabled=False)
    for row in (waiting, armed_before, paused):
        await schedules.insert(row)
    later = T0 + timedelta(minutes=30)
    event_id = uuid4()

    armed = await schedules.arm_with_pages(
        {waiting.id: later, armed_before.id: later, paused.id: later, uuid4(): later}, event_id=event_id, page_ids=["note.c", "note.a"]
    )
    repeat = await schedules.arm_with_pages({waiting.id: later + timedelta(minutes=5)}, event_id=event_id, page_ids=["note.z"])

    assert armed == Armed(schedule_ids=(waiting.id, armed_before.id))
    assert repeat == Armed(repeat=True)
    assert schedules.received_index == {event_id}
    first = await schedules.get(waiting.id)
    second = await schedules.get(armed_before.id)
    third = await schedules.get(paused.id)
    assert first is not None
    assert second is not None
    assert third is not None
    assert (first.next_run_at, first.pending_page_ids) == (later, ("note.a", "note.c")), "the repeat changed nothing"
    assert (second.next_run_at, second.pending_page_ids) == (later, ("note.a", "note.c")), "the earlier fire is moved and the ids are a union"
    assert (third.next_run_at, third.pending_page_ids) == (None, ()), "a paused schedule is never armed"


async def test_the_fake_stamp_removes_only_the_page_ids_it_passed() -> None:
    schedules = FakeSchedules()
    waiting = _schedule(cadence="on index notes", next_run_at=T0, pending_page_ids=("a", "b", "c"))
    await schedules.insert(waiting)

    await schedules.stamp(waiting.id, last_run_at=T0, last_outcome="launched", next_run_at=None, session_id="", cleared_page_ids=["a", "b"])

    stored = await schedules.get(waiting.id)
    assert stored is not None
    assert stored.pending_page_ids == ("c",)


async def test_the_fake_stamp_keeps_the_fire_an_index_set_while_ids_remain_pending() -> None:
    schedules = FakeSchedules()
    waiting = _schedule(cadence="on index notes", next_run_at=T0, pending_page_ids=("a", "b"))
    await schedules.insert(waiting)
    armed_fire = T0 + timedelta(minutes=30)
    await schedules.arm_with_pages({waiting.id: armed_fire}, event_id=uuid4(), page_ids=["c"])

    await schedules.stamp(waiting.id, last_run_at=T0, last_outcome="launched", next_run_at=None, session_id="", cleared_page_ids=["a", "b"])

    stored = await schedules.get(waiting.id)
    assert stored is not None
    assert (stored.next_run_at, stored.pending_page_ids) == (armed_fire, ("c",)), "the page the arm added fires on the arm's fire"


async def test_the_fake_stamp_that_passed_no_pages_applies_its_next_fire() -> None:
    schedules = FakeSchedules()
    waiting = _schedule(cadence="on index notes", next_run_at=T0, pending_page_ids=("a",))
    await schedules.insert(waiting)

    await schedules.stamp(waiting.id, last_run_at=T0, last_outcome="refused: no", next_run_at=None, session_id="", cleared_page_ids=())

    stored = await schedules.get(waiting.id)
    assert stored is not None
    assert (stored.next_run_at, stored.pending_page_ids) == (None, ("a",)), "a refused fire rests and keeps its pages"


async def test_an_index_arms_the_schedules_naming_its_knowledge_base() -> None:
    schedules = FakeSchedules()
    waiting = _schedule(cadence="on index docs", next_run_at=None)
    elsewhere = _schedule(cadence="on index other", next_run_at=None)
    on_refresh = _schedule(cadence="on refresh", next_run_at=None)
    unreadable = _schedule(cadence="on index Docs!", next_run_at=None)
    for row in (waiting, elsewhere, on_refresh, unreadable):
        await schedules.insert(row)
    topics = FakeBroadcaster()
    event_id = uuid4()

    armed = await Scheduler(schedules, schedules, topics).arm_after_index(
        event_id=event_id, knowledge_base="docs", page_ids=["note.b", "note.a"], now=T0
    )

    assert armed == Armed(schedule_ids=(waiting.id,))
    assert topics.published == ["schedules"]
    stored = await schedules.get(waiting.id)
    assert stored is not None
    assert (stored.next_run_at, stored.pending_page_ids) == (T0 + timedelta(seconds=1800), ("note.a", "note.b"))
    for row in (elsewhere, on_refresh, unreadable):
        left = await schedules.get(row.id)
        assert left is not None
        assert (left.next_run_at, left.pending_page_ids) == (None, ())


async def test_each_schedule_takes_its_own_quiet_period() -> None:
    schedules = FakeSchedules()
    short = _schedule(cadence="on index docs quiet 1m", next_run_at=None)
    default = _schedule(cadence="on index docs", next_run_at=None)
    await schedules.insert(short)
    await schedules.insert(default)

    await Scheduler(schedules, schedules, FakeBroadcaster()).arm_after_index(event_id=uuid4(), knowledge_base="docs", page_ids=["note.a"], now=T0)

    first = await schedules.get(short.id)
    second = await schedules.get(default.id)
    assert first is not None
    assert second is not None
    assert first.next_run_at == T0 + timedelta(seconds=60)
    assert second.next_run_at == T0 + timedelta(seconds=1800)


async def test_a_later_index_moves_the_fire_and_adds_its_pages() -> None:
    schedules = FakeSchedules()
    waiting = _schedule(cadence="on index docs", next_run_at=None)
    await schedules.insert(waiting)
    scheduler = Scheduler(schedules, schedules, FakeBroadcaster())
    later = T0 + timedelta(minutes=10)

    await scheduler.arm_after_index(event_id=uuid4(), knowledge_base="docs", page_ids=["note.a"], now=T0)
    await scheduler.arm_after_index(event_id=uuid4(), knowledge_base="docs", page_ids=["note.b", "note.a"], now=later)

    stored = await schedules.get(waiting.id)
    assert stored is not None
    assert stored.next_run_at == later + timedelta(seconds=1800), "the fire moved to the later index plus the quiet period"
    assert stored.pending_page_ids == ("note.a", "note.b")


async def test_a_disabled_schedule_collects_nothing() -> None:
    schedules = FakeSchedules()
    paused = _schedule(cadence="on index docs", next_run_at=None, enabled=False)
    await schedules.insert(paused)
    topics = FakeBroadcaster()
    event_id = uuid4()

    armed = await Scheduler(schedules, schedules, topics).arm_after_index(
        event_id=event_id, knowledge_base="docs", page_ids=["note.a"], now=T0
    )

    assert armed == Armed()
    assert topics.published == [], "an index that armed nothing announces nothing"
    assert schedules.received_index == {event_id}, "the index is recorded even when no schedule collects it"
    stored = await schedules.get(paused.id)
    assert stored is not None
    assert (stored.next_run_at, stored.pending_page_ids) == (None, ())


async def test_an_index_already_recorded_arms_nothing() -> None:
    schedules = FakeSchedules()
    waiting = _schedule(cadence="on index docs", next_run_at=None)
    await schedules.insert(waiting)
    topics = FakeBroadcaster()
    scheduler = Scheduler(schedules, schedules, topics)
    event_id = uuid4()

    await scheduler.arm_after_index(event_id=event_id, knowledge_base="docs", page_ids=["note.a"], now=T0)
    await schedules.stamp(waiting.id, last_run_at=T0, last_outcome="launched", next_run_at=None, session_id="", cleared_page_ids=["note.a"])
    again = await scheduler.arm_after_index(event_id=event_id, knowledge_base="docs", page_ids=["note.a"], now=T0 + timedelta(minutes=1))

    assert again == Armed(repeat=True)
    assert topics.published == ["schedules"], "the repeat announced nothing"
    stored = await schedules.get(waiting.id)
    assert stored is not None
    assert (stored.next_run_at, stored.pending_page_ids) == (None, ()), "the repeat did not arm the schedule that already fired"


async def test_an_armed_on_refresh_schedule_fires_once_and_rests(registry: StepRegistry, tmp_path: Path) -> None:
    store = FakeWorkflowStore(specs={"wf-sales": _one_node_spec()})
    pool = SessionPool(_runtime(registry, store))
    schedules = FakeSchedules()
    schedule = _schedule(cadence="on refresh", next_run_at=T0)
    await schedules.insert(schedule)
    run = RunCollaborators(records=records_under(tmp_path), topics=FakeBroadcaster(), run_settings=RunSettings())

    await Scheduler(schedules, schedules, FakeBroadcaster()).tick(pool, run=run, now=T0)

    after = await schedules.get(schedule.id)
    assert after is not None
    assert after.last_outcome == "launched"
    assert after.next_run_at is None, "an armed schedule fires once and then rests"
    session = await pool.get(after.session_id)
    assert session.active.task is not None
    await session.active.task


async def test_an_overlap_keeps_the_arm_without_catch_up_and_two_arms_fire_once(registry: StepRegistry, tmp_path: Path) -> None:
    hold = asyncio.Event()
    store = FakeWorkflowStore(specs={"wf-hold": _register_slow_step(registry, hold)})
    pool = SessionPool(_runtime(registry, store))
    schedules = FakeSchedules()
    schedule = _schedule(workflow_id="wf-hold", arguments={}, cadence="on refresh", catch_up=False, next_run_at=T0)
    await schedules.insert(schedule)
    scheduler = Scheduler(schedules, schedules, FakeBroadcaster())
    run = RunCollaborators(records=records_under(tmp_path), topics=FakeBroadcaster(), run_settings=RunSettings())

    await scheduler.tick(pool, run=run, now=T0)
    await schedules.arm([schedule.id], at=T0 + timedelta(minutes=1))
    await scheduler.tick(pool, run=run, now=T0 + timedelta(minutes=1))
    await schedules.arm([schedule.id], at=T0 + timedelta(minutes=2))

    held = await schedules.get(schedule.id)
    assert held is not None
    assert held.last_outcome == MISSED_OUTCOME
    assert held.next_run_at == T0 + timedelta(minutes=1), "the arm survived the overlap, and the second arm kept the first"

    hold.set()
    first_session = await pool.get(held.session_id)
    assert first_session.active.task is not None
    await first_session.active.task
    await scheduler.tick(pool, run=run, now=T0 + timedelta(minutes=3))

    fired = await schedules.get(schedule.id)
    assert fired is not None
    assert fired.last_outcome == "launched"
    assert fired.next_run_at is None
    second_session = await pool.get(fired.session_id)
    assert second_session.active.task is not None
    await second_session.active.task


@pytest.mark.parametrize("cadence", ["on refresh", "on index notes"])
async def test_a_blackout_keeps_the_arm_without_catch_up(cadence: str, registry: StepRegistry, tmp_path: Path) -> None:
    store = FakeWorkflowStore(specs={"wf-sales": _one_node_spec()})
    pool = SessionPool(_runtime(registry, store))
    schedules = FakeSchedules()
    schedule = _schedule(cadence=cadence, blackout="11:00-13:00", catch_up=False, next_run_at=T0)
    await schedules.insert(schedule)
    run = RunCollaborators(records=records_under(tmp_path), topics=FakeBroadcaster(), run_settings=RunSettings())

    await Scheduler(schedules, schedules, FakeBroadcaster()).tick(pool, run=run, now=T0)

    held = await schedules.get(schedule.id)
    assert held is not None
    assert held.last_outcome == "blacked out until 13:00"
    assert held.next_run_at == T0
