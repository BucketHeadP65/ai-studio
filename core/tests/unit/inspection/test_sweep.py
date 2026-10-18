"""The sweep: every run that ended in a watch's window and was never inspected under it gets its command."""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING

from core.ai.inspection.domain.watches import Watch
from core.ai.inspection.lanes import INSPECTOR_AGENT_NAME, JobLanes
from core.ai.inspection.sweep import Sweep
from core.cqrs.contracts import MemoryJobs
from tests.support.fakes.inspection import FakeRecordEvents
from tests.support.fakes.inspection_events import GrowingTerminalEvents
from tests.unit.inspection.test_dispatch import event

if TYPE_CHECKING:
    from core.ai.history.contracts import EventAtPositionRow

NOW = datetime(2026, 9, 8, 12, 0, tzinfo=UTC)
NIGHTLY = Watch(name="nightly", rules=("duplicate_call",), when="sweep", sweep_hours=24)
LIVE = Watch(name="every-run", rules=("duplicate_call",))


def at(hours_ago: int, **fields) -> EventAtPositionRow:
    return replace(event(**fields), at=NOW - timedelta(hours=hours_ago))


async def test_a_window_issues_one_command_per_ended_run_it_selects_and_skips_what_the_ledger_holds() -> None:
    events = FakeRecordEvents(
        [
            at(1, event_id=1, kind="run_completed", run_id="r1"),
            at(2, event_id=2, kind="run_failed", run_id="r2"),
            at(30, event_id=3, kind="run_completed", run_id="r3"),
        ]
    )
    store = MemoryJobs()
    lanes = JobLanes(store)
    await lanes.inspect_run("record:r2", "nightly")
    sweep = Sweep(watches=(NIGHTLY,), events=events, lanes=lanes, clock=lambda: NOW)

    passed = await sweep.window(NIGHTLY, 24)

    assert (passed.considered, passed.enqueued) == (2, 1)
    assert sorted(job.key for job in store.jobs.values()) == ["nightly:record:r1", "nightly:record:r2"]


async def test_once_reconciles_live_watches_as_well_as_sweep_watches() -> None:
    events = FakeRecordEvents([at(1, event_id=1, kind="run_completed", run_id="r1")])
    store = MemoryJobs()
    lanes = JobLanes(store)

    passed = await Sweep(watches=(LIVE, NIGHTLY), events=events, lanes=lanes, clock=lambda: NOW).tick()

    assert (passed.considered, passed.enqueued) == (2, 2)
    assert sorted(job.key for job in store.jobs.values()) == ["every-run:record:r1", "nightly:record:r1"]


async def test_reconciliation_reaches_runs_beyond_the_first_thousand() -> None:
    events = FakeRecordEvents([at(1, event_id=i, kind="run_completed", run_id=f"r{i}") for i in range(1, 1003)])
    jobs = MemoryJobs()
    sweep = Sweep(watches=(LIVE,), events=events, lanes=JobLanes(jobs), clock=lambda: NOW)

    passed = await sweep.tick()

    assert (passed.considered, passed.enqueued) == (1002, 1002)
    assert any(job.key == "every-run:record:r1002" for job in jobs.jobs.values())


async def test_all_watches_stop_at_the_same_upper_position_despite_new_arrivals() -> None:
    events = GrowingTerminalEvents([at(1, event_id=index, kind="run_completed", run_id=f"r{index}") for index in range(1, 4)])
    jobs = MemoryJobs()
    sweep = Sweep(watches=(LIVE, NIGHTLY), events=events, lanes=JobLanes(jobs), clock=lambda: NOW, limit=1)

    first = await sweep.tick()

    assert (first.considered, first.enqueued) == (6, 6)
    assert all(job.payload["terminal_event"] in (1, 2, 3) for job in jobs.jobs.values())
    following = await sweep.tick()
    assert following.enqueued == 10
    assert any(job.key == "nightly:record:r8" for job in jobs.jobs.values())


async def test_an_abandoned_final_scan_is_requeued_after_completed_rules() -> None:
    events = FakeRecordEvents([at(1, event_id=7, kind="run_completed", run_id="r1")])
    jobs = MemoryJobs()
    lanes = JobLanes(jobs)
    rules = await lanes.inspect_run("record:r1", LIVE.name, terminal_event=7)
    scan = await lanes.scan_run("record:r1", LIVE.name, terminal_event=7)
    await jobs.complete(rules.job_id, result="read the final transcript and queued scan")
    await jobs.fail(scan.job_id, error="provider temporarily unavailable", retry_at=None)
    sweep = Sweep(watches=(LIVE,), events=events, lanes=lanes, clock=lambda: NOW)

    assert not await lanes.inspected(LIVE.name, "record:r1", terminal_event=7)
    assert (await sweep.tick()).enqueued == 1
    assert (await sweep.tick()).enqueued == 0
    replacement = await lanes.scan_run("record:r1", LIVE.name, terminal_event=7)
    assert replacement.created
    assert jobs.jobs[replacement.job_id].max_attempts == 2
    final_rules = max(job.id for job in jobs.jobs.values() if job.kind == "inspect_run")
    await jobs.complete(final_rules, result="read the final transcript and queued scan")
    await jobs.complete(replacement.job_id, result="scan undecided: evidence not recorded")
    assert await lanes.inspected(LIVE.name, "record:r1", terminal_event=7)
    assert (await sweep.tick()).enqueued == 0


async def test_a_completed_early_inspection_does_not_suppress_the_terminal_transcript() -> None:
    events = FakeRecordEvents([at(1, event_id=2, kind="run_completed", run_id="r1")])
    jobs = MemoryJobs()
    lanes = JobLanes(jobs)
    earlier = await lanes.inspect_run("record:r1", "every-run")
    await jobs.complete(earlier.job_id, result="read the first step")
    sweep = Sweep(watches=(LIVE,), events=events, lanes=lanes, clock=lambda: NOW)

    passed = await sweep.tick()

    assert passed.enqueued == 1
    final = max(jobs.jobs.values(), key=lambda job: job.id)
    assert final.payload["terminal_event"] == 2
    await jobs.complete(final.id, result="read the final step")
    assert (await sweep.tick()).enqueued == 0


async def test_a_terminal_event_coalesced_behind_running_work_is_reconciled_after_it_finishes() -> None:
    events = FakeRecordEvents([at(1, event_id=2, kind="run_completed", run_id="r1")])
    jobs = MemoryJobs()
    lanes = JobLanes(jobs)
    earlier = await lanes.inspect_run("record:r1", "every-run")
    await jobs.claim(["inspect_run"], worker="early")
    sweep = Sweep(watches=(LIVE,), events=events, lanes=lanes, clock=lambda: NOW)

    assert (await sweep.tick()).enqueued == 0
    await jobs.complete(earlier.job_id, result="read the first step")

    assert (await sweep.tick()).enqueued == 1
    assert (await sweep.tick()).enqueued == 0


async def test_an_abandoned_final_inspection_is_reconciled_again() -> None:
    events = FakeRecordEvents([at(1, event_id=2, kind="run_completed", run_id="r1")])
    jobs = MemoryJobs()
    sweep = Sweep(watches=(LIVE,), events=events, lanes=JobLanes(jobs), clock=lambda: NOW)
    await sweep.tick()
    first = next(iter(jobs.jobs.values()))
    await jobs.fail(first.id, error="record temporarily unreachable", retry_at=None)

    assert (await sweep.tick()).enqueued == 1


async def test_a_window_never_issues_a_command_for_the_inspectors_own_runs() -> None:
    events = FakeRecordEvents(
        [
            at(1, event_id=1, kind="run_completed", run_id="r1"),
            at(1, event_id=2, kind="run_completed", run_id="inspector-1", agent=INSPECTOR_AGENT_NAME),
        ]
    )
    store = MemoryJobs()
    sweep = Sweep(watches=(NIGHTLY,), events=events, lanes=JobLanes(store), clock=lambda: NOW)

    passed = await sweep.window(NIGHTLY, 24)

    assert (passed.considered, passed.enqueued) == (1, 1)
    assert [job.key for job in store.jobs.values()] == ["nightly:record:r1"]
