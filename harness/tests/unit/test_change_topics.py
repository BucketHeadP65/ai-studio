"""What this process announces, and to whom.

Every test builds a real `Broadcaster` over fake connections, so the coalescing under test is the
production one and nothing is patched. The installed broadcaster is put back to `None` by the
fixture, so no test leaks its own into the next.
"""

from __future__ import annotations

import asyncio
import json

from contextlib import suppress
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import TYPE_CHECKING

import pytest

from core.cqrs.contracts import Broadcaster
from fastapi import Request
from fastapi.responses import JSONResponse

from harness.adapters import topics
from harness.adapters.session_log import SessionLog
from harness.domain.events import Booted, TurnSettled, TurnStarted
from harness.features.running.domain.progress import LiveProgress, RunTopics, Watchers
from harness.features.running.records import RunCollaborators
from harness.features.scheduling.service import Scheduler
from harness.main import change_events
from harness.session import PoolComposition, SessionPool
from harness.settings import RunSettings
from harness.tracing import POLLED
from tests.support.container import an_app
from tests.support.fakes.broadcaster import FakeBroadcaster
from tests.support.fakes.topics import FakeListener, FakeNotifier
from tests.support.state import records_under
from tests.unit.test_schedule_ticker import FakeSchedules, FakeWorkflowStore, _runtime, _schedule

if TYPE_CHECKING:
    from collections.abc import Iterator
    from pathlib import Path

    from core.ai.workflows.contracts import StepRegistry
    from fastapi import FastAPI
    from starlette.types import Message, Scope


@pytest.fixture
def application(tmp_path: Path) -> FastAPI:
    """This test's own application, with its state home under the temporary directory."""
    return an_app(state=tmp_path)


@pytest.fixture
def broadcaster(application: FastAPI) -> Iterator[Broadcaster]:
    """A broadcaster over fake connections: installed for the test, and what its application resolves."""
    built = Broadcaster("postgresql://u:p@h/db", notifier=FakeNotifier(), listener=FakeListener())
    with application.state.wireup_container.override({Broadcaster: built}):
        yield built


class RefusingResolver:
    """A resolver whose container cannot open the change channel this deployment names."""

    async def get[T](self, klass: type[T]) -> T:
        """Refuse the way an unreachable database refuses."""
        del klass
        msg = "[Errno 61] Connection refused"
        raise OSError(msg)


@dataclass(frozen=True)
class State:
    """The one attribute `resolver_of` reads off an application."""

    resolver: RefusingResolver


@dataclass(frozen=True)
class UnreachableChannel:
    """An application whose resolver cannot answer -- what `Request.app` hands back."""

    state: State = field(default_factory=lambda: State(RefusingResolver()))


class TestTheChangeChannel:
    def test_broadcast_topics_announces_on_the_channel_it_was_given(self, broadcaster: Broadcaster) -> None:
        topics.BroadcastTopics(broadcaster).publish("sessions")
        assert broadcaster.pending == ["sessions"]

    def test_no_topics_announces_nowhere(self, broadcaster: Broadcaster) -> None:
        topics.NoTopics().publish("sessions")
        assert broadcaster.pending == []


class TestRunTopics:
    def test_a_step_starting_announces_the_run(self) -> None:
        seen: list[str] = []
        observer = RunTopics(workflow_id="wf.report.v1", publish=seen.append)
        observer.started("fetch")
        assert seen == ["run.wf.report.v1"]

    def test_a_step_finishing_announces_the_run_whether_it_failed_or_not(self) -> None:
        seen: list[str] = []
        observer = RunTopics(workflow_id="wf.report.v1", publish=seen.append)
        observer.finished("fetch")
        observer.finished("draft", failed=True)
        assert seen == ["run.wf.report.v1", "run.wf.report.v1"]

    def test_watchers_carries_it_alongside_the_panel(self) -> None:
        seen: list[str] = []
        progress = LiveProgress()
        Watchers(progress, RunTopics(workflow_id="wf.report.v1", publish=seen.append)).started("fetch")
        assert progress.running == {"fetch"}
        assert seen == ["run.wf.report.v1"]


class TestJournalTopics:
    def test_every_record_announces_the_session_list(self, tmp_path: Path) -> None:
        channel = FakeBroadcaster()
        session_log = SessionLog(tmp_path / "session-log.jsonl", topics=channel)
        session_log.record(Booted(resumed_run_id=None))
        assert channel.published == ["sessions"]

    def test_an_event_naming_a_session_announces_that_session_too(self, tmp_path: Path) -> None:
        channel = FakeBroadcaster()
        session_log = SessionLog(tmp_path / "session-log.jsonl", topics=channel)
        session_log.record(TurnStarted(entry="spa", chars=5, session_id="s-1", prompt_excerpt=None))
        assert channel.published == ["sessions", "session.s-1"]

    def test_an_event_whose_session_is_unknown_announces_the_list_only(self, tmp_path: Path) -> None:
        channel = FakeBroadcaster()
        session_log = SessionLog(tmp_path / "session-log.jsonl", topics=channel)
        session_log.record(TurnSettled(run_id=None, started_seq=1))
        assert channel.published == ["sessions"]

    def test_a_log_over_the_installed_broadcaster_announces_through_it(self, tmp_path: Path, broadcaster: Broadcaster) -> None:
        SessionLog(tmp_path / "session-log.jsonl", topics=broadcaster).record(Booted(resumed_run_id=None))
        assert "sessions" in broadcaster.pending


class TestWorkspaceTopics:
    async def test_a_project_session_announces_through_the_installed_broadcaster(
        self, tmp_path: Path, broadcaster: Broadcaster, registry: StepRegistry
    ) -> None:
        project_root = tmp_path / "project"
        project_root.mkdir()
        pool = SessionPool(_runtime(registry, FakeWorkflowStore()), composition=PoolComposition(topics=broadcaster))

        await pool.create(project_root)

        assert "sessions" in broadcaster.pending


class TestScheduleTopics:
    async def test_a_fire_announces_the_schedule_table(self, broadcaster: Broadcaster, registry: StepRegistry, tmp_path: Path) -> None:
        records = records_under(tmp_path)
        pool = SessionPool(_runtime(registry, FakeWorkflowStore()))
        schedules = FakeSchedules()
        schedule = _schedule()
        await schedules.insert(schedule)

        await Scheduler(schedules, schedules, broadcaster).tick(
            pool,
            run=RunCollaborators(records=records, topics=broadcaster, run_settings=RunSettings()),
            now=datetime(2026, 8, 24, 12, 0, tzinfo=UTC),
        )

        assert "schedules" in broadcaster.pending

    async def test_a_fire_that_raises_still_announces_the_schedule_table(self, broadcaster: Broadcaster, registry: StepRegistry, tmp_path: Path) -> None:
        records = records_under(tmp_path)
        pool = SessionPool(_runtime(registry, FakeWorkflowStore()))
        schedules = FakeSchedules()
        poisoned = _schedule(workflow_id="wf-missing", cadence="not a cadence at all")
        await schedules.insert(poisoned)

        await Scheduler(schedules, schedules, broadcaster).tick(
            pool,
            run=RunCollaborators(records=records, topics=broadcaster, run_settings=RunSettings()),
            now=datetime(2026, 8, 24, 12, 0, tzinfo=UTC),
        )

        assert "schedules" in broadcaster.pending


async def _opened(application: FastAPI, path: str) -> tuple[Message, asyncio.Queue[Message], asyncio.Task[None]]:
    """Call the application over raw ASGI, and hand back the start message, the body queue, and its task.

    `httpx`'s ASGI transport collects a whole response before handing any of it back, which never
    happens for a stream this route only ever closes on a client disconnect. Driving the app
    directly is what lets a test read one frame and then walk away.
    """
    root, _, query = path.partition("?")
    scope: Scope = {
        "type": "http",
        "asgi": {"version": "3.0", "spec_version": "2.4"},
        "http_version": "1.1",
        "method": "GET",
        "headers": [(b"host", b"localhost")],
        "scheme": "http",
        "path": root,
        "raw_path": root.encode(),
        "query_string": query.encode(),
        "server": ("test", 80),
        "client": ("test", 123),
        "root_path": "",
    }
    body: asyncio.Queue[Message] = asyncio.Queue()

    async def receive() -> Message:
        return {"type": "http.disconnect"}

    async def send(message: Message) -> None:
        await body.put(message)

    task = asyncio.create_task(application(scope, receive, send))
    start = await asyncio.wait_for(body.get(), timeout=2)
    return start, body, task


async def _closed(task: asyncio.Task[None]) -> None:
    """Stop driving the app, since a change stream never ends on its own."""
    task.cancel()
    with suppress(asyncio.CancelledError):
        await task


class TestEventsEndpoint:
    async def test_a_delivered_topic_arrives_as_a_change_frame(self, application: FastAPI, broadcaster: Broadcaster) -> None:
        start, body, task = await _opened(application, "/api/events")
        try:
            assert start["status"] == 200
            assert dict(start["headers"])[b"content-type"].startswith(b"text/event-stream")
            await asyncio.sleep(0.05)
            broadcaster.deliver("sessions")
            frame = (await asyncio.wait_for(body.get(), timeout=2))["body"].decode()
            assert frame.startswith("event: change\n")
            assert "sessions" in frame
        finally:
            await _closed(task)

    async def test_a_filter_drops_a_topic_nobody_asked_for(self, application: FastAPI, broadcaster: Broadcaster) -> None:
        _, body, task = await _opened(application, "/api/events?topics=jobs")
        try:
            await asyncio.sleep(0.05)
            broadcaster.deliver("sessions")
            broadcaster.deliver("jobs")
            frame = (await asyncio.wait_for(body.get(), timeout=2))["body"].decode()
            assert frame.startswith("event: change\n")
            assert "jobs" in frame
        finally:
            await _closed(task)

    async def test_a_channel_that_cannot_open_answers_503_rather_than_a_stream(self) -> None:
        """A process whose change channel cannot reach its database says so, and the browser stops asking."""
        response = await change_events(Request({"type": "http", "app": UnreachableChannel(), "query_string": b""}))

        assert isinstance(response, JSONResponse)
        assert response.status_code == 503
        assert "no broadcaster" in json.loads(bytes(response.body))["message"]


def test_the_events_endpoint_is_never_traced() -> None:
    assert "/api/events" in POLLED
