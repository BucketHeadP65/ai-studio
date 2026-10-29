"""The route a knowledge base's index is reported to: arm the on-index schedules naming it with its changed pages, once per index."""

from __future__ import annotations

import json

from typing import TYPE_CHECKING
from uuid import uuid4

import pytest

from core.ai.workflows.contracts import StepRegistry
from pydantic_ai import Agent
from pydantic_ai.models.test import TestModel

from harness.features.running.records import RunCollaborators
from harness.features.scheduling.api_models import IndexRunIn
from harness.features.scheduling.controller import SchedulingController
from harness.features.scheduling.domain import Schedule
from harness.features.scheduling.service import Scheduler
from harness.session import NoSessionLog, Runtime, SessionPool
from harness.settings import RunSettings
from tests.support.controllers import a_client_of
from tests.support.fakes.broadcaster import FakeBroadcaster
from tests.support.fakes.schedules import FakeSchedules
from tests.support.fakes.sessions import APool
from tests.support.state import a_state, records_under
from tests.unit.test_schedule_ticker import FakeWorkflowStore

if TYPE_CHECKING:
    from pathlib import Path

    from fastapi.responses import JSONResponse
    from pydantic import JsonValue

NIGHTLY = "wf.nightly-report.v1"


def _controller(schedules: FakeSchedules, home: Path) -> SchedulingController:
    runtime = Runtime(
        registry=StepRegistry(), library=FakeWorkflowStore(), agent=Agent(TestModel(), deps_type=Runtime), session_log=NoSessionLog(), state=a_state(home)
    )
    pool = SessionPool(runtime)
    topics = FakeBroadcaster()
    return SchedulingController(
        scheduler=Scheduler(schedules, schedules, topics),
        sessions=pool,
        run=RunCollaborators(records=records_under(home), topics=topics, run_settings=RunSettings(), sessions=APool(pool)),
    )


def _body(response: JSONResponse) -> JsonValue:
    return json.loads(bytes(response.body))


def _report() -> IndexRunIn:
    return IndexRunIn(event_id=uuid4(), knowledge_base="docs", page_ids=["note.b", "note.a"])


async def test_an_index_run_arms_and_answers_the_armed_schedules(tmp_path: Path) -> None:
    schedules = FakeSchedules()
    waiting = Schedule(workflow_id=NIGHTLY, cadence="on index docs")
    elsewhere = Schedule(workflow_id=NIGHTLY, cadence="on index other")
    on_refresh = Schedule(workflow_id=NIGHTLY, arguments={"base": "docs"}, cadence="on refresh")
    for row in (waiting, elsewhere, on_refresh):
        await schedules.insert(row)

    response = await _controller(schedules, tmp_path).report_index_run(_report())

    assert response.status_code == 200
    assert _body(response) == {"repeat": False, "armed": [str(waiting.id)]}
    stored = await schedules.get(waiting.id)
    assert stored is not None
    assert stored.next_run_at is not None
    assert stored.pending_page_ids == ("note.a", "note.b")


async def test_a_repeated_index_run_answers_a_repeat(tmp_path: Path) -> None:
    schedules = FakeSchedules()
    waiting = Schedule(workflow_id=NIGHTLY, cadence="on index docs")
    await schedules.insert(waiting)
    controller = _controller(schedules, tmp_path)
    report = _report()

    await controller.report_index_run(report)
    again = _body(await controller.report_index_run(report))

    assert again == {"repeat": True, "armed": []}


@pytest.mark.parametrize(
    "body",
    [
        {"event_id": str(uuid4()), "knowledge_base": "docs"},
        {"event_id": str(uuid4()), "knowledge_base": "docs", "page_ids": []},
        {"event_id": str(uuid4()), "knowledge_base": "", "page_ids": ["note.a"]},
    ],
)
async def test_a_report_that_is_not_one_is_refused(body: dict[str, object], tmp_path: Path) -> None:
    schedules = FakeSchedules()
    await schedules.insert(Schedule(workflow_id=NIGHTLY, cadence="on index docs"))

    async with a_client_of(_controller(schedules, tmp_path)) as client:
        response = await client.post("/api/schedules/index-runs", json=body)

    assert response.status_code == 422
    assert schedules.received_index == set(), "a refusal records nothing"
