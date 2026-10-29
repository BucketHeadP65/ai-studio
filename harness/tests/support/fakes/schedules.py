"""An in-memory `Schedules` and `ScheduleQueries`, for a test that needs standing schedules and no database."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from harness.features.scheduling.domain import Armed

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence
    from datetime import datetime
    from uuid import UUID

    from harness.features.scheduling.domain import Schedule, ScheduleUpdate


@dataclass
class FakeSchedules:
    """`Schedules` and `ScheduleQueries` over a dict, keyed by schedule id."""

    rows: dict[UUID, Schedule] = field(default_factory=dict)
    stamp_error: Exception | None = None
    """Raised by every `stamp` when set: a store that fails while a fire records its outcome."""
    received: set[UUID] = field(default_factory=set)
    """Every event id `arm` recorded, standing in for `coordination.event_deliveries`."""
    received_index: set[UUID] = field(default_factory=set)
    """Every event id `arm_with_pages` recorded, standing in for `coordination.event_deliveries` under the on-index subscriber."""

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
            "gate_mode": changes.gate_mode,
            "capacity_key": changes.capacity_key,
            "capacity_limit": changes.capacity_limit,
            "blackout": changes.blackout,
            "catch_up": changes.catch_up,
            "next_run_at": changes.next_run_at,
        }
        updates = {k: v for k, v in fields.items() if v is not None}
        if changes.enabled is not None:
            updates["paused_at"] = changes.paused_at
        if changes.clears_next_fire:
            updates["next_run_at"] = None
        self.rows[schedule_id] = current.model_copy(update=updates)
        return True

    async def delete(self, schedule_id: UUID) -> bool:
        return self.rows.pop(schedule_id, None) is not None

    async def due(self, now: datetime) -> list[Schedule]:
        candidates = [
            (row.next_run_at, row) for row in self.rows.values() if row.enabled and row.next_run_at is not None and row.next_run_at <= now
        ]
        return [row for _, row in sorted(candidates, key=lambda pair: pair[0])]

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
        if self.stamp_error is not None:
            raise self.stamp_error
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

    async def arm(self, schedule_ids: Sequence[UUID], *, at: datetime, event_id: UUID | None = None) -> Armed:
        if event_id is not None:
            if event_id in self.received:
                return Armed(repeat=True)
            self.received.add(event_id)
        armed: list[UUID] = []
        for schedule_id in schedule_ids:
            row = self.rows.get(schedule_id)
            if row is None or not row.enabled:
                continue
            if row.next_run_at is None:
                self.rows[schedule_id] = row.model_copy(update={"next_run_at": at})
            armed.append(schedule_id)
        return Armed(schedule_ids=tuple(armed))

    async def arm_with_pages(self, fires: Mapping[UUID, datetime], *, event_id: UUID, page_ids: Sequence[str]) -> Armed:
        if event_id in self.received_index:
            return Armed(repeat=True)
        self.received_index.add(event_id)
        armed: list[UUID] = []
        for schedule_id, at in fires.items():
            row = self.rows.get(schedule_id)
            if row is None or not row.enabled:
                continue
            pending = tuple(sorted({*row.pending_page_ids, *page_ids}))
            self.rows[schedule_id] = row.model_copy(update={"next_run_at": at, "pending_page_ids": pending})
            armed.append(schedule_id)
        return Armed(schedule_ids=tuple(armed))
