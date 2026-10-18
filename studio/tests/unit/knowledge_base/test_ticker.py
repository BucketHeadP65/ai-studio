"""Unit specs for the refresh ticker (`studio.knowledge_base.refresh`).

Pins the due-computation (`is_due`: never-run, `every_seconds`, `daily_at`, and downtime
collapsing to a single catch-up) and `Ticker.tick`'s contract: it fires only due,
schedule-declaring knowledge bases through the caller's `run_one`, reads the registry fresh every call,
takes each knowledge base's last start off the `kb.refresh` job history, and lets one knowledge base's failure
never stop the rest of the tick. Time is always injected — nothing here sleeps for real.
"""

from __future__ import annotations

import json

from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING

from core.cqrs.contracts import Job, JobPolicy, MemoryJobs

from studio.knowledge_base.refresh.contracts import REFRESH_KIND, RefreshJob, Ticker
from studio.knowledge_base.refresh.domain import is_due
from studio.knowledge_base.settings import Schedule

if TYPE_CHECKING:
    from pathlib import Path

    from studio.knowledge_base.settings import KnowledgeBaseEntry

_NOW = datetime(2026, 8, 21, 12, 0, tzinfo=UTC)


class _HistoryFailsFor(MemoryJobs):
    """A job store whose refresh history cannot be read for one knowledge base."""

    def __init__(self, broken: str) -> None:
        super().__init__()
        self.broken = broken

    async def recent_jobs(self, kind: str, key: str, *, limit: int = 50) -> list[Job]:
        if key == self.broken:
            msg = "the jobs table is unreadable"
            raise RuntimeError(msg)
        return await super().recent_jobs(kind, key, limit=limit)


class TestIsDue:
    def test_a_knowledge_base_that_never_ran_is_due_immediately(self):
        schedule = Schedule(every_seconds=300)
        assert is_due(schedule, None, _NOW) is True

    def test_every_seconds_below_the_floor_is_not_due(self):
        schedule = Schedule(every_seconds=600)
        last = _NOW - timedelta(seconds=300)
        assert is_due(schedule, last, _NOW) is False

    def test_every_seconds_at_or_over_the_floor_is_due(self):
        schedule = Schedule(every_seconds=600)
        assert is_due(schedule, _NOW - timedelta(seconds=600), _NOW) is True
        assert is_due(schedule, _NOW - timedelta(seconds=900), _NOW) is True

    def test_daily_at_before_todays_boundary_and_run_before_yesterdays_boundary_is_due(self):
        schedule = Schedule(daily_at="06:00")
        now = _NOW.replace(hour=5, minute=59)  # today's 06:00 hasn't happened yet
        last = now - timedelta(days=2)  # ran two days ago, well before yesterday's 06:00
        assert is_due(schedule, last, now) is True

    def test_daily_at_already_run_after_todays_boundary_is_not_due(self):
        schedule = Schedule(daily_at="06:00")
        now = _NOW.replace(hour=10, minute=0)
        last = _NOW.replace(hour=6, minute=30)  # ran today, after the 06:00 boundary
        assert is_due(schedule, last, now) is False

    def test_daily_at_run_before_todays_boundary_and_now_past_it_is_due(self):
        schedule = Schedule(daily_at="06:00")
        now = _NOW.replace(hour=6, minute=1)
        last = _NOW.replace(hour=5, minute=0)  # ran today, before the 06:00 boundary
        assert is_due(schedule, last, now) is True

    def test_daily_at_run_exactly_at_the_boundary_is_not_due(self):
        schedule = Schedule(daily_at="06:00")
        now = _NOW.replace(hour=10, minute=0)
        last = _NOW.replace(hour=6, minute=0)  # ran at 06:00 sharp, that run WAS today's fire
        assert is_due(schedule, last, now) is False

    def test_a_long_downtime_still_fires_only_once(self):
        """A `daily_at` schedule missed for days collapses to the single most recent boundary."""
        schedule = Schedule(daily_at="06:00")
        now = _NOW.replace(hour=10, minute=0)
        last = now - timedelta(days=5)
        assert is_due(schedule, last, now) is True
        # the boundary check itself only ever looks at the SINGLE most recent boundary --
        # a run that already happened past yesterday's boundary is not due again today.
        recovered = now - timedelta(hours=1)
        assert is_due(schedule, recovered, now) is False


def _knowledge_base_dir(path: Path) -> Path:
    """A minimal knowledge base tree satisfying `KnowledgeBaseEntry.path` validation (the legacy `Units/`+`_meta/` shape)."""
    for sub in ("Units", "_meta"):
        (path / sub).mkdir(parents=True, exist_ok=True)
    return path


def _write_registry(settings_path: Path, knowledge_bases: dict[str, dict[str, object]]) -> None:
    """Write a minimal settings JSON carrying only the `knowledge_bases` map `Ticker`/`read_registry` read."""
    settings_path.write_text(json.dumps({"knowledge_bases": knowledge_bases}), encoding="utf-8")


def _knowledge_base_row(path: Path, *, schedule: dict[str, object] | None = None) -> dict[str, object]:
    """One raw registry row, optionally carrying a `schedule` sub-object."""
    row: dict[str, object] = {"path": str(path), "description": ""}
    if schedule is not None:
        row["schedule"] = schedule
    return row


@dataclass
class RecordingRunOne:
    """Fakes `Ticker`'s `run_one`: records every `(name, entry.path)` it was called with."""

    calls: list[tuple[str, Path]] = field(default_factory=list)
    fail_for: frozenset[str] = frozenset()

    async def __call__(self, name: str, entry: KnowledgeBaseEntry) -> None:
        self.calls.append((name, entry.path))
        if name in self.fail_for:
            msg = f"run_one deliberately failed for {name!r}"
            raise RuntimeError(msg)


@dataclass
class _FixedClock:
    """A clock frozen at one instant."""

    now: datetime

    def __call__(self) -> datetime:
        return self.now


async def _ran_at(jobs: MemoryJobs, name: str, started_at: datetime) -> None:
    """Put one finished `kb.refresh` job on `name`'s ledger, started at `started_at`."""
    at = MemoryJobs(clock=_FixedClock(started_at))
    answer = await at.enqueue(REFRESH_KIND, name, RefreshJob(knowledge_base=name), policy=JobPolicy())
    claimed = await at.claim([REFRESH_KIND], worker="test")
    assert claimed is not None
    await at.complete(answer.job_id, result="ok")
    job_id = len(jobs.jobs) + 1
    jobs.jobs[job_id] = at.jobs[answer.job_id].model_copy(update={"id": job_id})


class TestTick:
    async def test_fires_only_due_schedule_declaring_knowledge_bases(self, tmp_path: Path):
        due = _knowledge_base_dir(tmp_path / "due")
        not_due = _knowledge_base_dir(tmp_path / "not-due")
        no_schedule = _knowledge_base_dir(tmp_path / "no-schedule")
        jobs = MemoryJobs()
        await _ran_at(jobs, "due", _NOW - timedelta(seconds=600))
        await _ran_at(jobs, "not-due", _NOW - timedelta(seconds=10))
        settings_path = tmp_path / "application_settings.json"
        _write_registry(
            settings_path,
            {
                "due": _knowledge_base_row(due, schedule={"every_seconds": 300}),
                "not-due": _knowledge_base_row(not_due, schedule={"every_seconds": 300}),
                "no-schedule": _knowledge_base_row(no_schedule),
            },
        )
        run_one = RecordingRunOne()
        ticker = Ticker(settings_path=settings_path, jobs=jobs, run_one=run_one, clock=lambda: _NOW)

        await ticker.tick()

        assert run_one.calls == [("due", due)]

    async def test_a_never_run_scheduled_knowledge_base_fires_on_the_first_tick(self, tmp_path: Path):
        fresh = _knowledge_base_dir(tmp_path / "fresh")
        settings_path = tmp_path / "application_settings.json"
        _write_registry(settings_path, {"fresh": _knowledge_base_row(fresh, schedule={"every_seconds": 300})})
        run_one = RecordingRunOne()
        ticker = Ticker(settings_path=settings_path, jobs=MemoryJobs(), run_one=run_one, clock=lambda: _NOW)

        await ticker.tick()

        assert run_one.calls == [("fresh", fresh)]

    async def test_a_queued_job_counts_as_this_knowledge_base_having_just_run(self, tmp_path: Path):
        knowledge_base = _knowledge_base_dir(tmp_path / "demo")
        jobs = MemoryJobs(clock=lambda: _NOW - timedelta(seconds=10))
        await jobs.enqueue(REFRESH_KIND, "demo", RefreshJob(knowledge_base="demo"), policy=JobPolicy())
        settings_path = tmp_path / "application_settings.json"
        _write_registry(settings_path, {"demo": _knowledge_base_row(knowledge_base, schedule={"every_seconds": 300})})
        run_one = RecordingRunOne()
        ticker = Ticker(settings_path=settings_path, jobs=jobs, run_one=run_one, clock=lambda: _NOW)

        await ticker.tick()

        assert run_one.calls == []

    async def test_reads_the_registry_fresh_on_every_call(self, tmp_path: Path):
        knowledge_base = _knowledge_base_dir(tmp_path / "demo")
        settings_path = tmp_path / "application_settings.json"
        _write_registry(settings_path, {"demo": _knowledge_base_row(knowledge_base)})  # no schedule yet
        run_one = RecordingRunOne()
        ticker = Ticker(settings_path=settings_path, jobs=MemoryJobs(), run_one=run_one, clock=lambda: _NOW)

        await ticker.tick()
        assert run_one.calls == []

        _write_registry(settings_path, {"demo": _knowledge_base_row(knowledge_base, schedule={"every_seconds": 300})})
        await ticker.tick()
        assert run_one.calls == [("demo", knowledge_base)]

    async def test_one_knowledge_base_failing_never_stops_the_rest_of_the_tick(self, tmp_path: Path):
        failing = _knowledge_base_dir(tmp_path / "failing")
        healthy = _knowledge_base_dir(tmp_path / "healthy")
        settings_path = tmp_path / "application_settings.json"
        _write_registry(
            settings_path,
            {
                "failing": _knowledge_base_row(failing, schedule={"every_seconds": 300}),
                "healthy": _knowledge_base_row(healthy, schedule={"every_seconds": 300}),
            },
        )
        run_one = RecordingRunOne(fail_for=frozenset({"failing"}))
        ticker = Ticker(settings_path=settings_path, jobs=MemoryJobs(), run_one=run_one, clock=lambda: _NOW)

        await ticker.tick()

        assert {name for name, _path in run_one.calls} == {"failing", "healthy"}

    async def test_one_knowledge_base_whose_history_cannot_be_read_never_stops_the_rest_of_the_tick(self, tmp_path: Path):
        broken = _knowledge_base_dir(tmp_path / "broken")
        healthy = _knowledge_base_dir(tmp_path / "healthy")
        settings_path = tmp_path / "application_settings.json"
        _write_registry(
            settings_path,
            {
                "broken": _knowledge_base_row(broken, schedule={"every_seconds": 300}),
                "healthy": _knowledge_base_row(healthy, schedule={"every_seconds": 300}),
            },
        )
        run_one = RecordingRunOne()
        ticker = Ticker(settings_path=settings_path, jobs=_HistoryFailsFor("broken"), run_one=run_one, clock=lambda: _NOW)

        await ticker.tick()

        assert {name for name, _path in run_one.calls} == {"healthy"}

    async def test_a_missing_settings_file_does_not_raise(self, tmp_path: Path):
        run_one = RecordingRunOne()
        ticker = Ticker(settings_path=tmp_path / "does-not-exist.json", jobs=MemoryJobs(), run_one=run_one, clock=lambda: _NOW)

        await ticker.tick()  # must not raise

        assert run_one.calls == []
