"""The knowledge base worker's assembly (`studio.knowledge_base.cli.build_knowledge_base_worker`).

The worker is the one place the ingest kind, the schedule tick, and outbox delivery come together.
Built over in-memory adapters here, one pass of it runs a queued ingest job through the real job
handler, enqueues what the ticker says is due, and delivers what a handler put in the outbox.
"""

from __future__ import annotations

import json

from typing import TYPE_CHECKING

from core.cqrs.contracts import Event, ImmediateWake, MemoryJobs, MemoryOutbox, MessageBus, TopicPublisher
from engram.testing.fakes.bus_spy import BusSpy

from studio.knowledge_base.cli import build_knowledge_base_worker
from studio.knowledge_base.refresh.contracts import REFRESH_KIND, REFRESH_POLICY, REFRESH_TOPIC, RefreshJob

if TYPE_CHECKING:
    from pathlib import Path

    from core.cqrs.contracts import Job
    from core.cqrs.domain import JobState
    from pydantic import JsonValue


class _Happened(Event):
    knowledge_base: str = "demo"


class _RecordingObserver:
    """A `JobObserver` recording the state of every job transition it is told about."""

    def __init__(self) -> None:
        self.states: list[tuple[int, JobState]] = []

    def transitioned(self, job: Job) -> None:
        self.states.append((job.id, job.state))


class _RecordingSink:
    """A `TopicSink` recording every topic it is told about, in order."""

    def __init__(self) -> None:
        self.topics: list[str] = []

    def publish(self, topic: str) -> None:
        self.topics.append(topic)


def _write_registry(settings_path: Path, knowledge_bases: dict[str, dict[str, JsonValue]]) -> None:
    """A settings JSON carrying only the `knowledge_bases` map the ticker and the job read."""
    settings_path.write_text(json.dumps({"knowledge_bases": knowledge_bases}), encoding="utf-8")


def _knowledge_base_dir(path: Path) -> Path:
    """A minimal knowledge base tree satisfying `KnowledgeBaseEntry.path` validation."""
    (path / "_meta").mkdir(parents=True, exist_ok=True)
    (path / "knowledge-base-schema.json").write_text("{}", encoding="utf-8")
    return path


def _worker(settings_path: Path, jobs: MemoryJobs, bus: MessageBus, observer: _RecordingObserver | None = None):
    """The knowledge base worker over in-memory adapters, named for the test."""
    return build_knowledge_base_worker(
        jobs=jobs, bus=bus, wake=ImmediateWake(), settings_path=settings_path, name="test-worker", observer=observer
    )


class TestBuildKnowledgeBaseWorker:
    async def test_a_due_knowledge_base_is_enqueued_with_a_schedule_trigger(self, tmp_path: Path):
        knowledge_base = _knowledge_base_dir(tmp_path / "demo")
        settings_path = tmp_path / "application_settings.json"
        _write_registry(settings_path, {"demo": {"path": str(knowledge_base), "description": "", "schedule": {"every_seconds": 300}}})
        jobs = MemoryJobs()

        await _worker(settings_path, jobs, MessageBus({}, {}, outbox=MemoryOutbox())).tick()

        queued = list(jobs.jobs.values())
        assert [(job.kind, job.key, job.payload) for job in queued] == [(REFRESH_KIND, "demo", {"knowledge_base": "demo", "trigger": "schedule"})]
        assert queued[0].max_attempts == REFRESH_POLICY.max_attempts

    async def test_a_knowledge_base_with_no_schedule_is_never_enqueued(self, tmp_path: Path):
        knowledge_base = _knowledge_base_dir(tmp_path / "demo")
        settings_path = tmp_path / "application_settings.json"
        _write_registry(settings_path, {"demo": {"path": str(knowledge_base), "description": ""}})
        jobs = MemoryJobs()

        await _worker(settings_path, jobs, MessageBus({}, {}, outbox=MemoryOutbox())).tick()

        assert jobs.jobs == {}

    async def test_a_queued_ingest_job_is_claimed_and_abandoned_when_its_knowledge_base_is_gone(self, tmp_path: Path):
        settings_path = tmp_path / "application_settings.json"
        _write_registry(settings_path, {})
        jobs = MemoryJobs()
        answer = await jobs.enqueue(REFRESH_KIND, "ghost", RefreshJob(knowledge_base="ghost"), policy=REFRESH_POLICY)

        for _ in range(REFRESH_POLICY.max_attempts):
            await _worker(settings_path, jobs, MessageBus({}, {}, outbox=MemoryOutbox())).tick()
            failed = await jobs.read(answer.job_id)
            assert failed is not None
            jobs.jobs[answer.job_id] = failed.model_copy(update={"available_at": failed.created_at})

        abandoned = await jobs.read(answer.job_id)
        assert abandoned is not None
        assert abandoned.state == "abandoned"
        assert "ghost" in (abandoned.last_error or "")

    async def test_the_outbox_is_delivered_to_the_buss_subscriptions(self, tmp_path: Path):
        settings_path = tmp_path / "application_settings.json"
        _write_registry(settings_path, {})
        spy = BusSpy()
        outbox = MemoryOutbox()
        bus = MessageBus({}, {_Happened: [spy]}, outbox=outbox)
        event = _Happened()
        await outbox.append([event])

        await _worker(settings_path, MemoryJobs(), bus).tick()

        assert spy.received == [event]
        assert outbox.closed == {event.event_id}

    async def test_an_observer_is_told_about_every_transition_of_a_job_the_worker_runs(self, tmp_path: Path):
        settings_path = tmp_path / "application_settings.json"
        _write_registry(settings_path, {})
        jobs = MemoryJobs()
        answer = await jobs.enqueue(REFRESH_KIND, "ghost", RefreshJob(knowledge_base="ghost"), policy=REFRESH_POLICY)
        observer = _RecordingObserver()

        await _worker(settings_path, jobs, MessageBus({}, {}, outbox=MemoryOutbox()), observer).tick()

        assert [state for job_id, state in observer.states if job_id == answer.job_id] == ["running", "failed"]

    async def test_a_topic_publisher_announces_jobs_and_the_knowledge_base_a_transition_touched(self, tmp_path: Path):
        settings_path = tmp_path / "application_settings.json"
        _write_registry(settings_path, {})
        jobs = MemoryJobs()
        await jobs.enqueue(REFRESH_KIND, "ghost", RefreshJob(knowledge_base="ghost"), policy=REFRESH_POLICY)
        sink = _RecordingSink()
        observer = TopicPublisher(sink, key_topics={REFRESH_KIND: REFRESH_TOPIC})
        worker = build_knowledge_base_worker(
            jobs=jobs,
            bus=MessageBus({}, {}, outbox=MemoryOutbox()),
            wake=ImmediateWake(),
            settings_path=settings_path,
            name="test-worker",
            observer=observer,
        )

        await worker.tick()

        assert sink.topics == ["jobs", "knowledge-base.ghost", "jobs", "knowledge-base.ghost"]
