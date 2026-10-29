"""The route a knowledge base's refresh run is reported to: arm the schedules waiting on it and tell the owner, once per run."""

from __future__ import annotations

import json

from typing import TYPE_CHECKING
from uuid import uuid4

import pytest

from core.ai.workflows.contracts import LibraryError, StepRegistry
from pydantic import ValidationError
from pydantic_ai import Agent
from pydantic_ai.models.test import TestModel

from harness.features.running.records import RunCollaborators
from harness.features.scheduling.api_models import RefreshRunIn
from harness.features.scheduling.controller import SchedulingController
from harness.features.scheduling.domain import Schedule, new_sources_notice
from harness.features.scheduling.service import Scheduler
from harness.session import NoSessionLog, Runtime, SessionPool
from harness.settings import RunSettings
from tests.support.cards import base_card, base_workflow
from tests.support.fakes.broadcaster import FakeBroadcaster
from tests.support.fakes.owner_notifier import FakeOwnerNotifier
from tests.support.fakes.schedules import FakeSchedules
from tests.support.fakes.sessions import APool
from tests.support.state import a_state, records_under
from tests.unit.test_schedule_ticker import FakeWorkflowStore

if TYPE_CHECKING:
    from pathlib import Path

    from fastapi.responses import JSONResponse
    from pydantic import JsonValue

NIGHTLY = "wf.nightly-report.v1"


def _store(card_error: Exception | None = None) -> FakeWorkflowStore:
    return FakeWorkflowStore(specs={NIGHTLY: base_workflow("nightly-report")}, cards={NIGHTLY: base_card(NIGHTLY)}, card_error=card_error)


def _controller(store: FakeWorkflowStore, schedules: FakeSchedules, home: Path) -> SchedulingController:
    runtime = Runtime(registry=StepRegistry(), library=store, agent=Agent(TestModel(), deps_type=Runtime), session_log=NoSessionLog(), state=a_state(home))
    pool = SessionPool(runtime)
    topics = FakeBroadcaster()
    return SchedulingController(
        scheduler=Scheduler(schedules, schedules, topics),
        sessions=pool,
        run=RunCollaborators(records=records_under(home), topics=topics, run_settings=RunSettings(), sessions=APool(pool)),
    )


def _body(response: JSONResponse) -> JsonValue:
    return json.loads(bytes(response.body))


def _report(created: int = 6) -> RefreshRunIn:
    return RefreshRunIn(event_id=uuid4(), knowledge_base="notes", sources_created=created, requested_by_entry={"x:someone": created})


async def test_a_refresh_run_that_created_sources_arms_the_schedule_selecting_its_knowledge_base_and_tells_the_owner(tmp_path: Path) -> None:
    schedules = FakeSchedules()
    waiting = Schedule(workflow_id=NIGHTLY, arguments={"base": "notes"}, cadence="on refresh")
    elsewhere = Schedule(workflow_id=NIGHTLY, arguments={"base": "other"}, cadence="on refresh")
    by_clock = Schedule(workflow_id=NIGHTLY, arguments={"base": "notes"}, cadence="every 1h")
    after_a_run = Schedule(workflow_id=NIGHTLY, arguments={"base": "notes"}, cadence="after gather-notes")
    paused = Schedule(workflow_id=NIGHTLY, arguments={"base": "notes"}, cadence="on refresh", enabled=False)
    for row in (waiting, elsewhere, by_clock, after_a_run, paused):
        await schedules.insert(row)
    notifier = FakeOwnerNotifier()

    answer = _body(await _controller(_store(), schedules, tmp_path).report_refresh_run(_report(), notifier))

    assert answer == {"repeat": False, "notified": True, "armed": [str(waiting.id)]}
    assert notifier.sent == [("New sources", "notes: 6 new sources (6 found by x:someone)")]
    stored = await schedules.get(waiting.id)
    assert stored is not None
    assert stored.next_run_at is not None


async def test_the_same_refresh_run_reported_twice_arms_nothing_and_tells_nobody_again(tmp_path: Path) -> None:
    schedules = FakeSchedules()
    waiting = Schedule(workflow_id=NIGHTLY, arguments={"base": "notes"}, cadence="on refresh")
    await schedules.insert(waiting)
    notifier = FakeOwnerNotifier()
    controller = _controller(_store(), schedules, tmp_path)
    report = _report()

    await controller.report_refresh_run(report, notifier)
    fired = await schedules.get(waiting.id)
    assert fired is not None
    await schedules.stamp(
        waiting.id, last_run_at=fired.created_at, last_outcome="launched", next_run_at=None, session_id="", cleared_page_ids=()
    )
    again = _body(await controller.report_refresh_run(report, notifier))

    assert again == {"repeat": True, "notified": False, "armed": []}
    assert len(notifier.sent) == 1
    rested = await schedules.get(waiting.id)
    assert rested is not None
    assert rested.next_run_at is None


async def test_a_refresh_run_that_created_nothing_arms_nothing_tells_nobody_and_records_nothing(tmp_path: Path) -> None:
    schedules = FakeSchedules()
    await schedules.insert(Schedule(workflow_id=NIGHTLY, arguments={"base": "notes"}, cadence="on refresh"))
    notifier = FakeOwnerNotifier()

    answer = _body(await _controller(_store(), schedules, tmp_path).report_refresh_run(_report(created=0), notifier))

    assert answer == {"repeat": False, "notified": False, "armed": []}
    assert notifier.sent == []
    assert schedules.received == set()


async def test_a_card_the_library_cannot_read_is_a_503_and_records_nothing(tmp_path: Path) -> None:
    schedules = FakeSchedules()
    await schedules.insert(Schedule(workflow_id=NIGHTLY, arguments={"base": "notes"}, cadence="on refresh"))
    notifier = FakeOwnerNotifier()

    response = await _controller(_store(card_error=LibraryError("the library is down")), schedules, tmp_path).report_refresh_run(_report(), notifier)

    assert response.status_code == 503
    assert schedules.received == set(), "a refusal records nothing, so the studio's next delivery is heard in full"
    assert notifier.sent == []


async def test_a_notification_that_did_not_reach_the_owner_still_arms(tmp_path: Path) -> None:
    schedules = FakeSchedules()
    waiting = Schedule(workflow_id=NIGHTLY, arguments={"base": "notes"}, cadence="on refresh")
    await schedules.insert(waiting)

    answer = _body(await _controller(_store(), schedules, tmp_path).report_refresh_run(_report(), FakeOwnerNotifier(delivers=False)))

    assert answer == {"repeat": False, "notified": False, "armed": [str(waiting.id)]}


@pytest.mark.parametrize(
    ("created", "by_entry", "said"),
    [
        (6, {"x:someone": 6}, "notes: 6 new sources (6 found by x:someone)"),
        (1, {}, "notes: 1 new source"),
        (9, {"xml:https://feed.example/a.xml": 2, "x:someone": 6, "x:quiet": 0}, "notes: 9 new sources (6 found by x:someone, 2 found by xml:https://feed.example/a.xml)"),
    ],
)
def test_the_owner_is_told_how_many_sources_came_and_from_where(created: int, by_entry: dict[str, int], said: str) -> None:
    assert new_sources_notice("notes", created, by_entry) == said


@pytest.mark.parametrize(
    "body",
    [
        {"event_id": "not-a-uuid", "knowledge_base": "notes"},
        {"event_id": str(uuid4()), "knowledge_base": ""},
        {"event_id": str(uuid4()), "knowledge_base": "notes", "sources_created": -1},
    ],
)
def test_a_report_that_is_not_one_is_refused(body: dict[str, object]) -> None:
    with pytest.raises(ValidationError):
        RefreshRunIn.model_validate(body)
