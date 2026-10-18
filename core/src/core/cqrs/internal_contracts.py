"""The ports of the job runner: the jobs table, the outbox, the wake-up, handlers and the unit of work."""

from __future__ import annotations

from typing import TYPE_CHECKING, Protocol, Self, runtime_checkable

if TYPE_CHECKING:
    from collections.abc import Callable, Sequence
    from datetime import datetime
    from uuid import UUID

    from pydantic import BaseModel

    from core.cqrs.domain import Enqueued, Event, Job, JobContext, JobPolicy, StoredEvent


class Jobs(Protocol):
    """The jobs table, changed one targeted write at a time."""

    async def enqueue(self, kind: str, key: str, payload: BaseModel, *, policy: JobPolicy) -> Enqueued:
        """Insert a queued job, or answer the live job that already holds `(kind, key)`.

        A live job is one that is queued, running or failed. An empty `key` never matches another job.

        Args:
            kind: The job's kind, which picks the handler that runs it.
            key: What the job is about, unique among the live jobs of its kind.
            payload: The handler's input, stored as JSON.
            policy: The attempts and lease the job is stored with.

        Returns:
            The job's id, and whether this call created it.
        """
        ...

    async def claim(self, kinds: Sequence[str], *, worker: str) -> Job | None:
        """Lease the next claimable job of one of `kinds` to `worker`.

        Claimable is queued or failed with its `available_at` reached, or running with a lease that has expired. The
        earliest `available_at` goes first, then the lowest id. Claiming counts one more attempt.

        Args:
            kinds: The kinds the caller serves.
            worker: The lease holder's name.

        Returns:
            The claimed job, or `None` when there is none.
        """
        ...

    async def heartbeat(self, job_id: int) -> None:
        """Renew the lease of a running job. A job that is not running is left as it is.

        Args:
            job_id: The job's id.
        """
        ...

    async def complete(self, job_id: int, *, result: str, claimed_by: str | None = None) -> None:
        """Mark a running job done and release its lease.

        With `claimed_by`, the job changes only while it is still running under that job runner, so a runner whose lease
        expired never overwrites the runner that claimed the job after it.

        Args:
            job_id: The job's id.
            result: The handler's one-line result.
            claimed_by: The job runner that claimed the job, or `None` to change it whoever holds it.
        """
        ...

    async def fail(self, job_id: int, *, error: str, retry_at: datetime | None, claimed_by: str | None = None) -> None:
        """Mark a running job failed, to retry at `retry_at`, or abandoned when `retry_at` is `None`.

        With `claimed_by`, the job changes only while it is still running under that job runner, as `complete` does.

        Args:
            job_id: The job's id.
            error: What went wrong, in one line.
            retry_at: When the job may run again, or `None` to abandon it.
            claimed_by: The job runner that claimed the job, or `None` to change it whoever holds it.
        """
        ...


class JobQueries(Protocol):
    """Reads over the jobs table that change nothing."""

    async def read(self, job_id: int) -> Job | None:
        """One job by id.

        Args:
            job_id: The job's id.

        Returns:
            The job, or `None` when no job has that id.
        """
        ...

    async def recent_jobs(self, kind: str, key: str, *, limit: int = 50) -> list[Job]:
        """The newest jobs of `(kind, key)`, in any state.

        Args:
            kind: The jobs' kind.
            key: The jobs' key.
            limit: How many jobs to answer at most.

        Returns:
            The jobs, newest first.
        """
        ...


class JobsAndQueries(Jobs, JobQueries, Protocol):
    """The jobs table with its writes and its reads, for a caller that changes jobs and reads them back."""


class Outbox(Protocol):
    """The outbox and the deliveries recorded against it."""

    async def append(self, events: Sequence[Event]) -> None:
        """Store events for delivery, in order, in a transaction of their own.

        Args:
            events: The events to store.
        """
        ...

    async def undelivered(self, *, limit: int = 100) -> list[StoredEvent]:
        """The oldest events not yet closed.

        Args:
            limit: How many events to answer at most.

        Returns:
            The events, oldest first.
        """
        ...

    async def delivered(self, event_id: UUID, subscriber: str) -> bool:
        """Whether `subscriber` already handled `event_id`.

        Args:
            event_id: The event's id.
            subscriber: The subscription's name.

        Returns:
            True when the delivery is recorded.
        """
        ...

    async def record_delivery(self, event_id: UUID, subscriber: str) -> None:
        """Record that `subscriber` handled `event_id`. Recording twice is harmless.

        Args:
            event_id: The event's id.
            subscriber: The subscription's name.
        """
        ...

    async def close(self, event_id: UUID) -> None:
        """Mark the event delivered to every subscriber, so `undelivered` no longer answers it.

        Args:
            event_id: The event's id.
        """
        ...


class Wake(Protocol):
    """How a job runner waits for work: it returns when notified or when the wait runs out."""

    async def wait(self, seconds: float) -> None:
        """Block until a notification arrives or `seconds` pass.

        Args:
            seconds: The longest the wait may last.
        """
        ...


class JobHandler[P: BaseModel](Protocol):
    """Runs one job of one kind and answers a one-line result. Raising means a retry or an abandoned job."""

    async def __call__(self, payload: P, context: JobContext) -> str:
        """Do the work described by `payload`.

        Args:
            payload: The job's validated input.
            context: The job being run.

        Returns:
            The one-line result stored on the job.
        """
        ...


class Periodic(Protocol):
    """Work a job runner runs on a cadence, such as a due-scan."""

    async def __call__(self) -> None:
        """Run one pass."""
        ...


@runtime_checkable
class UnitOfWork(Protocol):
    """A transactional boundary: commit explicitly, else roll back on exit.

    Events and deliveries recorded through it are written in the same transaction as the work when `commit` runs, and
    are dropped with it on rollback.
    """

    async def __aenter__(self) -> Self:
        """Begin the transactional scope.

        Returns:
            This unit of work.
        """
        ...

    async def __aexit__(self, *exc: object) -> None:
        """Leave the scope, rolling back unless `commit` was called.

        Args:
            *exc: The exception type, value and traceback, all `None` when the scope ended cleanly.
        """
        ...

    async def commit(self) -> None:
        """Persist the work done within this scope, with the recorded events and deliveries."""
        ...

    def record(self, *events: Event) -> None:
        """Record events to be written to the outbox when this scope commits.

        Args:
            *events: The events, in the order they happened.
        """
        ...

    def record_delivery(self, event_id: UUID, subscriber: str) -> None:
        """Record that `subscriber` has handled `event_id`, written when this scope commits.

        Args:
            event_id: The event's id.
            subscriber: The subscription's name.
        """
        ...


@runtime_checkable
class TransactionalSubscriber(Protocol):
    """A subscriber whose effect is a database write.

    The job runner opens `uow_factory()`, calls the subscriber with it, records the delivery on the same unit of work,
    and commits, so the effect and the delivery land together.
    """

    uow_factory: Callable[[], UnitOfWork]

    async def __call__(self, event: Event, uow: UnitOfWork) -> None:
        """React to the event through `uow`. The job runner commits, the subscriber never does.

        Args:
            event: The event being delivered.
            uow: The open unit of work to write through.
        """
        ...
