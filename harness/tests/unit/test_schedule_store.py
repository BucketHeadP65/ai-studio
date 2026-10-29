"""Cadence parsing, and `Schedules`'s CRUD + due-scan + tick-stamp contract against a fake.

The fake is the reference behavior: `test_schedule_store_pg.py` runs the same shaped assertions
against `PostgresSchedules` over a real, migrated `schedules` table, so a pass here pins the
contract the adapter must also satisfy.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime, time, timedelta
from typing import TYPE_CHECKING
from uuid import UUID, uuid4

import pytest

from harness.features.scheduling.domain import (
    AfterRunCadence,
    BlackoutWindow,
    Cadence,
    CronCadence,
    IntervalCadence,
    OnIndexCadence,
    OnRefreshCadence,
    Schedule,
    ScheduleUpdate,
    blacked_out_until,
    gate_wait_seconds,
    next_after,
    parse_blackout,
    parse_cadence,
    preview_fires,
)

if TYPE_CHECKING:
    from collections.abc import Sequence

# --- parse_cadence ----------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("on refresh", OnRefreshCadence()),
        ("On  Refresh", OnRefreshCadence()),
        ("after nightly-report", AfterRunCadence(workflow_name="nightly-report")),
        ("After nightly-report", AfterRunCadence(workflow_name="nightly-report")),
    ],
)
def test_the_two_event_cadences_parse(text: str, expected: Cadence) -> None:
    assert parse_cadence(text) == expected


@pytest.mark.parametrize("text", ["on", "on refresh now", "after", "after Nightly-Report", "after nightly report", "after -report"])
def test_a_malformed_event_cadence_is_refused(text: str) -> None:
    with pytest.raises(ValueError, match="is not a cadence"):
        parse_cadence(text)


def test_the_on_index_cadence_parses_with_its_default_quiet_period() -> None:
    assert parse_cadence("on index repo-docs") == OnIndexCadence(knowledge_base="repo-docs", quiet_seconds=1800)


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("on index repo-docs quiet 10m", OnIndexCadence(knowledge_base="repo-docs", quiet_seconds=600)),
        ("On Index docs QUIET 45s", OnIndexCadence(knowledge_base="docs", quiet_seconds=45)),
    ],
)
def test_the_on_index_cadence_parses_a_named_quiet_period(text: str, expected: Cadence) -> None:
    assert parse_cadence(text) == expected


@pytest.mark.parametrize("text", ["on index", "on index Repo_Docs", "on index repo-docs quiet 10d", "on index repo-docs soon"])
def test_a_malformed_on_index_cadence_is_refused(text: str) -> None:
    with pytest.raises(ValueError, match="is not a cadence"):
        parse_cadence(text)


def test_a_zero_quiet_period_is_refused() -> None:
    with pytest.raises(ValueError, match="quiet period must be longer than zero"):
        parse_cadence("on index repo-docs quiet 0m")


@pytest.mark.parametrize("text", ["on index docs quiet 25h", "on index docs quiet 86401s", "on index docs quiet 99999999h"])
def test_a_quiet_period_over_a_day_is_refused(text: str) -> None:
    with pytest.raises(ValueError, match=r"quiet period must be at most 24 hours \(86400 seconds\)"):
        parse_cadence(text)


@pytest.mark.parametrize("text", ["on index docs quiet 24h", "on index docs quiet 1440m", "on index docs quiet 86400s"])
def test_a_quiet_period_of_a_day_is_accepted(text: str) -> None:
    assert parse_cadence(text) == OnIndexCadence(knowledge_base="docs", quiet_seconds=86400)


def test_the_refusal_names_every_cadence_form() -> None:
    with pytest.raises(ValueError, match="is not a cadence") as refusal:
        parse_cadence("sometimes")

    message = str(refusal.value)
    assert "'every <N><s|m|h|d>'" in message
    assert "a 5-field cron expression" in message
    assert "'on refresh', 'after <workflow name>'" in message
    assert "or 'on index <knowledge base>' with an optional 'quiet <N><s|m|h>'" in message


@pytest.mark.parametrize(
    "cadence",
    [OnRefreshCadence(), AfterRunCadence(workflow_name="nightly-report"), OnIndexCadence(knowledge_base="repo-docs", quiet_seconds=1800)],
)
def test_an_event_cadence_has_no_next_fire_and_no_preview(cadence: Cadence) -> None:
    now = datetime(2026, 8, 24, 12, 0, tzinfo=UTC)

    assert next_after(cadence, now) is None
    assert preview_fires(cadence, since=now) == []



@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("every 2h", IntervalCadence(seconds=7200)),
        ("every 30m", IntervalCadence(seconds=1800)),
        ("every 90s", IntervalCadence(seconds=90)),
        ("every 1d", IntervalCadence(seconds=86400)),
        ("0 9 * * 1", CronCadence(expression="0 9 * * 1")),
    ],
)
def test_parse_cadence_accepts_the_documented_shapes(text: str, expected: Cadence) -> None:
    assert parse_cadence(text) == expected


@pytest.mark.parametrize(
    "garbage",
    ["", "every", "every 5x", "every -1m", "not a cadence", "* * *", "0 9 * * 1 *"],
)
def test_parse_cadence_refuses_garbage_naming_the_shape(garbage: str) -> None:
    with pytest.raises(ValueError, match="cadence looks like"):
        parse_cadence(garbage)


# --- next_after --------------------------------------------------------------------------------


def test_next_after_an_interval_adds_its_seconds() -> None:
    now = datetime(2026, 8, 24, 10, 0, tzinfo=UTC)

    assert next_after(parse_cadence("every 30m"), now) == now + timedelta(minutes=30)


def test_next_after_a_cron_expression_finds_the_next_matching_instant() -> None:
    now = datetime(2026, 8, 24, 10, 0, tzinfo=UTC)  # a Monday, past this week's 09:00 slot

    next_run = next_after(parse_cadence("0 9 * * 1"), now)

    assert next_run == datetime(2026, 8, 31, 9, 0, tzinfo=UTC)


# --- parse_blackout / blacked_out_until -----------------------------------------------------


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("09:00-17:30", (BlackoutWindow(start_minutes=540, end_minutes=1050),)),
        ("17:30-09:00", (BlackoutWindow(start_minutes=1050, end_minutes=540),)),
        ("22:00-06:00", (BlackoutWindow(start_minutes=1320, end_minutes=360),)),
        (
            "09:00-10:00,14:00-15:00",
            (BlackoutWindow(start_minutes=540, end_minutes=600), BlackoutWindow(start_minutes=840, end_minutes=900)),
        ),
        (
            "14:00-15:00,09:00-10:00",
            (BlackoutWindow(start_minutes=840, end_minutes=900), BlackoutWindow(start_minutes=540, end_minutes=600)),
        ),
    ],
)
def test_parse_blackout_accepts_both_orders_wraps_and_multiple_windows(text: str, expected: tuple[BlackoutWindow, ...]) -> None:
    assert parse_blackout(text) == expected


@pytest.mark.parametrize(
    "garbage",
    ["", "  ", "whenever", "9:00-10:00", "25:00-02:00", "09:00-09:70", "09:00", "09:00-10:00,"],
)
def test_parse_blackout_refuses_garbage_naming_the_shape(garbage: str) -> None:
    with pytest.raises(ValueError, match="blackout looks like"):
        parse_blackout(garbage)


def test_parse_blackout_refuses_a_window_that_starts_and_ends_on_the_same_minute() -> None:
    with pytest.raises(ValueError, match="start and end cannot be the same minute"):
        parse_blackout("10:00-10:00")


def test_blacked_out_until_a_normal_window_names_its_end() -> None:
    windows = parse_blackout("09:00-17:30")

    assert blacked_out_until(windows, datetime(2026, 8, 24, 12, 0, tzinfo=UTC)) == time(17, 30)
    assert blacked_out_until(windows, datetime(2026, 8, 24, 8, 59, tzinfo=UTC)) is None
    assert blacked_out_until(windows, datetime(2026, 8, 24, 17, 30, tzinfo=UTC)) is None, "the window's end is exclusive"


def test_blacked_out_until_a_window_wrapping_midnight_spans_both_days() -> None:
    windows = parse_blackout("22:00-06:00")

    assert blacked_out_until(windows, datetime(2026, 8, 24, 23, 0, tzinfo=UTC)) == time(6, 0)
    assert blacked_out_until(windows, datetime(2026, 8, 25, 3, 0, tzinfo=UTC)) == time(6, 0)
    assert blacked_out_until(windows, datetime(2026, 8, 24, 12, 0, tzinfo=UTC)) is None


def test_blacked_out_until_checks_every_window_in_a_multi_window_blackout() -> None:
    windows = parse_blackout("09:00-10:00,22:00-06:00")

    assert blacked_out_until(windows, datetime(2026, 8, 24, 9, 30, tzinfo=UTC)) == time(10, 0)
    assert blacked_out_until(windows, datetime(2026, 8, 25, 1, 0, tzinfo=UTC)) == time(6, 0)
    assert blacked_out_until(windows, datetime(2026, 8, 24, 12, 0, tzinfo=UTC)) is None


# --- Schedules, exercised against the fake ------------------------------------------------


@dataclass
class FakeSchedules:
    """In-memory `Schedules`: a plain dict keyed by id, snapshot-on-write like the real table."""

    rows: dict[UUID, Schedule] = field(default_factory=dict)

    async def insert(self, schedule: Schedule) -> None:
        self.rows[schedule.id] = schedule

    async def get(self, schedule_id: UUID) -> Schedule | None:
        return self.rows.get(schedule_id)

    async def list_all(self) -> list[Schedule]:
        return list(self.rows.values())

    async def update(self, schedule_id: UUID, changes: ScheduleUpdate) -> bool:
        current = self.rows.get(schedule_id)
        if current is None:
            return False
        fields = {
            "workflow_id": changes.workflow_id,
            "arguments": changes.arguments,
            "cadence": changes.cadence,
            "enabled": changes.enabled,
            "gate_policy": changes.gate_policy,
            "capacity_key": changes.capacity_key,
            "capacity_limit": changes.capacity_limit,
            "blackout": changes.blackout,
            "catch_up": changes.catch_up,
        }
        updates = {k: v for k, v in fields.items() if v is not None}
        if changes.enabled is not None:
            updates["paused_at"] = changes.paused_at
        self.rows[schedule_id] = current.model_copy(update=updates)
        return True

    async def delete(self, schedule_id: UUID) -> bool:
        return self.rows.pop(schedule_id, None) is not None

    async def due(self, now: datetime) -> list[Schedule]:
        candidates = (row for row in self.rows.values() if row.enabled and row.next_run_at is not None and row.next_run_at <= now)
        return sorted(candidates, key=lambda row: row.next_run_at)  # type: ignore[arg-type,return-value]

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
        current = self.rows.get(schedule_id)
        if current is None:
            return False
        cleared = set(cleared_page_ids)
        pending = tuple(page_id for page_id in current.pending_page_ids if page_id not in cleared)
        self.rows[schedule_id] = current.model_copy(
            update={
                "last_run_at": last_run_at,
                "last_outcome": last_outcome,
                "next_run_at": next_run_at,
                "session_id": session_id,
                "pending_page_ids": pending,
            }
        )
        return True


def _schedule(**overrides: object) -> Schedule:
    defaults: dict[str, object] = {"workflow_id": "wf.report", "cadence": "every 30m"}
    defaults.update(overrides)
    return Schedule(**defaults)  # type: ignore[arg-type]


async def test_insert_then_get_round_trips() -> None:
    store = FakeSchedules()
    schedule = _schedule(arguments={"k": "v"})

    await store.insert(schedule)

    assert await store.get(schedule.id) == schedule


async def test_get_of_an_unknown_id_is_none() -> None:
    assert await FakeSchedules().get(uuid4()) is None


async def test_list_all_returns_every_inserted_schedule() -> None:
    store = FakeSchedules()
    one, two = _schedule(workflow_id="wf.a"), _schedule(workflow_id="wf.b")
    await store.insert(one)
    await store.insert(two)

    assert {schedule.id for schedule in await store.list_all()} == {one.id, two.id}


async def test_update_changes_only_the_given_fields() -> None:
    store = FakeSchedules()
    schedule = _schedule()
    await store.insert(schedule)

    changed = await store.update(schedule.id, ScheduleUpdate(cadence="every 2h", enabled=False))

    assert changed is True
    updated = await store.get(schedule.id)
    assert updated is not None
    assert updated.cadence == "every 2h"
    assert updated.enabled is False
    assert updated.workflow_id == "wf.report", "an untouched field must survive a partial update"


async def test_update_of_an_unknown_id_is_false() -> None:
    assert await FakeSchedules().update(uuid4(), ScheduleUpdate(cadence="every 1h")) is False


async def test_delete_removes_the_row_and_reports_it_existed() -> None:
    store = FakeSchedules()
    schedule = _schedule()
    await store.insert(schedule)

    assert await store.delete(schedule.id) is True
    assert await store.get(schedule.id) is None


async def test_delete_of_an_unknown_id_is_false() -> None:
    assert await FakeSchedules().delete(uuid4()) is False


async def test_due_returns_only_enabled_schedules_whose_next_run_has_arrived() -> None:
    store = FakeSchedules()
    now = datetime(2026, 8, 24, 12, 0, tzinfo=UTC)
    due_now = _schedule(workflow_id="wf.due", next_run_at=now - timedelta(minutes=1))
    not_yet = _schedule(workflow_id="wf.future", next_run_at=now + timedelta(hours=1))
    disabled = _schedule(workflow_id="wf.disabled", enabled=False, next_run_at=now - timedelta(minutes=1))
    never_scheduled = _schedule(workflow_id="wf.never")
    for schedule in (due_now, not_yet, disabled, never_scheduled):
        await store.insert(schedule)

    due = await store.due(now)

    assert [schedule.workflow_id for schedule in due] == ["wf.due"]


async def test_stamp_records_the_tick_and_reuses_the_runner_session() -> None:
    store = FakeSchedules()
    schedule = _schedule()
    await store.insert(schedule)
    ran_at = datetime(2026, 8, 24, 12, 0, tzinfo=UTC)
    next_at = ran_at + timedelta(hours=2)

    stamped = await store.stamp(
        schedule.id, last_run_at=ran_at, last_outcome="ok", next_run_at=next_at, session_id="sess-1", cleared_page_ids=()
    )

    assert stamped is True
    after = await store.get(schedule.id)
    assert after is not None
    assert after.last_run_at == ran_at
    assert after.last_outcome == "ok"
    assert after.next_run_at == next_at
    assert after.session_id == "sess-1"


async def test_stamp_of_an_unknown_id_is_false() -> None:
    assert (
        await FakeSchedules().stamp(
            uuid4(), last_run_at=datetime.now(UTC), last_outcome="ok", next_run_at=None, session_id="", cleared_page_ids=()
        )
        is False
    )


# --- paused_at ------------------------------------------------------------------------------


async def test_update_flipping_enabled_true_to_false_stamps_paused_at() -> None:
    store = FakeSchedules()
    schedule = _schedule()
    await store.insert(schedule)
    now = datetime(2026, 8, 25, 9, 0, tzinfo=UTC)

    changed = await store.update(schedule.id, ScheduleUpdate(enabled=False, paused_at=now))

    assert changed is True
    updated = await store.get(schedule.id)
    assert updated is not None
    assert updated.enabled is False
    assert updated.paused_at == now


async def test_update_flipping_enabled_false_to_true_clears_paused_at() -> None:
    store = FakeSchedules()
    schedule = _schedule(enabled=False, paused_at=datetime(2026, 8, 25, 9, 0, tzinfo=UTC))
    await store.insert(schedule)

    changed = await store.update(schedule.id, ScheduleUpdate(enabled=True))

    assert changed is True
    updated = await store.get(schedule.id)
    assert updated is not None
    assert updated.enabled is True
    assert updated.paused_at is None


async def test_update_of_an_unrelated_field_leaves_paused_at_alone() -> None:
    store = FakeSchedules()
    schedule = _schedule()
    await store.insert(schedule)

    changed = await store.update(schedule.id, ScheduleUpdate(cadence="every 2h"))

    assert changed is True
    updated = await store.get(schedule.id)
    assert updated is not None
    assert updated.paused_at is None, "never disabled, so never stamped"


async def test_update_leaving_enabled_unset_leaves_a_stored_pause_alone() -> None:
    store = FakeSchedules()
    schedule = _schedule(enabled=False, paused_at=datetime(2026, 8, 25, 9, 0, tzinfo=UTC))
    await store.insert(schedule)

    changed = await store.update(schedule.id, ScheduleUpdate(cadence="every 2h"))

    assert changed is True
    updated = await store.get(schedule.id)
    assert updated is not None
    assert updated.paused_at == schedule.paused_at


def test_turning_a_schedule_off_stamps_its_pause_with_the_given_clock() -> None:
    now = datetime(2026, 8, 25, 9, 0, tzinfo=UTC)

    assert _schedule().paused_at_after(enabled=False, now=now) == now


def test_turning_a_paused_schedule_on_clears_its_pause() -> None:
    paused = _schedule(enabled=False, paused_at=datetime(2026, 8, 25, 9, 0, tzinfo=UTC))

    assert paused.paused_at_after(enabled=True, now=datetime(2026, 8, 26, 9, 0, tzinfo=UTC)) is None


@pytest.mark.parametrize("enabled", [None, False])
def test_a_change_that_does_not_flip_enabled_keeps_the_pause(enabled: bool | None) -> None:
    stamp = datetime(2026, 8, 25, 9, 0, tzinfo=UTC)
    paused = _schedule(enabled=False, paused_at=stamp)

    assert paused.paused_at_after(enabled=enabled, now=datetime(2026, 8, 26, 9, 0, tzinfo=UTC)) == stamp


def test_a_schedule_minted_disabled_is_paused_from_its_creation() -> None:
    created = datetime(2026, 8, 20, 8, 0, tzinfo=UTC)

    assert _schedule(enabled=False, created_at=created).paused_at == created


def test_a_schedule_minted_disabled_starts_paused_now() -> None:
    disabled = _schedule(enabled=False)

    assert disabled.paused_at is not None


def test_a_schedule_minted_enabled_has_no_paused_at() -> None:
    enabled = _schedule()

    assert enabled.paused_at is None


def test_a_schedule_minted_disabled_with_its_own_paused_at_keeps_it() -> None:
    stamp = datetime(2026, 8, 20, 8, 0, tzinfo=UTC)

    disabled = _schedule(enabled=False, paused_at=stamp)

    assert disabled.paused_at == stamp


class TestGateWaitSeconds:
    """The gate-policy vocabulary: wait forever, or fail after a stated number of seconds."""

    def test_wait_means_no_timeout(self) -> None:
        assert gate_wait_seconds("wait") is None

    def test_fail_after_names_its_seconds(self) -> None:
        assert gate_wait_seconds("fail_after:300") == 300.0

    def test_garbage_is_refused_naming_the_shape(self) -> None:
        with pytest.raises(ValueError, match=r"wait.*fail_after"):
            gate_wait_seconds("whenever")

    def test_a_non_positive_timeout_is_refused(self) -> None:
        with pytest.raises(ValueError, match="fail_after"):
            gate_wait_seconds("fail_after:0")
