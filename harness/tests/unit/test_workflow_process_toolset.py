"""`WorkflowProcess`'s own tool descriptions, exactly as the chat model reads them."""

from __future__ import annotations

import asyncio

from typing import TYPE_CHECKING, Any, cast

from core.ai.workflows.contracts import StepRegistry, WorkflowContract
from core.ai.workflows.infrastructure.folder import FOLDER, folder_toolset
from pydantic_ai.models.test import TestModel
from pydantic_ai.tools import RunContext
from pydantic_ai.usage import RunUsage

from harness.features.authoring.process import WorkflowProcess
from harness.features.running import service
from harness.features.running.domain.progress import GateVerdict
from harness.features.running.spend_report import SpendReporter
from harness.features.scheduling.contracts import Scheduler
from harness.session import NoSessionLog, Runtime, SessionPool
from harness.settings import RunSettings
from tests.support.fakes.broadcaster import FakeBroadcaster
from tests.support.fakes.schedules import FakeSchedules
from tests.support.fakes.sessions import APool
from tests.support.fakes.telemetry import FakeTelemetry
from tests.support.runtime import a_runtime
from tests.support.state import a_state, records_under

if TYPE_CHECKING:
    from pathlib import Path

    from core.ai.workflows.contracts import Workflow


def _process(tmp_path: Path, session: Runtime | None = None) -> WorkflowProcess:
    """The capability under test, its pool holding `session` when a case has a live run in one."""
    port = WorkflowContract[Runtime](
        source=folder_toolset(tmp_path / "workflows"),
        vocabulary=FOLDER,
        expose=(),
        registry=StepRegistry(),
        saved_by="test",
        save_capability_id="saving-a-workflow",
    )
    return WorkflowProcess(
        port=port,
        records=records_under(tmp_path),
        reporter=SpendReporter(FakeTelemetry()),
        scheduler=Scheduler(FakeSchedules(), FakeSchedules(), FakeBroadcaster()),
        sessions=APool(SessionPool(session if session is not None else a_runtime())),
        topics=FakeBroadcaster(),
        run=RunSettings(),
    )


async def test_run_values_tool_description_carries_no_semicolon(tmp_path: Path) -> None:
    toolset = _process(tmp_path).get_toolset()
    ctx: RunContext[Runtime] = RunContext(deps=cast("Any", None), model=TestModel(), usage=RunUsage())
    tools = await toolset.get_tools(ctx)

    description = tools["run_values"].tool_def.description
    assert description is not None
    assert ";" not in description


async def test_check_runs_sends_a_question_about_ended_runs_to_recent_runs(tmp_path: Path) -> None:
    """The two run reads rhyme, so the in-flight one says where the ended runs are."""
    toolset = _process(tmp_path).get_toolset()
    ctx: RunContext[Runtime] = RunContext(deps=cast("Any", None), model=TestModel(), usage=RunUsage())
    tools = await toolset.get_tools(ctx)

    description = tools["check_runs"].tool_def.description

    assert description is not None
    assert "recent_runs" in description


async def test_recent_runs_opens_with_the_runs_in_flight(tmp_path: Path, quarterly_report: Workflow, registry: StepRegistry) -> None:
    """A reviewer deciding a live run's gate must find that run here, not mistake the archive's last one for it."""
    session = Runtime(registry=registry, library=cast("Any", None), agent=cast("Any", object()), session_log=NoSessionLog(), state=a_state(tmp_path))
    service.launch(session, records=records_under(tmp_path), topics=FakeBroadcaster(), spec=quarterly_report, workflow_id="wf.in-flight", params={"quarter": "Q2"})
    for _ in range(500):
        if session.active.decision is not None:
            break
        await asyncio.sleep(0.01)
    decision = session.active.decision
    assert decision is not None, "the run never waiting at its gate"
    toolset = _process(tmp_path, session).get_toolset()
    ctx: RunContext[Runtime] = RunContext(deps=session, model=TestModel(), usage=RunUsage())
    tools = await toolset.get_tools(ctx)

    said = await toolset.call_tool("recent_runs", {"limit": 5}, ctx, tools["recent_runs"])

    decision.set_result(GateVerdict(approve=True))
    task = session.active.task
    assert task is not None
    await task
    assert said.startswith("Runs in flight:\n- wf.in-flight in this session: waiting at gate 'approve'")
    assert said.endswith("\n\nNo archived runs yet.")


async def test_the_mounted_instructions_send_a_proposed_spec_through_check_draft(tmp_path: Path) -> None:
    """The local voice names the tool, and never the reply envelope the chat entry stopped speaking."""
    voice = _process(tmp_path).get_instructions()
    ctx: RunContext[Runtime] = RunContext(deps=a_runtime(), model=TestModel(), usage=RunUsage())
    text = voice(ctx)

    assert "check_draft" in text
    assert "spec_yaml" not in text
    assert "structured slots" not in text
