"""The two lanes as job kinds: one InspectRun in flight per watch and run, one AttributeLead per lead."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from core.ai.inspection.lanes import (
    ATTRIBUTE_LEAD_KIND,
    INSPECT_RUN_KIND,
    SCAN_RUN_KIND,
    AttributeLead,
    JobLanes,
    ScanRun,
    inspect_run_key,
    scan_run_key,
)
from core.cqrs.contracts import MemoryJobs


async def test_the_same_run_under_the_same_watch_is_one_live_command() -> None:
    lanes = JobLanes(MemoryJobs())

    first = await lanes.inspect_run("record:run-1", "every-run")
    again = await lanes.inspect_run("record:run-1", "every-run")
    other = await lanes.inspect_run("record:run-1", "session-x")

    assert first.created
    assert not again.created
    assert again.job_id == first.job_id
    assert other.created
    assert other.job_id != first.job_id


async def test_leads_go_to_the_inspectors_lane_one_command_each() -> None:
    jobs = MemoryJobs()
    lanes = JobLanes(jobs)

    await lanes.enqueue([7, 8, 7])

    claimed = [await jobs.claim([ATTRIBUTE_LEAD_KIND], worker="w") for _ in range(3)]
    payloads = [AttributeLead.model_validate(job.payload) for job in claimed if job is not None]
    assert sorted(payload.lead_id for payload in payloads) == [7, 8]


async def test_a_run_counts_as_inspected_only_after_its_command_succeeds() -> None:
    jobs = MemoryJobs()
    lanes = JobLanes(jobs)

    assert not await lanes.inspected("every-run", "record:run-1")
    answer = await lanes.inspect_run("record:run-1", "every-run")
    assert not await lanes.inspected("every-run", "record:run-1")

    job = await jobs.claim([INSPECT_RUN_KIND], worker="w")
    assert job is not None
    assert job.id == answer.job_id
    assert job.key == inspect_run_key("every-run", "record:run-1")
    await jobs.complete(job.id, result="done")
    assert await lanes.inspected("every-run", "record:run-1")


async def test_duplicate_terminal_deliveries_do_not_repeat_successful_inspections() -> None:
    jobs = MemoryJobs()
    lanes = JobLanes(jobs)
    first = await lanes.inspect_run("record:run-1", "every-run", terminal_event=7)
    await jobs.complete(first.job_id, result="inspected")

    again = await lanes.inspect_run("record:run-1", "every-run", terminal_event=7)

    assert not again.created
    assert again.job_id == first.job_id
    assert not await lanes.inspected("every-run", "record:run-1", terminal_event=8)


async def test_pending_running_and_retryable_final_scans_remain_incomplete() -> None:
    jobs = MemoryJobs()
    lanes = JobLanes(jobs)
    rules = await lanes.inspect_run("record:run-1", "every-run", terminal_event=7)
    scan = await lanes.scan_run("record:run-1", "every-run", terminal_event=7)
    await jobs.complete(rules.job_id, result="scan queued")

    assert not await lanes.inspected("every-run", "record:run-1", terminal_event=7)
    await jobs.claim([SCAN_RUN_KIND], worker="scanner")
    assert not await lanes.inspected("every-run", "record:run-1", terminal_event=7)
    await jobs.fail(scan.job_id, error="rate limited", retry_at=datetime.now(UTC) + timedelta(minutes=1))
    assert not await lanes.inspected("every-run", "record:run-1", terminal_event=7)
    assert not (await lanes.scan_run("record:run-1", "every-run", terminal_event=7)).created


async def test_a_terminal_scan_waits_for_an_earlier_live_scan_then_queues_the_final_transcript_once() -> None:
    jobs = MemoryJobs()
    lanes = JobLanes(jobs)
    earlier = await lanes.scan_run("record:run-1", "every-run")
    await jobs.claim([SCAN_RUN_KIND], worker="model")

    with pytest.raises(RuntimeError, match="earlier transcript"):
        await lanes.scan_run("record:run-1", "every-run", terminal_event=7)
    await jobs.complete(earlier.job_id, result="scanned first step")
    final = await lanes.scan_run("record:run-1", "every-run", terminal_event=7)
    assert final.created
    assert jobs.jobs[final.job_id].payload["terminal_event"] == 7
    await jobs.complete(final.job_id, result="scan undecided: insufficient evidence")
    assert not (await lanes.scan_run("record:run-1", "every-run", terminal_event=7)).created


async def test_a_scan_of_the_same_run_under_the_same_watch_is_one_live_command() -> None:
    jobs = MemoryJobs()
    lanes = JobLanes(jobs)

    first = await lanes.scan_run("record:run-1", "every-run")
    again = await lanes.scan_run("record:run-1", "every-run")

    assert first.created
    assert not again.created
    assert again.job_id == first.job_id
    job = await jobs.claim([SCAN_RUN_KIND], worker="w")
    assert job is not None
    assert job.key == scan_run_key("every-run", "record:run-1") == "scan:every-run:record:run-1"
    assert ScanRun.model_validate(job.payload) == ScanRun(source="record:run-1", watch="every-run")
