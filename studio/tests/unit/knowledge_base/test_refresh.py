"""The three refresh facts, and the history projected from them.

A refresh announces `RefreshStarted` and then one ending. `refresh_facts` reads those facts back
out of a `kb.refresh` job row, so only a finished job carries any: a queued, running or retrying
job is not a run yet. `refresh_runs` pairs each ending with its start by job id, and
`refresh_history` is the two together, the read every refresh listing shares.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING

import pytest

from core.cqrs.contracts import Job
from pydantic import ValidationError

from studio.knowledge_base.refresh.contracts import (
    RefreshAbandoned,
    RefreshCompleted,
    RefreshCounts,
    RefreshResult,
    RefreshStarted,
    refresh_history,
)
from studio.knowledge_base.refresh.domain import is_forwarded, refresh_facts, refresh_runs

if TYPE_CHECKING:
    from core.cqrs.domain import JobState

CREATED = datetime(2026, 8, 26, 6, 0, tzinfo=UTC)
STARTED = CREATED + timedelta(seconds=1)
FINISHED = CREATED + timedelta(seconds=9)


def _job(
    state: JobState,
    *,
    job_id: int = 1,
    result: str | None = None,
    last_error: str | None = None,
    trigger: str = "manual",
    started: bool = True,
    finished: bool = True,
) -> Job:
    """One `kb.refresh` job row in `state`, with the payload the ticker or the route wrote."""
    return Job(
        id=job_id,
        kind="kb.refresh",
        key="demo",
        payload={"knowledge_base": "demo", "trigger": trigger},
        state=state,
        attempts=1,
        max_attempts=3,
        available_at=CREATED,
        locked_at=None,
        locked_by=None,
        lease_seconds=1800,
        last_error=last_error,
        result=result,
        created_at=CREATED,
        started_at=STARTED if started else None,
        finished_at=FINISHED if finished else None,
    )


def _document(detail: str) -> str:
    """The result a clean pass stores: its detail and no counts."""
    return RefreshResult(detail=detail).model_dump_json()


class TestRefreshCounts:
    def test_two_counts_add_label_by_label(self):
        first = RefreshCounts(sources_created=6, requested_by_entry={"x:a": 6}, created_by_connector={"queue": 6})
        second = RefreshCounts(sources_created=2, requested_by_entry={"x:a": 1, "x:b": 1}, created_by_connector={"queue": 2})

        assert first.plus(second) == RefreshCounts(
            sources_created=8, requested_by_entry={"x:a": 7, "x:b": 1}, created_by_connector={"queue": 8}
        )


class TestRefreshFacts:
    def test_a_done_job_carries_a_start_and_a_completion(self):
        started, completed = refresh_facts(_job("done", result=_document("indexed 3 pages, 3 embedded")))

        assert isinstance(started, RefreshStarted)
        assert isinstance(completed, RefreshCompleted)
        assert (started.knowledge_base, started.job_id, started.trigger) == ("demo", 1, "manual")
        assert (completed.detail, completed.occurred_at) == ("indexed 3 pages, 3 embedded", FINISHED)

    def test_a_done_job_whose_result_is_the_pass_document_carries_its_detail_and_counts(self):
        counts = RefreshCounts(sources_created=6, requested_by_entry={"x:someone": 6})

        _, completed = refresh_facts(_job("done", result=RefreshResult(detail="indexed 6 pages", counts=counts).model_dump_json()))

        assert isinstance(completed, RefreshCompleted)
        assert (completed.detail, completed.counts) == ("indexed 6 pages", counts)

    def test_a_done_job_whose_result_is_not_the_pass_document_is_refused(self):
        with pytest.raises(ValidationError):
            refresh_facts(_job("done", result="indexed 3 pages, 3 embedded"))

    def test_an_abandoned_job_carries_the_last_error_as_the_reason(self):
        started, abandoned = refresh_facts(_job("abandoned", last_error="RuntimeError: index validation failed: 2 error(s)"))

        assert isinstance(started, RefreshStarted)
        assert isinstance(abandoned, RefreshAbandoned)
        assert abandoned.reason == "RuntimeError: index validation failed: 2 error(s)"

    def test_the_payloads_trigger_rides_onto_both_facts(self):
        started, ending = refresh_facts(_job("done", result=_document("ok"), trigger="schedule"))

        assert (started.trigger, ending.trigger) == ("schedule", "schedule")

    def test_a_payload_with_no_trigger_the_shape_recognises_reads_as_manual(self):
        job = _job("done", result=_document("ok")).model_copy(update={"payload": {"knowledge_base": "demo", "trigger": "cron"}})

        started, _ending = refresh_facts(job)

        assert started.trigger == "manual"

    def test_a_job_that_never_started_falls_back_to_when_it_was_created(self):
        started, ending = refresh_facts(_job("done", result=_document("ok"), started=False, finished=False))

        assert (started.occurred_at, ending.occurred_at) == (CREATED, CREATED)

    def test_an_unfinished_job_carries_no_facts_yet(self):
        assert refresh_facts(_job("queued", started=False, finished=False)) == ()
        assert refresh_facts(_job("running", finished=False)) == ()
        assert refresh_facts(_job("failed", last_error="boom", finished=False)) == ()


class TestRefreshRuns:
    def test_a_start_and_its_completion_are_one_finished_run(self):
        facts = [
            RefreshStarted(knowledge_base="demo", job_id=7, occurred_at=STARTED),
            RefreshCompleted(knowledge_base="demo", job_id=7, detail="indexed 3 pages", occurred_at=FINISHED),
        ]

        (run,) = refresh_runs(facts)

        assert (run.outcome, run.detail, run.trigger) == ("ok", "indexed 3 pages", "manual")
        assert (run.started_at, run.finished_at) == (STARTED, FINISHED)

    def test_a_finished_run_carries_its_endings_counts(self):
        counts = RefreshCounts(sources_created=2)
        facts = [
            RefreshStarted(knowledge_base="demo", job_id=7, occurred_at=STARTED),
            RefreshCompleted(knowledge_base="demo", job_id=7, detail="indexed 3 pages", counts=counts, occurred_at=FINISHED),
        ]

        (run,) = refresh_runs(facts)

        assert run.counts == counts

    def test_an_abandonment_is_a_failed_run_carrying_its_reason(self):
        facts = [
            RefreshStarted(knowledge_base="demo", job_id=7, trigger="schedule", occurred_at=STARTED),
            RefreshAbandoned(knowledge_base="demo", job_id=7, trigger="schedule", reason="boom", occurred_at=FINISHED),
        ]

        (run,) = refresh_runs(facts)

        assert (run.outcome, run.detail, run.trigger) == ("failed", "boom", "schedule")

    def test_an_ending_is_paired_by_job_id_whatever_order_the_facts_arrive_in(self):
        facts = [
            RefreshCompleted(knowledge_base="demo", job_id=2, detail="second", occurred_at=FINISHED),
            RefreshCompleted(knowledge_base="demo", job_id=1, detail="first", occurred_at=FINISHED),
            RefreshStarted(knowledge_base="demo", job_id=1, occurred_at=STARTED),
            RefreshStarted(knowledge_base="demo", job_id=2, occurred_at=STARTED),
        ]

        assert [run.detail for run in refresh_runs(facts)] == ["second", "first"]

    def test_an_ending_with_no_start_is_left_out(self):
        facts = [RefreshCompleted(knowledge_base="demo", job_id=9, detail="orphan", occurred_at=FINISHED)]

        assert refresh_runs(facts) == ()

    def test_a_start_with_no_ending_is_not_a_finished_run(self):
        assert refresh_runs([RefreshStarted(knowledge_base="demo", job_id=9, occurred_at=STARTED)]) == ()


class TestRefreshHistory:
    def test_keeps_the_job_order_and_drops_the_unfinished(self):
        jobs = [_job("running", job_id=3, finished=False), _job("done", job_id=2, result=_document("second")), _job("done", job_id=1, result=_document("first"))]

        assert [run.detail for run in refresh_history(jobs)] == ["second", "first"]

    def test_caps_at_the_limit(self):
        jobs = [_job("done", job_id=n, result=_document(str(n))) for n in range(5)]

        assert [run.detail for run in refresh_history(jobs, limit=2)] == ["0", "1"]

    def test_no_jobs_is_an_empty_tuple(self):
        assert refresh_history([]) == ()


class TestForwarding:
    def test_every_completed_refresh_run_is_forwarded(self):
        assert is_forwarded(RefreshCompleted(knowledge_base="demo", job_id=1)) is True

    def test_an_abandoned_refresh_run_is_forwarded_only_when_it_created_sources(self):
        assert is_forwarded(RefreshAbandoned(knowledge_base="demo", job_id=1)) is False
        assert is_forwarded(RefreshAbandoned(knowledge_base="demo", job_id=1, counts=RefreshCounts(sources_created=3))) is True
