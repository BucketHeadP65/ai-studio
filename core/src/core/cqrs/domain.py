"""The messages on the bus and the records of the job runner: jobs, their policy and stored events.

A `Command` is handled by exactly one handler. An `Event` is a fact observed by zero or more subscribers after its
transaction commits. Every record here is frozen, so nothing can change a message or a job underneath its reader.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import Literal
from uuid import UUID, uuid4

from pydantic import BaseModel, ConfigDict, Field, JsonValue

_IDENTITY: set[str] = {"event_id", "occurred_at"}

type JobState = Literal["queued", "running", "failed", "done", "abandoned"]
"""`queued` waits for a job runner, `running` is leased to one, `failed` waits for its retry time,
`done` and `abandoned` are final, `abandoned` meaning the attempts are spent and a person must look."""

type Clock = Callable[[], datetime]
"""A function that answers the current time."""


def utcnow() -> datetime:
    """The default clock: the current time in UTC.

    Returns:
        A timezone-aware UTC datetime.
    """
    return datetime.now(UTC)


class Message(BaseModel):
    """The frozen base of every command and event on the bus."""

    model_config = ConfigDict(frozen=True)


class Command(Message):
    """An intent handled by exactly one handler."""


class Event(Message):
    """A fact emitted during handling and delivered to every subscriber of its type.

    `event_id` is the delivery identity: the outbox keeps one row per id and the deliveries are keyed on it, so the same
    fact reaching a subscriber twice is recognisable. Equality and hashing ignore `event_id` and `occurred_at`, so two
    events with the same content are the same fact.
    """

    event_id: UUID = Field(default_factory=uuid4)
    occurred_at: datetime = Field(default_factory=utcnow)

    def __eq__(self, other: object) -> bool:
        """Whether `other` is an event of the same type with the same content.

        Args:
            other: The object to compare with.

        Returns:
            True for the same type and content, `NotImplemented` for anything that is not an event of this type.
        """
        if not isinstance(other, Event) or type(other) is not type(self):
            return NotImplemented
        return self.model_dump(exclude=_IDENTITY) == other.model_dump(exclude=_IDENTITY)

    def __hash__(self) -> int:
        """A hash of the event's type and content, the same for two equal events.

        Returns:
            The hash.
        """
        return hash((type(self).__name__, self.model_dump_json(exclude=_IDENTITY)))


class JobPolicy(BaseModel):
    """How many times a job may run and how the retries space out.

    Backoff after the n-th failed attempt is `min(base * 2**(n-1), cap)` seconds. The lease is how long a running job
    may go without a heartbeat before another job runner may take it over.
    """

    model_config = ConfigDict(frozen=True)

    max_attempts: int = 3
    lease_seconds: int = 300
    backoff_base_seconds: float = 30.0
    backoff_cap_seconds: float = 900.0

    def retry_at(self, attempts: int, now: datetime, *, max_attempts: int | None = None) -> datetime | None:
        """When the next attempt may start after `attempts` have run.

        Args:
            attempts: How many attempts have run.
            now: The time the backoff is counted from.
            max_attempts: The ceiling stored on the job, used in place of the policy's own when given.

        Returns:
            The time of the next attempt, or `None` once the attempts are spent.
        """
        ceiling = self.max_attempts if max_attempts is None else max_attempts
        if attempts >= ceiling:
            return None
        delay = min(self.backoff_base_seconds * (2 ** (attempts - 1)), self.backoff_cap_seconds)
        return now + timedelta(seconds=delay)


class Job(BaseModel):
    """One row of the jobs table, as a job runner or a reader of recent jobs sees it."""

    model_config = ConfigDict(frozen=True)

    id: int
    kind: str
    key: str
    payload: dict[str, JsonValue]
    state: JobState
    attempts: int
    max_attempts: int
    available_at: datetime
    locked_at: datetime | None
    locked_by: str | None
    lease_seconds: int
    last_error: str | None
    result: str | None
    created_at: datetime
    started_at: datetime | None
    finished_at: datetime | None


def is_claimable(job: Job, now: datetime) -> bool:
    """Whether a job runner may claim `job` at `now`.

    A queued or failed job is claimable once its `available_at` is reached. A running job is claimable once its lease
    has expired, which is `locked_at` plus `lease_seconds` before `now`. A done or abandoned job never is.

    Args:
        job: The job as it is stored.
        now: The time the claim happens at.

    Returns:
        True when the job may be claimed.
    """
    if job.state in ("queued", "failed"):
        return job.available_at <= now
    if job.state == "running":
        return job.locked_at is not None and job.locked_at + timedelta(seconds=job.lease_seconds) < now
    return False


class Enqueued(BaseModel):
    """What enqueueing a job answers: the id to watch, and whether this call created it."""

    model_config = ConfigDict(frozen=True)

    job_id: int
    created: bool


class StoredEvent(BaseModel):
    """One undelivered outbox row."""

    model_config = ConfigDict(frozen=True)

    event_id: UUID
    event_type: str
    payload: dict[str, JsonValue]
    created_at: datetime


class JobContext(BaseModel):
    """What a handler knows about the job it is running."""

    model_config = ConfigDict(frozen=True)

    job_id: int
    kind: str
    key: str
    attempt: int
