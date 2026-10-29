"""The schedule ports over the `public.schedules` table, through an asyncpg pool.

Every statement names the table with its schema. An unqualified name can resolve into `ag_catalog`.
"""

from __future__ import annotations

import json

from typing import TYPE_CHECKING

from core.db import open_asyncpg_pool

from harness.adapters.contracts import store_writer_pool_terms
from harness.features.scheduling.domain import ON_INDEX_SUBSCRIBER, ON_REFRESH_SUBSCRIBER, Armed, Schedule

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence
    from datetime import datetime
    from uuid import UUID

    import asyncpg

    from harness.features.scheduling.domain import ScheduleUpdate


def _schedule_from_row(row: asyncpg.Record) -> Schedule:
    """One row as a schedule."""
    arguments = row["arguments"]
    return Schedule(
        id=row["id"],
        workflow_id=row["workflow_id"],
        arguments=arguments if isinstance(arguments, dict) else json.loads(arguments),
        cadence=row["cadence"],
        enabled=row["enabled"],
        gate_policy=row["gate_policy"],
        gate_mode=row["gate_mode"],
        capacity_key=row["capacity_key"],
        capacity_limit=row["capacity_limit"],
        blackout=row["blackout"],
        catch_up=row["catch_up"],
        session_id=row["session_id"],
        created_at=row["created_at"],
        last_run_at=row["last_run_at"],
        last_outcome=row["last_outcome"],
        next_run_at=row["next_run_at"],
        paused_at=row["paused_at"],
        pending_page_ids=tuple(sorted(row["pending_page_ids"])),
    )


_COLUMNS = """id, workflow_id, arguments, cadence, enabled, gate_policy, gate_mode,
               capacity_key, capacity_limit, blackout, catch_up,
               session_id, created_at, last_run_at, last_outcome, next_run_at, paused_at, pending_page_ids"""


class PostgresSchedules:
    """The schedules and their reads, over an asyncpg pool into the workflow library's database.

    It takes an open pool and never opens one itself.
    """

    def __init__(self, pool: asyncpg.Pool) -> None:
        """Hold an open pool.

        Args:
            pool: The asyncpg pool into the workflow library's database.
        """
        self._pool = pool

    async def insert(self, schedule: Schedule) -> None:
        """Insert one row.

        Args:
            schedule: The schedule to add.
        """
        await self._pool.execute(
            f"""
            INSERT INTO public.schedules
              ({_COLUMNS})
            VALUES ($1, $2, $3::jsonb, $4, $5, $6, $7, $8, $9, $10, $11, $12, $13, $14, $15, $16, $17, $18::text[])
            """,
            schedule.id,
            schedule.workflow_id,
            json.dumps(schedule.arguments),
            schedule.cadence,
            schedule.enabled,
            schedule.gate_policy,
            schedule.gate_mode,
            schedule.capacity_key,
            schedule.capacity_limit,
            schedule.blackout,
            schedule.catch_up,
            schedule.session_id,
            schedule.created_at,
            schedule.last_run_at,
            schedule.last_outcome,
            schedule.next_run_at,
            schedule.paused_at,
            list(schedule.pending_page_ids),
        )

    async def get(self, schedule_id: UUID) -> Schedule | None:
        """Select one row by id.

        Args:
            schedule_id: The schedule's id.

        Returns:
            The schedule, or `None` when no row has that id.
        """
        row = await self._pool.fetchrow(f"SELECT {_COLUMNS} FROM public.schedules WHERE id = $1", schedule_id)
        return _schedule_from_row(row) if row is not None else None

    async def list_all(self) -> list[Schedule]:
        """Select every row, oldest first.

        Returns:
            The schedules, oldest first.
        """
        rows = await self._pool.fetch(f"SELECT {_COLUMNS} FROM public.schedules ORDER BY created_at")
        return [_schedule_from_row(row) for row in rows]

    async def update(self, schedule_id: UUID, changes: ScheduleUpdate) -> bool:
        """Update the set fields in one statement, writing `paused_at` as given whenever `enabled` is set and clearing the next fire when asked.

        Args:
            schedule_id: The schedule's id.
            changes: The fields to change.

        Returns:
            Whether a row had that id.
        """
        result = await self._pool.execute(
            """
            UPDATE public.schedules SET
              workflow_id        = COALESCE($2, workflow_id),
              arguments          = COALESCE($3::jsonb, arguments),
              cadence            = COALESCE($4, cadence),
              enabled            = COALESCE($5, enabled),
              gate_policy        = COALESCE($6, gate_policy),
              gate_mode          = COALESCE($7, gate_mode),
              capacity_key       = COALESCE($8, capacity_key),
              capacity_limit     = COALESCE($9, capacity_limit),
              blackout           = COALESCE($10, blackout),
              catch_up           = COALESCE($11, catch_up),
              next_run_at        = CASE WHEN $14 THEN NULL ELSE COALESCE($13, next_run_at) END,
              paused_at          = CASE WHEN $5 IS NULL THEN paused_at ELSE $12::timestamptz END
            WHERE id = $1
            """,
            schedule_id,
            changes.workflow_id,
            json.dumps(changes.arguments) if changes.arguments is not None else None,
            changes.cadence,
            changes.enabled,
            changes.gate_policy,
            changes.gate_mode,
            changes.capacity_key,
            changes.capacity_limit,
            changes.blackout,
            changes.catch_up,
            changes.paused_at,
            changes.next_run_at,
            changes.clears_next_fire,
        )
        return result != "UPDATE 0"

    async def delete(self, schedule_id: UUID) -> bool:
        """Delete one row.

        Args:
            schedule_id: The schedule's id.

        Returns:
            Whether a row had that id.
        """
        result = await self._pool.execute("DELETE FROM public.schedules WHERE id = $1", schedule_id)
        return result != "DELETE 0"

    async def due(self, now: datetime) -> list[Schedule]:
        """Select the enabled rows whose `next_run_at` is at or before `now`.

        Args:
            now: The moment to compare against.

        Returns:
            The due schedules, earliest `next_run_at` first.
        """
        rows = await self._pool.fetch(
            f"SELECT {_COLUMNS} FROM public.schedules WHERE enabled AND next_run_at <= $1 ORDER BY next_run_at", now
        )
        return [_schedule_from_row(row) for row in rows]

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
        """Update the tick log columns of one row and remove `cleared_page_ids` from its `pending_page_ids`, in one statement.

        Args:
            schedule_id: The schedule's id.
            last_run_at: When the fire happened.
            last_outcome: What the fire did, in words.
            next_run_at: When the schedule next comes due, or `None` to stop it coming due.
            session_id: The runner session the fire used.
            cleared_page_ids: The changed pages the fire passed on. Empty when the fire passed none on.

        Returns:
            Whether a row had that id.
        """
        result = await self._pool.execute(
            """
            UPDATE public.schedules SET
              last_run_at      = $2,
              last_outcome     = $3,
              next_run_at      = $4,
              session_id       = $5,
              pending_page_ids = ARRAY(SELECT unnest(pending_page_ids) EXCEPT SELECT unnest($6::text[]))
            WHERE id = $1
            """,
            schedule_id,
            last_run_at,
            last_outcome,
            next_run_at,
            session_id,
            list(cleared_page_ids),
        )
        return result != "UPDATE 0"

    async def arm(self, schedule_ids: Sequence[UUID], *, at: datetime, event_id: UUID | None = None) -> Armed:
        """Record the event in `coordination.event_deliveries` and set the next fires in one transaction, so a repeat changes nothing.

        Args:
            schedule_ids: The schedules to arm. An id no enabled row has is skipped.
            at: The next fire each armed row gets.
            event_id: The event this arming answers, or `None` when nothing will deliver it twice.

        Returns:
            Whether the event was a repeat, and which schedules will fire on the next tick.
        """
        async with self._pool.acquire() as connection, connection.transaction():
            if event_id is not None:
                recorded = await connection.fetchval(
                    "INSERT INTO coordination.event_deliveries (event_id, subscriber) VALUES ($1, $2) ON CONFLICT DO NOTHING RETURNING event_id",
                    event_id,
                    ON_REFRESH_SUBSCRIBER,
                )
                if recorded is None:
                    return Armed(repeat=True)
            rows = await connection.fetch(
                "UPDATE public.schedules SET next_run_at = COALESCE(next_run_at, $2) WHERE id = ANY($1::uuid[]) AND enabled RETURNING id",
                list(schedule_ids),
                at,
            )
        return Armed(schedule_ids=tuple(row["id"] for row in rows))

    async def arm_with_pages(self, fires: Mapping[UUID, datetime], *, event_id: UUID, page_ids: Sequence[str]) -> Armed:
        """Record the event in `coordination.event_deliveries` under the on-index subscriber, then move each fire and add the ids, in one transaction.

        Args:
            fires: The next fire of each schedule to arm, by schedule id. An id no enabled row has is skipped.
            event_id: The event id of the index this arming answers.
            page_ids: The changed pages of that index.

        Returns:
            Whether the event was a repeat, and which schedules it armed.
        """
        armed: list[UUID] = []
        async with self._pool.acquire() as connection, connection.transaction():
            recorded = await connection.fetchval(
                "INSERT INTO coordination.event_deliveries (event_id, subscriber) VALUES ($1, $2) ON CONFLICT DO NOTHING RETURNING event_id",
                event_id,
                ON_INDEX_SUBSCRIBER,
            )
            if recorded is None:
                return Armed(repeat=True)
            for schedule_id, at in fires.items():
                updated = await connection.fetchval(
                    """
                    UPDATE public.schedules SET
                      next_run_at      = $2,
                      pending_page_ids = ARRAY(SELECT DISTINCT unnest(pending_page_ids || $3::text[]))
                    WHERE id = $1 AND enabled
                    RETURNING id
                    """,
                    schedule_id,
                    at,
                    list(page_ids),
                )
                if updated is not None:
                    armed.append(updated)
        return Armed(schedule_ids=tuple(armed))


async def schedule_pool() -> asyncpg.Pool:
    """Open a writer pool over the workflow library's database, where the schedules live.

    Returns:
        The open pool. The caller closes it.

    Raises:
        RuntimeError: asyncpg returned no pool.
    """
    dsn, ssl = store_writer_pool_terms()
    return await open_asyncpg_pool(dsn, ssl=ssl, target="to the schedule store")
