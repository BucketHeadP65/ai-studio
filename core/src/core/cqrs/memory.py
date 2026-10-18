"""In-memory adapters for the jobs table, the outbox and the wake-up.

They serve two purposes with one implementation: the default outbox of a bus in a one-shot
process, and the doubles every test suite builds on. State is public and plain so a test can
script and assert it directly.
"""

from __future__ import annotations

from itertools import count
from typing import TYPE_CHECKING

from core.cqrs.domain import Clock, Enqueued, Job, JobPolicy, StoredEvent, is_claimable, utcnow

if TYPE_CHECKING:
    from collections.abc import Sequence
    from datetime import datetime
    from uuid import UUID

    from pydantic import BaseModel

    from core.cqrs.domain import Event

_LIVE = frozenset({"queued", "running", "failed"})


class MemoryJobs:
    """A jobs table in a dict, with the same claim rules the Postgres adapter gets from its indexes."""

    def __init__(self, *, clock: Clock = utcnow) -> None:
        """Start with no jobs.

        Args:
            clock: What every timestamp is read from.
        """
        self.jobs: dict[int, Job] = {}
        self._ids = count(1)
        self._clock = clock

    async def enqueue(self, kind: str, key: str, payload: BaseModel, *, policy: JobPolicy) -> Enqueued:
        """Insert a queued job into the dict, or answer the live job that already holds `(kind, key)`.

        Args:
            kind: The job's kind.
            key: What the job is about.
            payload: The handler's input, stored as JSON.
            policy: The attempts and lease the job is stored with.

        Returns:
            The job's id, and whether this call created it.
        """
        if key:
            for job in self.jobs.values():
                if job.kind == kind and job.key == key and job.state in _LIVE:
                    return Enqueued(job_id=job.id, created=False)
        now = self._clock()
        job_id = next(self._ids)
        self.jobs[job_id] = Job(
            id=job_id,
            kind=kind,
            key=key,
            payload=payload.model_dump(mode="json"),
            state="queued",
            attempts=0,
            max_attempts=policy.max_attempts,
            available_at=now,
            locked_at=None,
            locked_by=None,
            lease_seconds=policy.lease_seconds,
            last_error=None,
            result=None,
            created_at=now,
            started_at=None,
            finished_at=None,
        )
        return Enqueued(job_id=job_id, created=True)

    async def claim(self, kinds: Sequence[str], *, worker: str) -> Job | None:
        """Lease the earliest claimable job in the dict to `worker`.

        Args:
            kinds: The kinds the caller serves.
            worker: The lease holder's name.

        Returns:
            The claimed job, or `None` when there is none.
        """
        now = self._clock()
        candidates = sorted(
            (job for job in self.jobs.values() if job.kind in kinds and is_claimable(job, now)),
            key=lambda job: (job.available_at, job.id),
        )
        if not candidates:
            return None
        job = candidates[0]
        claimed = job.model_copy(
            update={
                "state": "running",
                "attempts": job.attempts + 1,
                "locked_at": now,
                "locked_by": worker,
                "started_at": job.started_at or now,
            }
        )
        self.jobs[job.id] = claimed
        return claimed

    async def heartbeat(self, job_id: int) -> None:
        """Renew a running job's lease to the clock's current time.

        Args:
            job_id: The job's id.
        """
        job = self.jobs[job_id]
        if job.state == "running":
            self.jobs[job_id] = job.model_copy(update={"locked_at": self._clock()})

    async def complete(self, job_id: int, *, result: str, claimed_by: str | None = None) -> None:
        """Mark a job in the dict done with `result`.

        Args:
            job_id: The job's id.
            result: The handler's one-line result.
            claimed_by: The job runner that claimed the job, or `None` to change it whoever holds it.
        """
        if not self._held(job_id, claimed_by):
            return
        self.jobs[job_id] = self.jobs[job_id].model_copy(
            update={"state": "done", "result": result, "finished_at": self._clock(), "locked_at": None, "locked_by": None}
        )

    async def fail(self, job_id: int, *, error: str, retry_at: datetime | None, claimed_by: str | None = None) -> None:
        """Mark a job in the dict failed to retry at `retry_at`, or abandoned when `retry_at` is `None`.

        Args:
            job_id: The job's id.
            error: What went wrong, in one line.
            retry_at: When the job may run again, or `None` to abandon it.
            claimed_by: The job runner that claimed the job, or `None` to change it whoever holds it.
        """
        if not self._held(job_id, claimed_by):
            return
        job = self.jobs[job_id]
        if retry_at is None:
            update = {"state": "abandoned", "last_error": error, "finished_at": self._clock(), "locked_at": None, "locked_by": None}
        else:
            update = {"state": "failed", "last_error": error, "available_at": retry_at, "locked_at": None, "locked_by": None}
        self.jobs[job_id] = job.model_copy(update=update)

    def _held(self, job_id: int, claimed_by: str | None) -> bool:
        """Whether a finishing write may change the job: always without `claimed_by`, else only while that runner holds it."""
        job = self.jobs[job_id]
        return claimed_by is None or (job.state == "running" and job.locked_by == claimed_by)

    async def read(self, job_id: int) -> Job | None:
        """One job from the dict by id.

        Args:
            job_id: The job's id.

        Returns:
            The job, or `None` when no job has that id.
        """
        return self.jobs.get(job_id)

    async def recent_jobs(self, kind: str, key: str, *, limit: int = 50) -> list[Job]:
        """The newest jobs of `(kind, key)` in the dict, by id.

        Args:
            kind: The jobs' kind.
            key: The jobs' key.
            limit: How many jobs to answer at most.

        Returns:
            The jobs, newest first.
        """
        matching = [job for job in self.jobs.values() if job.kind == kind and job.key == key]
        return sorted(matching, key=lambda job: job.id, reverse=True)[:limit]


class MemoryOutbox:
    """An outbox and its deliveries in lists and sets."""

    def __init__(self, *, clock: Clock = utcnow) -> None:
        """Start with no rows and no deliveries.

        Args:
            clock: What each row's `created_at` is read from.
        """
        self.rows: list[StoredEvent] = []
        self.appended: list[Event] = []
        self.deliveries: set[tuple[UUID, str]] = set()
        self.closed: set[UUID] = set()
        self._clock = clock

    async def append(self, events: Sequence[Event]) -> None:
        """Store `events` as undelivered rows in the list, in order.

        Args:
            events: The events to store.
        """
        now = self._clock()
        for event in events:
            self.appended.append(event)
            self.rows.append(
                StoredEvent(
                    event_id=event.event_id,
                    event_type=type(event).__name__,
                    payload=event.model_dump(mode="json"),
                    created_at=now,
                )
            )

    async def undelivered(self, *, limit: int = 100) -> list[StoredEvent]:
        """The rows in the list not yet closed.

        Args:
            limit: How many rows to answer at most.

        Returns:
            The rows, oldest first.
        """
        return [row for row in self.rows if row.event_id not in self.closed][:limit]

    async def delivered(self, event_id: UUID, subscriber: str) -> bool:
        """Whether the set holds the delivery of `event_id` to `subscriber`.

        Args:
            event_id: The event's id.
            subscriber: The subscription's name.

        Returns:
            True when the delivery is recorded.
        """
        return (event_id, subscriber) in self.deliveries

    async def record_delivery(self, event_id: UUID, subscriber: str) -> None:
        """Add the delivery of `event_id` to `subscriber` to the set. Recording twice is harmless.

        Args:
            event_id: The event's id.
            subscriber: The subscription's name.
        """
        self.deliveries.add((event_id, subscriber))

    async def close(self, event_id: UUID) -> None:
        """Mark `event_id` delivered to every subscriber, hiding it from `undelivered`.

        Args:
            event_id: The event's id.
        """
        self.closed.add(event_id)


class ImmediateWake:
    """A wake-up that never waits. Records every wait it was asked for."""

    def __init__(self) -> None:
        self.waits: list[float] = []

    async def wait(self, seconds: float) -> None:
        """Return at once, recording `seconds` in `waits`.

        Args:
            seconds: The wait that was asked for.
        """
        self.waits.append(seconds)
