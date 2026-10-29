"""The ports the scheduler reads and writes schedules through."""

from __future__ import annotations

from typing import TYPE_CHECKING, Protocol

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence
    from datetime import datetime
    from uuid import UUID

    from harness.features.scheduling.domain import Armed, Schedule, ScheduleUpdate


class Schedules(Protocol):
    """The stored schedules, loaded and changed one targeted write at a time."""

    async def insert(self, schedule: Schedule) -> None:
        """Add a schedule.

        Args:
            schedule: The schedule to add.
        """
        ...

    async def get(self, schedule_id: UUID) -> Schedule | None:
        """One schedule by its id.

        Args:
            schedule_id: The schedule's id.

        Returns:
            The schedule, or `None` when no schedule has that id.
        """
        ...

    async def update(self, schedule_id: UUID, changes: ScheduleUpdate) -> bool:
        """Apply only the set fields of `changes`.

        When `changes` sets `enabled`, its `paused_at` is saved as given in the same write, so `None` clears it.
        A change that leaves `enabled` unset leaves `paused_at` alone.
        When `changes.clears_next_fire` is set, the next fire is cleared in the same write and `changes.next_run_at` is ignored.
        When `changes.clears_pending_page_ids` is set, the pending changed pages are cleared in the same write.

        Args:
            schedule_id: The schedule's id.
            changes: The fields to change.

        Returns:
            Whether a schedule had that id.
        """
        ...

    async def delete(self, schedule_id: UUID) -> bool:
        """Remove one schedule.

        Args:
            schedule_id: The schedule's id.

        Returns:
            Whether a schedule had that id.
        """
        ...

    async def due(self, now: datetime) -> list[Schedule]:
        """Every enabled schedule whose `next_run_at` is at or before `now`.

        Args:
            now: The moment to compare against.

        Returns:
            The due schedules, earliest `next_run_at` first.
        """
        ...

    async def stamp(  # noqa: PLR0913 (one fire's tick log is one write)
        self,
        schedule_id: UUID,
        *,
        last_run_at: datetime,
        last_outcome: str,
        next_run_at: datetime | None,
        session_id: str,
        cleared_page_ids: Sequence[str],
    ) -> bool:
        """Record one fire in the tick log, with the runner session that made it, and remove the changed pages it passed on.

        Only the ids in `cleared_page_ids` leave the schedule's pending changed pages, so a page an arm added during the fire stays.
        When `cleared_page_ids` is not empty and pages stay pending, the schedule keeps the next fire it holds, the one that arm gave it,
        and `next_run_at` is ignored.

        Args:
            schedule_id: The schedule's id.
            last_run_at: When the fire happened.
            last_outcome: What the fire did, in words.
            next_run_at: When the schedule next comes due, or `None` to stop it coming due. Ignored when the stamp clears pages and others stay pending.
            session_id: The runner session the fire used.
            cleared_page_ids: The changed pages the fire passed on. Empty when the fire passed none on.

        Returns:
            Whether a schedule had that id.
        """
        ...

    async def arm(self, schedule_ids: Sequence[UUID], *, at: datetime, event_id: UUID | None = None) -> Armed:
        """Give every enabled schedule among `schedule_ids` the next fire `at`, keeping an earlier arm it already holds.

        With `event_id`, the event is recorded as handled in the same write, and an event recorded before arms nothing.

        Args:
            schedule_ids: The schedules to arm. An id no enabled schedule has is skipped.
            at: The next fire each armed schedule gets.
            event_id: The event this arming answers, or `None` when nothing will deliver it twice.

        Returns:
            Whether the event was a repeat, and which schedules will fire on the next tick.
        """
        ...

    async def arm_with_pages(self, fires: Mapping[UUID, datetime], *, event_id: UUID, page_ids: Sequence[str]) -> Armed:
        """Give every enabled schedule among `fires` the next fire it maps to, and add `page_ids` to its pending changed pages.

        The fire given replaces any fire the schedule already holds, and each page id is added once.
        The event is recorded as handled in the same write, and an event recorded before arms nothing.

        Args:
            fires: The next fire of each schedule to arm, by schedule id. An id no enabled schedule has is skipped.
            event_id: The event id of the index this arming answers.
            page_ids: The changed pages of that index.

        Returns:
            Whether the event was a repeat, and which schedules it armed.
        """
        ...


class ScheduleQueries(Protocol):
    """Reads over the stored schedules that change nothing."""

    async def list_all(self) -> list[Schedule]:
        """Every schedule.

        Returns:
            The schedules, in an order that stays the same for one adapter.
        """
        ...
