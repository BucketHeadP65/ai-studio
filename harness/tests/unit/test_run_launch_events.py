"""Every entry that launches a run records the launch, and every run that ends records its ending.

The session log gets `RunLaunched`. The run's own event file gets `RunLaunched` and, when the run
ends, `RunFinished`, whichever entry started the run and whichever engine runs it. A pause is not
an ending, so it records no finish.
"""

from __future__ import annotations

import asyncio

from typing import TYPE_CHECKING

from core.ai.workflows.contracts import RunFinished, RunLaunched, StepRegistry
from pydantic_ai.models.test import TestModel
from pydantic_ai.tools import RunContext
from pydantic_ai.usage import RunUsage

from harness.domain import events as session_events
from harness.features.running import service
from harness.features.running.infrastructure import durable, run_events
from harness.features.running.records import RunCollaborators
from harness.features.running.tools import run_workflow
from harness.session import SessionPool
from harness.settings import RunSettings
from tests.conftest import CallLog, SalesData, register_quarterly_steps
from tests.support.fakes.broadcaster import FakeBroadcaster
from tests.support.fakes.session_log import FakeSessionLog
from tests.support.fakes.sessions import APool
from tests.support.runtime import a_runtime
from tests.support.state import a_state, records_under
from tests.unit.test_schedule_ticker import _one_node_spec

if TYPE_CHECKING:
    from pathlib import Path

    import pytest

    from harness.session import Runtime

WORKFLOW_ID = "wf-sales"
"""The workflow a durable launch here is filed under."""


def _session(registry: StepRegistry, log: FakeSessionLog, tmp_path: Path) -> Runtime:
    """A session whose session log keeps what it was told, over `registry`."""
    return a_runtime(registry=registry, session_log=log, state=a_state(tmp_path))


def _ctx(session: Runtime) -> RunContext[Runtime]:
    """One turn's context over `session`."""
    return RunContext(deps=session, model=TestModel(), usage=RunUsage())


def _collaborators(session: Runtime, tmp_path: Path) -> RunCollaborators:
    """What a chat launch reaches beyond its arguments, all of it under `tmp_path`."""
    return RunCollaborators(records=records_under(tmp_path), topics=FakeBroadcaster(), run_settings=RunSettings(), sessions=APool(SessionPool(session)))


def _launches(log: FakeSessionLog) -> list[session_events.RunLaunched]:
    """Every launch the session log was told about."""
    return [event for event in log.recorded if isinstance(event, session_events.RunLaunched)]


async def test_a_chat_run_records_its_launch_and_its_finish(registry: StepRegistry, tmp_path: Path) -> None:
    log = FakeSessionLog()
    session = _session(registry, log, tmp_path)
    session.conversation.last_workflow = _one_node_spec(WORKFLOW_ID)

    await run_workflow(_ctx(session), "draft", {"quarter": "Q2"}, run=_collaborators(session, tmp_path))
    run_id = session.active.run_id or ""
    await _settled(session)

    assert _launches(log) == [session_events.RunLaunched(workflow_id="draft", run_id=run_id)]
    assert [entry.event for entry in run_events.events_for(session.state, run_id)] == [
        RunLaunched(run_id=run_id, workflow_id="draft"),
        RunFinished(run_id=run_id, workflow_id="draft", status="completed"),
    ]


async def test_a_paused_chat_run_records_its_launch_and_no_finish(call_log: CallLog, tmp_path: Path) -> None:
    registry = StepRegistry()
    register_quarterly_steps(registry, call_log, exclude=frozenset({"fetch_sales"}))
    hold = asyncio.Event()

    async def fetch_sales(quarter: str) -> SalesData:
        await hold.wait()
        return SalesData(quarter=quarter, total=1)

    registry.register(fetch_sales)
    log = FakeSessionLog()
    session = _session(registry, log, tmp_path)
    session.conversation.last_workflow = _one_node_spec(WORKFLOW_ID)

    await run_workflow(_ctx(session), "draft", {"quarter": "Q2"}, run=_collaborators(session, tmp_path))
    run_id = session.active.run_id or ""
    for _ in range(500):
        if session.active.progress.running:
            break
        await asyncio.sleep(0.01)
    assert await service.pause(session)
    await _settled(session)

    assert _launches(log) == [session_events.RunLaunched(workflow_id="draft", run_id=run_id)]
    assert [entry.event for entry in run_events.events_for(session.state, run_id)] == [RunLaunched(run_id=run_id, workflow_id="draft")]


async def _settled(session: Runtime) -> None:
    """Await the chat run's settlement and the outcome turn it hands back, so nothing runs past the test."""
    settlement = session.active.settlement
    assert settlement is not None
    await settlement
    delivery = session.active.delivery
    assert delivery is not None
    await delivery


class _FinishedHandle:
    """A durable run's handle whose run has already finished and answers no query."""

    first_execution_run_id = "run-abc"
    result_run_id = "run-abc"

    async def result(self) -> str:
        return "done"

    async def query(self, *_args: object) -> object:
        msg = "this workflow answers no query"
        raise RuntimeError(msg)


class _FinishedClient:
    """A Temporal client whose every start hands back a finished run."""

    async def start_workflow(self, *_args: object, **_kwargs: object) -> _FinishedHandle:
        return _FinishedHandle()


async def test_a_durable_run_records_its_launch_and_its_finish(registry: StepRegistry, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    async def _finished_client(_run: RunSettings) -> _FinishedClient:
        return _FinishedClient()

    monkeypatch.setattr(durable, "_client", _finished_client)
    log = FakeSessionLog()
    session = _session(registry, log, tmp_path)

    await durable.launch(
        session,
        records=records_under(tmp_path),
        topics=FakeBroadcaster(),
        run=RunSettings(run_engine="temporal"),
        spec=_one_node_spec(WORKFLOW_ID),
        workflow_id=WORKFLOW_ID,
        params={"quarter": "Q2"},
    )
    task = session.active.task
    assert task is not None
    await task

    assert _launches(log) == [session_events.RunLaunched(workflow_id=WORKFLOW_ID, run_id="run-abc")]
    assert [entry.event for entry in run_events.events_for(session.state, "run-abc")] == [
        RunLaunched(run_id="run-abc", workflow_id=WORKFLOW_ID),
        RunFinished(run_id="run-abc", workflow_id=WORKFLOW_ID, status="completed"),
    ]
