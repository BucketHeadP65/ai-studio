"""A workflow this app cannot run is refused in one wording, whichever entry asked to run it.

The wording is scheduling's: `this app cannot run it. `, then each problem as `where: detail`,
joined by a period and a space. An entry may put its own opening and closing around it.
"""

from __future__ import annotations

import json

from typing import TYPE_CHECKING, Any, cast

import pytest
import yaml

from core.ai.workflows.contracts import StepRegistry, validate
from pydantic_ai.exceptions import ModelRetry
from pydantic_ai.models.test import TestModel
from pydantic_ai.tools import RunContext
from pydantic_ai.usage import RunUsage

from harness.features.authoring.domain import Draft
from harness.features.running import controller
from harness.features.running.contracts import RunCollaborators, WorkflowNotRunnableError
from harness.features.running.tools import run_workflow
from harness.features.scheduling.contracts import Scheduler, schedule_workflow
from harness.session import NoSessionLog, Runtime, SessionPool, verdict_on
from harness.settings import RunSettings
from tests.support.controllers import a_client_of
from tests.support.fakes.broadcaster import FakeBroadcaster
from tests.support.fakes.schedules import FakeSchedules
from tests.support.running import a_running_controller
from tests.support.runtime import a_runtime
from tests.support.state import a_state, records_under
from tests.unit.test_schedule_ticker import FakeWorkflowStore, _one_node_spec

if TYPE_CHECKING:
    from pathlib import Path

WORKFLOW_ID = "wf-sales"
"""The workflow every entry here is asked to run. Its one step is in no registry these tests hold."""


def _library() -> FakeWorkflowStore:
    """A library holding the one-node workflow."""
    return FakeWorkflowStore(specs={WORKFLOW_ID: _one_node_spec(WORKFLOW_ID)})


def _shared_refusal() -> str:
    """The refusal for the one-node workflow over an empty registry, written out by hand."""
    report = validate(_one_node_spec(WORKFLOW_ID), StepRegistry())
    assert not report.ok
    return "this app cannot run it. " + ". ".join(f"{issue.where}: {issue.detail}" for issue in report.issues)


def _session(tmp_path: Path) -> Runtime:
    """A session whose library holds the workflow and whose registry holds none of its steps."""
    return Runtime(registry=StepRegistry(), library=_library(), agent=cast("Any", None), session_log=NoSessionLog(), state=a_state(tmp_path))


def _ctx(session: Runtime) -> RunContext[Runtime]:
    """One turn's context over `session`."""
    return RunContext(deps=session, model=TestModel(), usage=RunUsage())


async def test_the_run_route_refuses_in_the_shared_wording(tmp_path: Path) -> None:
    pool = SessionPool(a_runtime(registry=StepRegistry(), library=_library(), state=a_state(tmp_path)))
    async with a_client_of(a_running_controller(pool, tmp_path)) as client:
        refused = await client.post("/api/run", json={"workflow_id": WORKFLOW_ID, "arguments": {"quarter": "Q1"}})

    assert refused.status_code == 400
    assert refused.json()["message"] == _shared_refusal()


async def test_a_resume_of_the_waiting_run_own_workflow_refuses_in_the_shared_wording(tmp_path: Path) -> None:
    session = _session(tmp_path)
    session.active.workflow_id = WORKFLOW_ID

    answer = await controller.run_resume(session, None, records_under(tmp_path), FakeBroadcaster())

    assert answer.status_code == 400
    assert json.loads(bytes(answer.body))["message"] == _shared_refusal()


async def test_a_resume_over_a_draft_refuses_with_the_draft_verdict_in_the_shared_wording(tmp_path: Path) -> None:
    session = _session(tmp_path)
    spec_yaml = yaml.safe_dump(_one_node_spec(WORKFLOW_ID).model_dump(mode="json"))
    session.conversation.draft = Draft(spec_yaml=spec_yaml)
    _, findings = verdict_on(spec_yaml, session.registry)

    answer = await controller.run_resume(session, None, records_under(tmp_path), FakeBroadcaster())

    assert answer.status_code == 400
    assert json.loads(bytes(answer.body))["message"] == "this app cannot run it. " + ". ".join(findings)


async def test_the_run_tool_puts_its_source_and_its_instruction_around_the_shared_wording(tmp_path: Path) -> None:
    session = _session(tmp_path)
    run = RunCollaborators(records=records_under(tmp_path), topics=FakeBroadcaster(), run_settings=RunSettings())

    with pytest.raises(ModelRetry) as refused:
        await run_workflow(_ctx(session), WORKFLOW_ID, {"quarter": "Q1"}, run=run)

    assert str(refused.value) == f"refusing to run {WORKFLOW_ID}. {_shared_refusal()}. Do not substitute steps. Say what is missing."
    assert session.active.task is None


async def test_scheduling_refuses_with_the_running_slice_error(tmp_path: Path) -> None:
    session = _session(tmp_path)

    with pytest.raises(ModelRetry) as refused:
        await schedule_workflow(Scheduler(FakeSchedules(), FakeSchedules(), FakeBroadcaster()), _ctx(session), WORKFLOW_ID, "every 30m", run_engine="in_process")

    assert isinstance(refused.value.__cause__, WorkflowNotRunnableError)
    assert str(refused.value) == f"cannot schedule {WORKFLOW_ID}. {_shared_refusal()}"
