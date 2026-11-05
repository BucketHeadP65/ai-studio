"""The refresh of one registered knowledge base: its job, its three events, its history, and when its cadence comes due.

A refresh runs as a job of type `kb.refresh`, keyed by the knowledge base's registry name.
It announces `RefreshStarted` when a pass begins, `RefreshCompleted` when it runs to its end and `RefreshAbandoned` when its last
attempt fails.
A pass whose scan could not read a watch-list entry still runs to its end, and its completion names each such entry with its reason.
All three carry the job id, so a start pairs with its ending in whatever order they are read, and both endings carry what the job brought in.
A refresh appears in the history only once it is over.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import TYPE_CHECKING, Literal

from core.cqrs.contracts import Event, JobPolicy
from pydantic import BaseModel, ConfigDict, Field, TypeAdapter, ValidationError

if TYPE_CHECKING:
    from collections.abc import Iterable, Mapping, Sequence

    from core.cqrs.contracts import Job
    from pydantic import JsonValue

    from studio.knowledge_base.settings import Schedule

RefreshOutcome = Literal["ok", "partial", "failed"]
"""How a refresh ended: `ok` ran to its end, `partial` did so with a watch-list entry it could not scan, `failed` was abandoned."""
RefreshTrigger = Literal["manual", "schedule"]

REFRESH_KIND = "kb.refresh"
"""The job type a refresh runs under. The job key is the knowledge base's registry name."""

REFRESH_TOPIC = "knowledge-base"
"""The root of the change-notification topic a refresh job's transitions are announced under, beside the `jobs` topic.

The knowledge base's registry name is appended to it, so the topic a subscriber reads is `knowledge-base.<registry name>`.
"""

REFRESH_POLICY = JobPolicy(max_attempts=3, lease_seconds=1800, backoff_base_seconds=30.0, backoff_cap_seconds=600.0)
"""Three attempts, a 30 minute lease, and a 30 second backoff doubling to a 10 minute cap.

The worker's heartbeat renews the lease while a long index runs, so the lease expires only for a worker that died.
A retry keeps the rows an earlier attempt wrote and picks up what is still missing.
"""

_TRIGGER = TypeAdapter[RefreshTrigger](RefreshTrigger)


class RefreshJob(BaseModel):
    """The payload of one refresh job: which registered knowledge base, and what asked for it."""

    model_config = ConfigDict(frozen=True)

    knowledge_base: str
    """The registry name of the knowledge base to refresh."""
    trigger: RefreshTrigger = "manual"
    """`manual` for a pass the HTTP route queued, `schedule` for one the ticker queued."""


def _summed(left: Mapping[str, int], right: Mapping[str, int]) -> dict[str, int]:
    """Two label-to-count maps added together."""
    return {label: left.get(label, 0) + right.get(label, 0) for label in {*left, *right}}


class RefreshCounts(BaseModel):
    """What a refresh brought in: the sources its sync created, and the new sources by watch-list entry and by connector."""

    model_config = ConfigDict(frozen=True)

    sources_created: int = 0
    """The mirrors the sync created."""
    requested_by_entry: dict[str, int] = Field(default_factory=dict)
    """The rows each watch-list entry added to the source queue, by the entry's label without URL secrets.

    An entry that added none is absent.
    """
    created_by_connector: dict[str, int] = Field(default_factory=dict)
    """The mirrors each connector created, by connector name. A connector that created none is absent."""

    def plus(self, other: RefreshCounts) -> RefreshCounts:
        """These counts and `other` added together, label by label.

        Args:
            other: The counts to add.

        Returns:
            The sums.
        """
        return RefreshCounts(
            sources_created=self.sources_created + other.sources_created,
            requested_by_entry=_summed(self.requested_by_entry, other.requested_by_entry),
            created_by_connector=_summed(self.created_by_connector, other.created_by_connector),
        )


class ScanFailure(BaseModel):
    """One watch-list entry a refresh's scan could not read, and why."""

    model_config = ConfigDict(frozen=True)

    entry: str
    """The entry's label, `<scanner>:<ref>`, with no user part, query or fragment on a URL in it."""
    reason: str
    """One line saying why the scan failed, as the scan recorded it."""


class RefreshResult(BaseModel):
    """What a pass that ran to its end stores as its job's result, as one JSON line: its detail, its counts and its scan failures."""

    model_config = ConfigDict(frozen=True)

    detail: str = ""
    counts: RefreshCounts = Field(default_factory=RefreshCounts)
    scan_failures: tuple[ScanFailure, ...] = ()
    """The watch-list entries the pass's scan could not read. Empty when every entry was read or the pass scanned nothing."""


class RefreshStarted(Event):
    """One refresh pass began: which knowledge base, which job, and what asked for it."""

    knowledge_base: str
    """The registry name of the knowledge base."""
    job_id: int
    trigger: RefreshTrigger = "manual"


class RefreshCompleted(Event):
    """One refresh pass ran to its end, carrying the one-sentence detail it answered and the watch-list entries it could not scan."""

    knowledge_base: str
    """The registry name of the knowledge base."""
    job_id: int
    trigger: RefreshTrigger = "manual"
    detail: str = ""
    counts: RefreshCounts = Field(default_factory=RefreshCounts)
    """What every attempt of the job brought in."""
    scan_failures: tuple[ScanFailure, ...] = ()
    """The watch-list entries the last attempt's scan could not read."""


class RefreshAbandoned(Event):
    """One refresh pass used up its attempts, carrying the last error word for word."""

    knowledge_base: str
    """The registry name of the knowledge base."""
    job_id: int
    trigger: RefreshTrigger = "manual"
    reason: str = ""
    """The message of the error the last attempt raised."""
    counts: RefreshCounts = Field(default_factory=RefreshCounts)
    """What every attempt of the job brought in."""


type RefreshEvent = RefreshStarted | RefreshCompleted | RefreshAbandoned
"""The three events a refresh announces."""


def is_forwarded(ending: RefreshCompleted | RefreshAbandoned) -> bool:
    """Whether the harness hears about this ending: every completed refresh run, and an abandoned one that created sources.

    Args:
        ending: How one refresh run ended.

    Returns:
        True when the ending is posted to the harness.
    """
    return isinstance(ending, RefreshCompleted) or ending.counts.sources_created > 0


class RefreshRun(BaseModel):
    """One finished refresh: when it started and finished, how it ended, its detail, what it brought in and what it could not scan.

    `started_at` and `finished_at` are RFC 3339 times in UTC.
    """

    model_config = ConfigDict(frozen=True)

    started_at: datetime
    finished_at: datetime
    outcome: RefreshOutcome
    detail: str
    trigger: RefreshTrigger = "manual"
    """`manual` for a pass the HTTP route queued, `schedule` for one the ticker queued."""
    counts: RefreshCounts = Field(default_factory=RefreshCounts)
    """What the refresh brought in. An abandoned run read back from its job carries zeros."""
    scan_failures: tuple[ScanFailure, ...] = ()
    """The watch-list entries the refresh could not scan, each with its reason. An abandoned run carries none."""


def _trigger_of(payload: Mapping[str, JsonValue]) -> RefreshTrigger:
    """The payload's trigger, `manual` when it carries none this shape recognises."""
    try:
        return _TRIGGER.validate_python(payload.get("trigger"))
    except ValidationError:
        return "manual"


def _result_of(text: str | None) -> RefreshResult:
    """A done job's stored result, the document a completed pass writes, or an empty result when it stored none."""
    if not text:
        return RefreshResult()
    return RefreshResult.model_validate_json(text)


def refresh_facts(job: Job) -> tuple[RefreshEvent, ...]:
    """The events one job row stands for: a start and its ending, or nothing while the pass is unfinished.

    A `done` job reads as a `RefreshCompleted` carrying the detail, the counts and the scan failures its result holds.
    An `abandoned` job reads as a `RefreshAbandoned` carrying its last error.
    A time the job never recorded falls back to an earlier one, so both events always carry a time.

    Args:
        job: One `kb.refresh` job row.

    Returns:
        The start and its ending, or an empty tuple for a job that is queued, running or waiting to retry.

    Raises:
        ValidationError: A `done` job's result is not the document a completed pass writes.
    """
    started_at = job.started_at or job.created_at
    trigger = _trigger_of(job.payload)
    started = RefreshStarted(knowledge_base=job.key, job_id=job.id, trigger=trigger, occurred_at=started_at)
    finished_at = job.finished_at or started_at
    if job.state == "done":
        result = _result_of(job.result)
        completed = RefreshCompleted(
            knowledge_base=job.key,
            job_id=job.id,
            trigger=trigger,
            detail=result.detail,
            counts=result.counts,
            scan_failures=result.scan_failures,
            occurred_at=finished_at,
        )
        return (started, completed)
    if job.state == "abandoned":
        return (
            started,
            RefreshAbandoned(knowledge_base=job.key, job_id=job.id, trigger=trigger, reason=job.last_error or "", occurred_at=finished_at),
        )
    return ()


def refresh_run_of(started: RefreshStarted, ending: RefreshCompleted | RefreshAbandoned) -> RefreshRun:
    """One start and its ending as a finished refresh run.

    Args:
        started: The event the pass began with.
        ending: The event the pass ended with.

    Returns:
        The run: `ok` for a completed pass, `partial` for a completed pass with a scan failure, `failed` for an abandoned one.
    """
    if isinstance(ending, RefreshCompleted):
        outcome: RefreshOutcome = "partial" if ending.scan_failures else "ok"
        detail = ending.detail
        scan_failures = ending.scan_failures
    else:
        outcome = "failed"
        detail = ending.reason
        scan_failures = ()
    return RefreshRun(
        started_at=started.occurred_at,
        finished_at=ending.occurred_at,
        outcome=outcome,
        detail=detail,
        trigger=ending.trigger,
        counts=ending.counts,
        scan_failures=scan_failures,
    )


def refresh_runs(events: Iterable[RefreshEvent], *, limit: int = 20) -> tuple[RefreshRun, ...]:
    """Every finished refresh among `events`, in the order their endings arrive.

    An ending pairs with the start that carries the same job id.
    An ending whose start is not among `events` is left out.

    Args:
        events: Refresh events in any order.
        limit: The most runs to return.

    Returns:
        The runs, at most `limit` of them.
    """
    facts: Sequence[RefreshEvent] = tuple(events)
    starts = {fact.job_id: fact for fact in facts if isinstance(fact, RefreshStarted)}
    runs: list[RefreshRun] = []
    for fact in facts:
        if isinstance(fact, RefreshStarted):
            continue
        started = starts.get(fact.job_id)
        if started is not None:
            runs.append(refresh_run_of(started, fact))
    return tuple(runs[:limit])


def refresh_history(jobs: Sequence[Job], *, limit: int = 20) -> tuple[RefreshRun, ...]:
    """The finished refreshes among `jobs` as runs, in the order given.

    Args:
        jobs: `kb.refresh` job rows.
        limit: The most runs to return.

    Returns:
        The runs, at most `limit` of them.

    Raises:
        ValidationError: A `done` job's result is not the document a completed pass writes.
    """
    return refresh_runs([fact for job in jobs for fact in refresh_facts(job)], limit=limit)


def _daily_boundary(daily_at: str, now: datetime) -> datetime:
    """The most recent `daily_at` clock time, read as `HH:MM` in UTC, at or before `now`."""
    hour, minute = (int(part) for part in daily_at.split(":"))
    boundary = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
    return boundary if boundary <= now else boundary - timedelta(days=1)


def is_due(schedule: Schedule, last_run_started_at: datetime | None, now: datetime) -> bool:
    """Whether `schedule` has come due at `now`, given when the knowledge base's last refresh started.

    A knowledge base that has never been refreshed is due at once.
    `every_seconds` is due once that many seconds have passed since the last start.
    `daily_at` is an `HH:MM` clock time in UTC. It is due once today's clock time has passed and the last start came before it.
    Only the most recent clock time counts, so any length of downtime comes due once.

    Args:
        schedule: The knowledge base's refresh cadence.
        last_run_started_at: When its last refresh started, or `None` when it never ran.
        now: The time to judge at. You must pass it in UTC, because `daily_at` is read in the zone `now` carries.

    Returns:
        True when a refresh should be queued now.
    """
    if last_run_started_at is None:
        return True
    if schedule.every_seconds is not None:
        return (now - last_run_started_at).total_seconds() >= schedule.every_seconds
    if schedule.daily_at is not None:
        return last_run_started_at < _daily_boundary(schedule.daily_at, now)
    return False  # pragma: no cover (Schedule's own validator guarantees exactly one field is set)
