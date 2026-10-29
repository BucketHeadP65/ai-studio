"""`PostgresSchedules` against a real, migration-created `schedules` table.

Nothing here is mocked: `migrated` (engram's own testcontainers plugin) runs the repo's one
alembic chain to head inside a fresh, disposable database, so a pass means the adapter's SQL
actually ran against the table migration 0028 creates -- not a hand-rolled schema this test
invented. Skips the whole tier when Docker is unavailable (the plugin's own skip).
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING
from uuid import uuid4

import asyncpg
import pytest_asyncio

from engram.testing.infrastructure.knowledge_base_db import migrated, pg  # noqa: F401 -- imported to be used as pytest fixtures, never called directly

from harness.features.scheduling.domain import Armed, Schedule, ScheduleUpdate
from harness.features.scheduling.infrastructure.postgres import PostgresSchedules

if TYPE_CHECKING:
    from collections.abc import AsyncIterator


def _asyncpg_dsn(url: str) -> str:
    """The `migrated` fixture's SQLAlchemy-style URL, in the plain form asyncpg wants."""
    return url.replace("postgresql+asyncpg://", "postgresql://", 1)


@pytest_asyncio.fixture
async def store(migrated: str) -> AsyncIterator[PostgresSchedules]:  # noqa: F811 -- must keep this exact
    # parameter name: pytest resolves the imported `migrated` fixture by its registered name
    # (fixed at its own definition), not by whatever local name imports it here.
    pool = await asyncpg.create_pool(_asyncpg_dsn(migrated))
    try:
        yield PostgresSchedules(pool)
    finally:
        await pool.close()


def _schedule(**overrides: object) -> Schedule:
    defaults: dict[str, object] = {"workflow_id": "wf.report", "cadence": "every 30m"}
    defaults.update(overrides)
    return Schedule(**defaults)  # type: ignore[arg-type]


async def test_insert_then_get_round_trips(store: PostgresSchedules) -> None:
    schedule = _schedule(arguments={"k": "v"})

    await store.insert(schedule)

    fetched = await store.get(schedule.id)
    assert fetched is not None
    assert fetched.id == schedule.id
    assert fetched.workflow_id == "wf.report"
    assert fetched.arguments == {"k": "v"}
    assert fetched.cadence == "every 30m"
    assert fetched.enabled is True
    assert fetched.gate_policy == "wait"
    assert fetched.gate_mode == "wait"
    assert fetched.session_id == ""
    assert fetched.last_outcome == ""
    assert fetched.pending_page_ids == ()


async def test_get_of_an_unknown_id_is_none(store: PostgresSchedules) -> None:
    assert await store.get(uuid4()) is None


async def test_list_all_returns_every_inserted_schedule(store: PostgresSchedules) -> None:
    one, two = _schedule(workflow_id="wf.a"), _schedule(workflow_id="wf.b")
    await store.insert(one)
    await store.insert(two)

    listed = await store.list_all()

    assert {schedule.id for schedule in listed} == {one.id, two.id}


async def test_update_changes_only_the_given_fields(store: PostgresSchedules) -> None:
    schedule = _schedule()
    await store.insert(schedule)

    changed = await store.update(schedule.id, ScheduleUpdate(cadence="every 2h", enabled=False))

    assert changed is True
    fetched = await store.get(schedule.id)
    assert fetched is not None
    assert fetched.cadence == "every 2h"
    assert fetched.enabled is False
    assert fetched.workflow_id == "wf.report", "an untouched field must survive a partial update"


async def test_update_of_an_unknown_id_is_false(store: PostgresSchedules) -> None:
    assert await store.update(uuid4(), ScheduleUpdate(cadence="every 1h")) is False


async def test_delete_removes_the_row(store: PostgresSchedules) -> None:
    schedule = _schedule()
    await store.insert(schedule)

    assert await store.delete(schedule.id) is True
    assert await store.get(schedule.id) is None


async def test_delete_of_an_unknown_id_is_false(store: PostgresSchedules) -> None:
    assert await store.delete(uuid4()) is False


async def test_due_returns_only_enabled_schedules_past_their_next_run(store: PostgresSchedules) -> None:
    now = datetime(2026, 8, 24, 12, 0, tzinfo=UTC)
    due_now = _schedule(workflow_id="wf.due", next_run_at=now - timedelta(minutes=1))
    not_yet = _schedule(workflow_id="wf.future", next_run_at=now + timedelta(hours=1))
    disabled = _schedule(workflow_id="wf.disabled", enabled=False, next_run_at=now - timedelta(minutes=1))
    never_scheduled = _schedule(workflow_id="wf.never")
    for schedule in (due_now, not_yet, disabled, never_scheduled):
        await store.insert(schedule)

    due = await store.due(now)

    assert [schedule.workflow_id for schedule in due] == ["wf.due"]


async def test_stamp_records_the_tick(store: PostgresSchedules) -> None:
    schedule = _schedule()
    await store.insert(schedule)
    ran_at = datetime(2026, 8, 24, 12, 0, tzinfo=UTC)
    next_at = ran_at + timedelta(hours=2)

    stamped = await store.stamp(
        schedule.id, last_run_at=ran_at, last_outcome="ok", next_run_at=next_at, session_id="sess-1", cleared_page_ids=()
    )

    assert stamped is True
    fetched = await store.get(schedule.id)
    assert fetched is not None
    assert fetched.last_run_at == ran_at
    assert fetched.last_outcome == "ok"
    assert fetched.next_run_at == next_at
    assert fetched.session_id == "sess-1"


async def test_stamp_of_an_unknown_id_is_false(store: PostgresSchedules) -> None:
    stamped = await store.stamp(uuid4(), last_run_at=datetime.now(UTC), last_outcome="ok", next_run_at=None, session_id="", cleared_page_ids=())

    assert stamped is False


async def test_update_flipping_enabled_true_to_false_stamps_paused_at(store: PostgresSchedules) -> None:
    schedule = _schedule()
    await store.insert(schedule)
    now = datetime(2026, 8, 25, 9, 0, tzinfo=UTC)

    changed = await store.update(schedule.id, ScheduleUpdate(enabled=False, paused_at=now))

    assert changed is True
    fetched = await store.get(schedule.id)
    assert fetched is not None
    assert fetched.enabled is False
    assert fetched.paused_at == now


async def test_update_flipping_enabled_false_to_true_clears_paused_at(store: PostgresSchedules) -> None:
    schedule = _schedule(enabled=False, paused_at=datetime(2026, 8, 25, 9, 0, tzinfo=UTC))
    await store.insert(schedule)

    changed = await store.update(schedule.id, ScheduleUpdate(enabled=True))

    assert changed is True
    fetched = await store.get(schedule.id)
    assert fetched is not None
    assert fetched.enabled is True
    assert fetched.paused_at is None


async def test_update_of_an_unrelated_field_leaves_paused_at_alone(store: PostgresSchedules) -> None:
    schedule = _schedule()
    await store.insert(schedule)

    changed = await store.update(schedule.id, ScheduleUpdate(cadence="every 2h"))

    assert changed is True
    fetched = await store.get(schedule.id)
    assert fetched is not None
    assert fetched.paused_at is None, "never disabled, so never stamped"


async def test_update_leaving_enabled_unset_leaves_a_stored_pause_alone(store: PostgresSchedules) -> None:
    schedule = _schedule(enabled=False, paused_at=datetime(2026, 8, 25, 9, 0, tzinfo=UTC))
    await store.insert(schedule)

    changed = await store.update(schedule.id, ScheduleUpdate(cadence="every 2h"))

    assert changed is True
    fetched = await store.get(schedule.id)
    assert fetched is not None
    assert fetched.paused_at == schedule.paused_at


async def test_update_writes_a_next_fire_when_one_is_given(store: PostgresSchedules) -> None:
    schedule = _schedule(next_run_at=None)
    await store.insert(schedule)
    restored = datetime(2026, 9, 27, 9, 0, tzinfo=UTC)

    assert await store.update(schedule.id, ScheduleUpdate(next_run_at=restored)) is True

    got = await store.get(schedule.id)
    assert got is not None
    assert got.next_run_at == restored


async def test_arm_sets_the_next_fire_of_resting_enabled_rows_and_keeps_an_earlier_one(store: PostgresSchedules) -> None:
    at = datetime(2026, 10, 6, 12, 0, tzinfo=UTC)
    resting = _schedule(cadence="on refresh")
    armed_before = _schedule(cadence="on refresh", next_run_at=at - timedelta(minutes=5))
    paused = _schedule(cadence="on refresh", enabled=False)
    for row in (resting, armed_before, paused):
        await store.insert(row)

    armed = await store.arm([resting.id, armed_before.id, paused.id, uuid4()], at=at)

    assert armed.repeat is False
    assert set(armed.schedule_ids) == {resting.id, armed_before.id}
    first = await store.get(resting.id)
    second = await store.get(armed_before.id)
    third = await store.get(paused.id)
    assert first is not None
    assert second is not None
    assert third is not None
    assert first.next_run_at == at
    assert second.next_run_at == at - timedelta(minutes=5)
    assert third.next_run_at is None


async def test_arm_with_an_event_id_seen_before_arms_nothing(store: PostgresSchedules) -> None:
    at = datetime(2026, 10, 6, 12, 0, tzinfo=UTC)
    resting = _schedule(cadence="on refresh")
    await store.insert(resting)
    event_id = uuid4()

    first = await store.arm([resting.id], at=at, event_id=event_id)
    await store.stamp(resting.id, last_run_at=at, last_outcome="launched", next_run_at=None, session_id="", cleared_page_ids=())
    second = await store.arm([resting.id], at=at + timedelta(minutes=1), event_id=event_id)

    assert first.schedule_ids == (resting.id,)
    assert (second.repeat, second.schedule_ids) == (True, ())
    fetched = await store.get(resting.id)
    assert fetched is not None
    assert fetched.next_run_at is None


async def test_update_that_clears_the_next_fire_writes_null(store: PostgresSchedules) -> None:
    held = _schedule(next_run_at=datetime(2026, 8, 28, 7, 0, tzinfo=UTC))
    await store.insert(held)

    assert await store.update(held.id, ScheduleUpdate(cadence="on refresh", clears_next_fire=True)) is True

    fetched = await store.get(held.id)
    assert fetched is not None
    assert (fetched.cadence, fetched.next_run_at) == ("on refresh", None)


async def test_arm_with_pages_records_the_event_once(store: PostgresSchedules, migrated: str) -> None:  # noqa: F811 (pytest resolves the imported fixture by this name)
    at = datetime(2026, 10, 29, 12, 0, tzinfo=UTC)
    waiting = _schedule(cadence="on index notes")
    await store.insert(waiting)
    event_id = uuid4()

    first = await store.arm_with_pages({waiting.id: at}, event_id=event_id, page_ids=["note.a"])
    second = await store.arm_with_pages({waiting.id: at + timedelta(minutes=5)}, event_id=event_id, page_ids=["note.b"])

    assert first == Armed(schedule_ids=(waiting.id,))
    assert second == Armed(repeat=True)
    fetched = await store.get(waiting.id)
    assert fetched is not None
    assert (fetched.next_run_at, fetched.pending_page_ids) == (at, ("note.a",)), "the repeat changed no row"
    connection = await asyncpg.connect(_asyncpg_dsn(migrated))
    try:
        subscribers = await connection.fetch("SELECT subscriber FROM coordination.event_deliveries WHERE event_id = $1", event_id)
    finally:
        await connection.close()
    assert [row["subscriber"] for row in subscribers] == ["schedules.on_index"]


async def test_arm_with_pages_moves_the_fire_and_collects_ids(store: PostgresSchedules) -> None:
    first_fire = datetime(2026, 10, 29, 12, 30, tzinfo=UTC)
    second_fire = first_fire + timedelta(minutes=10)
    waiting = _schedule(cadence="on index notes")
    await store.insert(waiting)

    await store.arm_with_pages({waiting.id: first_fire}, event_id=uuid4(), page_ids=["note.b", "note.a"])
    armed = await store.arm_with_pages({waiting.id: second_fire}, event_id=uuid4(), page_ids=["note.c", "note.b"])

    assert armed == Armed(schedule_ids=(waiting.id,))
    fetched = await store.get(waiting.id)
    assert fetched is not None
    assert fetched.next_run_at == second_fire, "the latest index moves the fire"
    assert fetched.pending_page_ids == ("note.a", "note.b", "note.c")


async def test_arm_with_pages_skips_a_disabled_schedule(store: PostgresSchedules) -> None:
    at = datetime(2026, 10, 29, 12, 30, tzinfo=UTC)
    waiting = _schedule(cadence="on index notes")
    paused = _schedule(cadence="on index notes", enabled=False)
    for row in (waiting, paused):
        await store.insert(row)

    armed = await store.arm_with_pages({waiting.id: at, paused.id: at, uuid4(): at}, event_id=uuid4(), page_ids=["note.a"])

    assert armed == Armed(schedule_ids=(waiting.id,))
    left = await store.get(paused.id)
    assert left is not None
    assert (left.next_run_at, left.pending_page_ids) == (None, ())


async def test_stamp_removes_only_the_page_ids_it_passed(store: PostgresSchedules) -> None:
    at = datetime(2026, 10, 29, 12, 30, tzinfo=UTC)
    waiting = _schedule(cadence="on index notes", next_run_at=at, pending_page_ids=("a", "b", "c"))
    await store.insert(waiting)

    stamped = await store.stamp(waiting.id, last_run_at=at, last_outcome="launched", next_run_at=None, session_id="", cleared_page_ids=["a", "b"])

    assert stamped is True
    fetched = await store.get(waiting.id)
    assert fetched is not None
    assert fetched.pending_page_ids == ("c",)


async def test_stamp_keeps_the_fire_an_index_set_while_ids_remain_pending(store: PostgresSchedules) -> None:
    fired_at = datetime(2026, 10, 29, 12, 30, tzinfo=UTC)
    armed_fire = fired_at + timedelta(minutes=30)
    waiting = _schedule(cadence="on index notes", next_run_at=fired_at, pending_page_ids=("a", "b"))
    await store.insert(waiting)
    await store.arm_with_pages({waiting.id: armed_fire}, event_id=uuid4(), page_ids=["c"])

    await store.stamp(waiting.id, last_run_at=fired_at, last_outcome="launched", next_run_at=None, session_id="", cleared_page_ids=["a", "b"])

    fetched = await store.get(waiting.id)
    assert fetched is not None
    assert (fetched.next_run_at, fetched.pending_page_ids) == (armed_fire, ("c",)), "the page the arm added fires on the arm's fire"


async def test_stamp_that_passed_no_pages_applies_its_next_fire(store: PostgresSchedules) -> None:
    fired_at = datetime(2026, 10, 29, 12, 30, tzinfo=UTC)
    waiting = _schedule(cadence="on index notes", next_run_at=fired_at, pending_page_ids=("a",))
    await store.insert(waiting)

    await store.stamp(waiting.id, last_run_at=fired_at, last_outcome="refused: no", next_run_at=None, session_id="", cleared_page_ids=[])

    fetched = await store.get(waiting.id)
    assert fetched is not None
    assert (fetched.next_run_at, fetched.pending_page_ids) == (None, ("a",)), "a refused fire rests and keeps its pages"
