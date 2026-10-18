"""SQLAlchemy adapters for the jobs table, the outbox and the wake-up, and the flush a unit of work runs at commit.

Every adapter opens a short session per call on the engine it is given, except `flush`, which
writes onto the session the caller is about to commit. A wake-up (`pg_notify` on `WAKE_CHANNEL`)
rides the same transaction as the rows it announces, so a listener never wakes for work that
rolled back.
"""

from __future__ import annotations

import asyncio

from contextlib import suppress
from typing import TYPE_CHECKING
from uuid import UUID

from pydantic import JsonValue
from sqlalchemy import ARRAY, Text, bindparam, insert, select, text, update
from sqlalchemy.dialects.postgresql import insert as pg_insert

from core.cqrs.domain import Enqueued, Job, JobPolicy, StoredEvent
from core.cqrs.infrastructure import schema
from core.db import Listener, build_session_factory

if TYPE_CHECKING:
    from collections.abc import Sequence
    from datetime import datetime

    from pydantic import BaseModel
    from sqlalchemy import ColumnElement
    from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

    from core.cqrs.buffer import CoordinationBuffer
    from core.cqrs.domain import Event

WAKE_CHANNEL = "coordination_wake"
"""The channel a job runner listens on. The payload is empty, the notification means "look"."""

type OutboxRow = dict[str, UUID | str | dict[str, JsonValue]]
"""One row to insert into the outbox: its event id, wire name and JSON payload."""

_LIVE_STATES = "state IN ('queued', 'running', 'failed') AND key <> ''"

CLAIM = text(
    """
    UPDATE coordination.jobs
       SET state = 'running', attempts = attempts + 1, locked_at = now(), locked_by = :worker,
           started_at = coalesce(started_at, now())
     WHERE id = (SELECT id FROM coordination.jobs
                  WHERE kind = ANY(:kinds)
                    AND ((state IN ('queued', 'failed') AND available_at <= now())
                      OR (state = 'running' AND locked_at < now() - make_interval(secs => lease_seconds)))
                  ORDER BY available_at, id
                  LIMIT 1
                  FOR UPDATE SKIP LOCKED)
    RETURNING id, kind, key, payload, state, attempts, max_attempts, available_at, locked_at, locked_by,
              lease_seconds, last_error, result, created_at, started_at, finished_at
    """
).bindparams(bindparam("kinds", type_=ARRAY(Text())))
"""One statement claims the next job. The subquery picks under `SKIP LOCKED`, so two job runners never
take the same row, and the running-with-expired-lease branch is how a dead job runner's job is taken over."""


def outbox_rows(events: Sequence[Event]) -> list[OutboxRow]:
    """The outbox rows for `events`: wire name, id, and a JSON-safe payload.

    Args:
        events: The events to store.

    Returns:
        One row per event, in order.
    """
    return [{"event_id": event.event_id, "event_type": type(event).__name__, "payload": event.model_dump(mode="json")} for event in events]


async def notify_wake(session: AsyncSession) -> None:
    """Announce on `WAKE_CHANNEL`, delivered when the session's transaction commits.

    Args:
        session: The caller's open session.
    """
    await session.execute(text("SELECT pg_notify(:channel, '')"), {"channel": WAKE_CHANNEL})


async def flush(session: AsyncSession, buffer: CoordinationBuffer) -> None:
    """Write what `buffer` holds onto `session` and clear it. You must call it right before `session.commit()`.

    Args:
        session: The session about to commit.
        buffer: The events and deliveries recorded during the transaction.
    """
    if buffer.events:
        await session.execute(insert(schema.outbox), outbox_rows(buffer.events))
    if buffer.deliveries:
        rows = [{"event_id": event_id, "subscriber": subscriber} for event_id, subscriber in buffer.deliveries]
        await session.execute(pg_insert(schema.event_deliveries).on_conflict_do_nothing(), rows)
    if buffer.events:
        await notify_wake(session)
    buffer.clear()



def _held(job_id: int, claimed_by: str | None) -> tuple[ColumnElement[bool], ...]:
    """The rows a finishing write may change: the job, and while `claimed_by` is given, only while that runner holds it."""
    if claimed_by is None:
        return (schema.jobs.c.id == job_id,)
    return (schema.jobs.c.id == job_id, schema.jobs.c.state == "running", schema.jobs.c.locked_by == claimed_by)

class PostgresJobs:
    """The `coordination.jobs` table."""

    def __init__(self, engine: AsyncEngine) -> None:
        """Build the session factory used for every call.

        Args:
            engine: The engine of the database that holds the table.
        """
        self._sessions = build_session_factory(engine)

    async def enqueue(self, kind: str, key: str, payload: BaseModel, *, policy: JobPolicy) -> Enqueued:
        """Insert a queued job and wake the job runners, or answer the live job that already holds `(kind, key)`.

        Args:
            kind: The job's kind.
            key: What the job is about.
            payload: The handler's input, stored as JSON.
            policy: The attempts and lease the job is stored with.

        Returns:
            The job's id, and whether this call created it.

        Raises:
            RuntimeError: When the live job vanished between the insert and the lookup, twice.
        """
        statement = (
            pg_insert(schema.jobs)
            .values(kind=kind, key=key, payload=payload.model_dump(mode="json"), max_attempts=policy.max_attempts, lease_seconds=policy.lease_seconds)
            .on_conflict_do_nothing(index_elements=[schema.jobs.c.kind, schema.jobs.c.key], index_where=text(_LIVE_STATES))
            .returning(schema.jobs.c.id)
        )
        existing = (
            select(schema.jobs.c.id)
            .where(schema.jobs.c.kind == kind, schema.jobs.c.key == key, schema.jobs.c.state.in_(("queued", "running", "failed")))
            .order_by(schema.jobs.c.id.desc())
            .limit(1)
        )
        for _ in range(2):
            async with self._sessions() as session, session.begin():
                inserted = (await session.execute(statement)).scalar_one_or_none()
                if inserted is not None:
                    await notify_wake(session)
                    return Enqueued(job_id=inserted, created=True)
                found = (await session.execute(existing)).scalar_one_or_none()
                if found is not None:
                    return Enqueued(job_id=found, created=False)
        msg = f"could not enqueue {kind} {key}: the live job vanished between the insert and the lookup, twice"
        raise RuntimeError(msg)

    async def claim(self, kinds: Sequence[str], *, worker: str) -> Job | None:
        """Lease the next claimable job to `worker` in one statement, skipping rows another job runner has locked.

        Args:
            kinds: The kinds the caller serves.
            worker: The lease holder's name.

        Returns:
            The claimed job, or `None` when there is none.
        """
        async with self._sessions() as session, session.begin():
            row = (await session.execute(CLAIM, {"kinds": list(kinds), "worker": worker})).mappings().first()
            return None if row is None else Job.model_validate(dict(row))

    async def heartbeat(self, job_id: int) -> None:
        """Renew the lease of a running job to the database's current time.

        Args:
            job_id: The job's id.
        """
        async with self._sessions() as session, session.begin():
            await session.execute(update(schema.jobs).where(schema.jobs.c.id == job_id, schema.jobs.c.state == "running").values(locked_at=text("now()")))

    async def complete(self, job_id: int, *, result: str, claimed_by: str | None = None) -> None:
        """Mark a job done at the database's current time.

        Args:
            job_id: The job's id.
            result: The handler's one-line result.
            claimed_by: The job runner that claimed the job, or `None` to change it whoever holds it.
        """
        async with self._sessions() as session, session.begin():
            await session.execute(
                update(schema.jobs)
                .where(*_held(job_id, claimed_by))
                .values(state="done", result=result, finished_at=text("now()"), locked_at=None, locked_by=None)
            )

    async def fail(self, job_id: int, *, error: str, retry_at: datetime | None, claimed_by: str | None = None) -> None:
        """Mark a job failed, to retry at `retry_at`, or abandoned when `retry_at` is `None`.

        Args:
            job_id: The job's id.
            error: What went wrong, in one line.
            retry_at: When the job may run again, or `None` to abandon it.
            claimed_by: The job runner that claimed the job, or `None` to change it whoever holds it.
        """
        if retry_at is None:
            values = {"state": "abandoned", "last_error": error, "finished_at": text("now()"), "locked_at": None, "locked_by": None}
        else:
            values = {"state": "failed", "last_error": error, "available_at": retry_at, "locked_at": None, "locked_by": None}
        async with self._sessions() as session, session.begin():
            await session.execute(update(schema.jobs).where(*_held(job_id, claimed_by)).values(**values))

    async def read(self, job_id: int) -> Job | None:
        """One job by id, read in a session of its own.

        Args:
            job_id: The job's id.

        Returns:
            The job, or `None` when no job has that id.
        """
        async with self._sessions() as session:
            row = (await session.execute(select(schema.jobs).where(schema.jobs.c.id == job_id))).mappings().first()
            return None if row is None else Job.model_validate(dict(row))

    async def recent_jobs(self, kind: str, key: str, *, limit: int = 50) -> list[Job]:
        """The newest jobs of `(kind, key)`, by id.

        Args:
            kind: The jobs' kind.
            key: The jobs' key.
            limit: How many jobs to answer at most.

        Returns:
            The jobs, newest first.
        """
        async with self._sessions() as session:
            rows = await session.execute(
                select(schema.jobs).where(schema.jobs.c.kind == kind, schema.jobs.c.key == key).order_by(schema.jobs.c.id.desc()).limit(limit)
            )
            return [Job.model_validate(dict(row)) for row in rows.mappings()]


class PostgresOutbox:
    """The `coordination.outbox` table and the `event_deliveries` table."""

    def __init__(self, engine: AsyncEngine) -> None:
        """Build the session factory used for every call.

        Args:
            engine: The engine of the database that holds the tables.
        """
        self._sessions = build_session_factory(engine)

    async def append(self, events: Sequence[Event]) -> None:
        """Store events for delivery in a transaction of their own and wake the job runners.

        Args:
            events: The events to store. Nothing is written when there are none.
        """
        if not events:
            return
        async with self._sessions() as session, session.begin():
            await session.execute(insert(schema.outbox), outbox_rows(events))
            await notify_wake(session)

    async def undelivered(self, *, limit: int = 100) -> list[StoredEvent]:
        """The oldest events not yet closed, by outbox id.

        Args:
            limit: How many events to answer at most.

        Returns:
            The events, oldest first.
        """
        async with self._sessions() as session:
            rows = await session.execute(
                select(schema.outbox.c.event_id, schema.outbox.c.event_type, schema.outbox.c.payload, schema.outbox.c.created_at)
                .where(schema.outbox.c.delivered_at.is_(None))
                .order_by(schema.outbox.c.id)
                .limit(limit)
            )
            return [StoredEvent.model_validate(dict(row)) for row in rows.mappings()]

    async def delivered(self, event_id: UUID, subscriber: str) -> bool:
        """Whether the `event_deliveries` table holds a row for `event_id` and `subscriber`.

        Args:
            event_id: The event's id.
            subscriber: The subscription's name.

        Returns:
            True when the delivery is recorded.
        """
        async with self._sessions() as session:
            found = await session.execute(
                select(schema.event_deliveries.c.event_id).where(
                    schema.event_deliveries.c.event_id == event_id, schema.event_deliveries.c.subscriber == subscriber
                )
            )
            return found.first() is not None

    async def record_delivery(self, event_id: UUID, subscriber: str) -> None:
        """Insert the delivery row, doing nothing when it is already there.

        Args:
            event_id: The event's id.
            subscriber: The subscription's name.
        """
        async with self._sessions() as session, session.begin():
            await session.execute(pg_insert(schema.event_deliveries).values(event_id=event_id, subscriber=subscriber).on_conflict_do_nothing())

    async def close(self, event_id: UUID) -> None:
        """Stamp the event's `delivered_at` with the database's current time.

        Args:
            event_id: The event's id.
        """
        async with self._sessions() as session, session.begin():
            await session.execute(update(schema.outbox).where(schema.outbox.c.event_id == event_id).values(delivered_at=text("now()")))


class PostgresWake:
    """Wakes on `WAKE_CHANNEL` notifications, on a dedicated listening connection.

    A burst of notifications wakes once: whatever arrived while a pass ran is drained before the
    next wait, so the job runner does one tick per burst, not one per notification.
    """

    def __init__(self, dsn: str) -> None:
        """Hold a `Listener` on `WAKE_CHANNEL`, not yet connected.

        Args:
            dsn: The database to listen on.
        """
        self._listener = Listener(dsn, WAKE_CHANNEL)

    async def wait(self, seconds: float) -> None:
        """Block until a notification arrives or `seconds` pass, then drain the notifications that queued meanwhile.

        Args:
            seconds: The longest the wait may last.
        """
        await self._listener.open()
        with suppress(TimeoutError):
            async with asyncio.timeout(seconds):
                await self._listener.get()
        self._listener.drain()

    async def close(self) -> None:
        """Close the listening connection."""
        await self._listener.close()
