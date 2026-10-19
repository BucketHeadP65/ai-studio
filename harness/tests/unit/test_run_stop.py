"""A run can be stopped for good, distinct from a pause: attested, archived, no session log left.

Pause steps the session log on disk so a resume can pick it back up; stop is the other ending --
mechanically the same unwind, but it closes the book: an evidence record with `outcome="stopped"`,
an archive record, and the on-disk session log gone. The chat entry's `run_workflow` answers a stop
with its own sentence, distinct from a pause's, so the person reading the turn is not told a
stopped run is still resumable.
"""

from __future__ import annotations

import asyncio
import json

from contextlib import asynccontextmanager
from typing import TYPE_CHECKING, Any, cast

import pytest

from core.ai.workflows.contracts import FromNode, FromParameter, ParameterSpec, PersonGate, RunEvidence, RunNode, StepRegistry, Workflow
from pydantic_ai.models.test import TestModel
from pydantic_ai.tools import RunContext
from pydantic_ai.usage import RunUsage

from harness.features.running import service
from harness.features.running.infrastructure.run_log import load_waiting
from harness.features.running.records import RunCollaborators
from harness.features.running.tools import run_workflow, stop_run
from harness.session import NoSessionLog, Runtime, SessionPool, run_log_path
from harness.settings import RunSettings
from tests.conftest import CallLog, ReportUrl, SalesData, register_quarterly_steps
from tests.support.fakes.broadcaster import FakeBroadcaster
from tests.support.fakes.sessions import APool
from tests.support.running import a_pool, a_running_controller
from tests.support.runtime import a_runtime
from tests.support.state import a_state, records_under

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator
    from pathlib import Path


def _session(registry: StepRegistry, home: Path) -> Runtime:
    return Runtime(registry=registry, library=cast("Any", None), agent=cast("Any", object()), session_log=NoSessionLog(), state=a_state(home))


def _ctx(session: Runtime) -> RunContext[Runtime]:
    return RunContext(deps=session, model=TestModel(), usage=RunUsage())


def _held_open_spec(hold: asyncio.Event) -> tuple[Workflow, StepRegistry, CallLog]:
    """A tiny two-fetch-one-reconcile graph with `fetch_sales` waiting on `hold`, so a stop lands mid-run."""
    call_log = CallLog()
    registry = StepRegistry()
    register_quarterly_steps(registry, call_log, exclude=frozenset({"fetch_sales"}))

    async def fetch_sales(quarter: str) -> SalesData:
        await hold.wait()
        return SalesData(quarter=quarter, total=1)

    registry.register(fetch_sales)
    spec = Workflow(
        name="quarterly-report",
        version=4,
        description="Fetch and reconcile, with one fetch held open so the stop lands mid-run.",
        parameters={"quarter": ParameterSpec(description="Fiscal quarter", json_schema={"type": "string"})},
        nodes=(
            RunNode(id="fetch_sales", work="fetch_sales", inputs={"quarter": FromParameter(parameter="quarter")}),
            RunNode(id="fetch_costs", work="fetch_costs", inputs={"quarter": FromParameter(parameter="quarter")}),
            RunNode(
                id="reconcile", work="reconcile", inputs={"sales": FromNode(node="fetch_sales"), "costs": FromNode(node="fetch_costs")}
            ),
        ),
        output=FromNode(node="reconcile"),
    )
    return spec, registry, call_log


def _gated_then_held_spec(hold: asyncio.Event) -> tuple[Workflow, StepRegistry, CallLog]:
    """The report with a gate before `publish`, and `publish` held open on `hold`.

    Automode passes the gate itself, so a stop landing on the held `publish` node is a stop
    reached AFTER an auto-approval -- the scenario the gate's evidence record must tell apart from a
    human's own approval.
    """
    call_log = CallLog()
    registry = StepRegistry()
    register_quarterly_steps(registry, call_log, exclude=frozenset({"publish"}))

    async def publish(report: object) -> ReportUrl:
        del report
        await hold.wait()
        return ReportUrl(url="https://reports.example/held")

    registry.register(publish)
    spec = Workflow(
        name="quarterly-report",
        version=4,
        description="Fetch, reconcile, draft, gate, then hold publish open so a stop lands after the gate.",
        parameters={"quarter": ParameterSpec(description="Fiscal quarter", json_schema={"type": "string"})},
        nodes=(
            RunNode(id="fetch_sales", work="fetch_sales", inputs={"quarter": FromParameter(parameter="quarter")}),
            RunNode(id="fetch_costs", work="fetch_costs", inputs={"quarter": FromParameter(parameter="quarter")}),
            RunNode(
                id="reconcile", work="reconcile", inputs={"sales": FromNode(node="fetch_sales"), "costs": FromNode(node="fetch_costs")}
            ),
            RunNode(id="draft_report", work="draft_report", inputs={"ledger": FromNode(node="reconcile")}),
            PersonGate(id="approve", reviews=FromNode(node="draft_report"), rejected_to="draft_report"),
            RunNode(id="publish", work="publish", inputs={"report": FromNode(node="approve")}),
        ),
        output=FromNode(node="publish"),
    )
    return spec, registry, call_log


async def test_stopping_after_an_automode_gate_writes_the_real_gates_auto_not_an_empty_one(tmp_path: Path) -> None:
    """A gate automode passed, then a stop mid-run: the stop must not attest it as human-approved."""
    records = records_under(tmp_path)
    hold = asyncio.Event()
    spec, registry, _call_log = _gated_then_held_spec(hold)
    session = _session(registry, tmp_path)
    session.session_id = "sess-auto"
    service.launch(session, records=records, topics=FakeBroadcaster(), spec=spec, workflow_id="wf.report", params={"quarter": "Q2"}, gate_mode="auto")
    for _ in range(500):
        if "publish" in session.active.progress.running:
            break
        await asyncio.sleep(0.01)
    else:
        pytest.fail("the run never reached the held node after its auto-approved gate")

    assert await service.stop(session, records=records) is True

    events = await records.evidence.events_for("wf.report")
    evidence_records = [event for event in events if isinstance(event, RunEvidence)]
    assert len(evidence_records) == 1
    evidence_record = evidence_records[0]
    assert evidence_record.outcome == "stopped"
    assert evidence_record.gates_auto == ("approve",), "the gate automode passed must be named, not silently dropped"
    assert "approve" not in evidence_record.gates_approved, "an auto-passed gate is never attested as a human approval"

    archived = records.archive.recent(10)
    assert len(archived) == 1
    record = records.archive.get(archived[0].run_id)
    assert record is not None
    assert record.gates_auto == ("approve",)
    assert "approve" not in record.gates_approved


async def test_stop_with_nothing_live_reports_false(registry: StepRegistry, tmp_path: Path) -> None:
    records = records_under(tmp_path)
    assert await service.stop(_session(registry, tmp_path), records=records) is False


async def test_stopping_a_button_launched_run_ends_it_attested_and_archived_with_no_journal(
    quarterly_report: Workflow, registry: StepRegistry, tmp_path: Path
) -> None:
    records = records_under(tmp_path)
    session = _session(registry, tmp_path)
    session.session_id = "sess-1"
    service.launch(session, records=records, topics=FakeBroadcaster(), spec=quarterly_report, workflow_id="wf.report", params={"quarter": "Q2"})
    for _ in range(500):
        if session.active.pending is not None:
            break
        await asyncio.sleep(0.01)
    else:
        pytest.fail("the run never waiting at its gate")

    assert await service.stop(session, records=records) is True

    assert session.active.task is None, "a stopped run's task is cleared, so run_phase reads it as nothing live"
    assert service.run_phase(session) is None, "a stopped run steps no ghost card, unlike a paused one"
    assert load_waiting(run_log_path(session)) is None, "stop steps no waiting session log for a resume to pick up"

    events = await records.evidence.events_for("wf.report")
    evidence_records = [event for event in events if isinstance(event, RunEvidence)]
    assert len(evidence_records) == 1
    assert evidence_records[0].outcome == "stopped"

    archived = records.archive.recent(10)
    assert len(archived) == 1
    assert archived[0].outcome == "stopped"
    record = records.archive.get(archived[0].run_id)
    assert record is not None
    assert "fetch_sales" in record.values, "the run's own progress up to the stop is kept, not discarded"


async def test_stopping_a_scheduled_run_records_it_under_the_schedule_that_launched_it(
    quarterly_report: Workflow, registry: StepRegistry, tmp_path: Path
) -> None:
    records = records_under(tmp_path)
    session = _session(registry, tmp_path)
    service.launch(
        session,
        records=records,
        topics=FakeBroadcaster(),
        spec=quarterly_report,
        workflow_id="wf.report",
        params={"quarter": "Q2"},
        runner="app:schedule:sched-7",
    )
    for _ in range(500):
        if session.active.pending is not None:
            break
        await asyncio.sleep(0.01)
    else:
        pytest.fail("the run never waiting at its gate")

    assert await service.stop(session, records=records) is True

    events = await records.evidence.events_for("wf.report")
    evidence_records = [event for event in events if isinstance(event, RunEvidence)]
    assert [evidence_record.runner for evidence_record in evidence_records] == ["app:schedule:sched-7"]
    archived = records.archive.recent(10)
    assert len(archived) == 1
    record = records.archive.get(archived[0].run_id)
    assert record is not None
    assert record.runner == "app:schedule:sched-7"


async def test_stopping_a_chat_run_settles_into_the_stopped_sentence_not_the_paused_one(tmp_path: Path) -> None:
    records = records_under(tmp_path)
    hold = asyncio.Event()
    spec, registry, _call_log = _held_open_spec(hold)
    session = _session(registry, tmp_path)
    session.conversation.last_workflow = spec
    await run_workflow(_ctx(session), "draft", {"quarter": "Q2"}, run=RunCollaborators(records=records, run_settings=RunSettings(), sessions=APool(SessionPool(session)), topics=FakeBroadcaster()))
    for _ in range(500):
        if session.active.task is not None and session.active.progress.running:
            break
        await asyncio.sleep(0.01)
    else:
        pytest.fail("the run never started")

    assert await service.stop(session, records=records), "the chat run's task is the one the stop plugin cancels"
    settlement = session.active.settlement
    assert settlement is not None
    answer = await settlement

    assert "stopped for good" in answer
    assert "do not restart" in answer
    assert load_waiting(run_log_path(session)) is None, "the stop plugin already archived and unlinked the session log"
    events = await records.evidence.events_for("draft")
    evidence_records = [event for event in events if isinstance(event, RunEvidence)]
    assert len(evidence_records) == 1, "the chat entry's own ledger append is skipped; the stop plugin already wrote one"
    assert evidence_records[0].outcome == "stopped"


async def test_a_run_with_no_listener_closes_its_own_book(tmp_path: Path) -> None:
    """The turn that started the run is long gone; the run carries on and closes its own book."""
    records = records_under(tmp_path)
    hold = asyncio.Event()
    spec, registry, _call_log = _held_open_spec(hold)
    session = _session(registry, tmp_path)
    session.conversation.last_workflow = spec
    await run_workflow(_ctx(session), "draft", {"quarter": "Q2"}, run=RunCollaborators(records=records, run_settings=RunSettings(), sessions=APool(SessionPool(session)), topics=FakeBroadcaster()))
    for _ in range(500):
        if session.active.task is not None and session.active.progress.running:
            break
        await asyncio.sleep(0.01)
    else:
        pytest.fail("the run never started")

    assert session.active.task is not None, "the graph task outlives the turn that started it"
    assert not session.active.task.done(), "the graph task outlives the turn that started it"
    hold.set()
    for _ in range(500):
        if session.active.result:
            break
        await asyncio.sleep(0.01)
    assert session.active.result.startswith("Finished"), "the settlement spoke with nobody listening"
    events = await records.evidence.events_for("draft")
    evidence_records = [event for event in events if isinstance(event, RunEvidence)]
    assert [evidence_record.outcome for evidence_record in evidence_records] == ["completed"]


async def test_the_stop_run_tool_ends_this_sessions_run_and_answers_plainly(tmp_path: Path) -> None:
    records = records_under(tmp_path)
    hold = asyncio.Event()
    spec, registry, _call_log = _held_open_spec(hold)
    session = _session(registry, tmp_path)
    session.conversation.last_workflow = spec
    turn = asyncio.create_task(run_workflow(_ctx(session), "draft", {"quarter": "Q2"}, run=RunCollaborators(records=records, run_settings=RunSettings(), sessions=APool(SessionPool(session)), topics=FakeBroadcaster())))
    for _ in range(500):
        if session.active.task is not None and session.active.progress.running:
            break
        await asyncio.sleep(0.01)
    else:
        pytest.fail("the run never started")

    answer = await stop_run(_ctx(session), records=records, sessions=APool(SessionPool(session)), run=RunSettings())
    await turn

    assert answer.startswith("Stopped")
    assert "pending for the next run" in answer
    assert await service.stop(session, records=records) is False, "the tool went through the same stop plugin; nothing is left live"


async def test_the_stop_run_tool_with_nothing_live_says_so(registry: StepRegistry, tmp_path: Path) -> None:
    records = records_under(tmp_path)
    session = _session(registry, tmp_path)
    assert "nothing is running in this session" in await stop_run(_ctx(session), records=records, sessions=APool(SessionPool(session)), run=RunSettings())


async def test_the_stop_run_tool_refuses_a_prefix_no_held_session_matches(registry: StepRegistry, tmp_path: Path) -> None:
    records = records_under(tmp_path)
    async with _installed_pool(registry):
        session = _session(registry, tmp_path)
        answer = await stop_run(_ctx(session), session="deadbeef", records=records, sessions=APool(SessionPool(session)), run=RunSettings())

    assert "no held session matches" in answer


async def test_stopping_twice_the_second_call_finds_nothing_live(quarterly_report: Workflow, registry: StepRegistry, tmp_path: Path) -> None:
    records = records_under(tmp_path)
    session = _session(registry, tmp_path)
    service.launch(session, records=records, topics=FakeBroadcaster(), spec=quarterly_report, workflow_id="wf.report", params={"quarter": "Q2"})
    for _ in range(500):
        if session.active.pending is not None:
            break
        await asyncio.sleep(0.01)

    assert await service.stop(session, records=records) is True
    assert await service.stop(session, records=records) is False


@asynccontextmanager
async def _installed_pool(registry: StepRegistry) -> AsyncGenerator[SessionPool]:
    """A pool this process holds, for the agent tools that still read the installed one."""
    pool = SessionPool(a_runtime(registry=registry))
    yield pool
async def test_stop_route_refuses_when_nothing_is_running(registry: StepRegistry, tmp_path: Path) -> None:
    controller = a_running_controller(a_pool(registry), tmp_path)

    refused = await controller.stop_run()

    assert refused.status_code == 409
    assert json.loads(bytes(refused.body)) == {"ok": False, "message": "nothing is running"}


async def test_stop_route_ends_the_unscoped_sessions_run(quarterly_report: Workflow, registry: StepRegistry, tmp_path: Path) -> None:
    records = records_under(tmp_path)
    pool = a_pool(registry, state=tmp_path)
    controller = a_running_controller(pool, tmp_path)
    service.launch(
        pool.default, records=records, topics=FakeBroadcaster(), spec=quarterly_report, workflow_id="wf.report", params={"quarter": "Q2"}
    )
    for _ in range(500):
        if pool.default.active.pending is not None:
            break
        await asyncio.sleep(0.01)

    stopped = await controller.stop_run()

    assert stopped.status_code == 200
    assert json.loads(bytes(stopped.body)) == {"ok": True, "stopped": "wf.report"}
    assert service.run_phase(pool.default) is None


async def test_scoped_stop_route_ends_that_sessions_run(quarterly_report: Workflow, registry: StepRegistry, tmp_path: Path) -> None:
    records = records_under(tmp_path)
    pool = a_pool(registry, state=tmp_path)
    controller = a_running_controller(pool, tmp_path)
    runtime = await pool.create(None)
    service.launch(runtime, records=records, topics=FakeBroadcaster(), spec=quarterly_report, workflow_id="wf.report", params={"quarter": "Q2"})
    for _ in range(500):
        if runtime.active.pending is not None:
            break
        await asyncio.sleep(0.01)

    stopped = await controller.session_stop_run(runtime.session_id or "")

    assert stopped.status_code == 200
    assert json.loads(bytes(stopped.body)) == {"ok": True, "stopped": "wf.report"}


async def test_scoped_stop_route_on_an_unknown_session_is_a_404(registry: StepRegistry, tmp_path: Path) -> None:
    controller = a_running_controller(a_pool(registry), tmp_path)

    refused = await controller.session_stop_run("00000000-0000-0000-0000-000000000000")

    assert refused.status_code == 404
    assert json.loads(bytes(refused.body)) == {"ok": False, "message": "no session with that id here"}


async def test_the_stop_route_on_temporal_refuses_in_words(registry: StepRegistry, tmp_path: Path) -> None:
    controller = a_running_controller(a_pool(registry), tmp_path, run=RunSettings(run_engine="temporal"))

    refused = await controller.stop_run()

    assert refused.status_code == 409
    assert "outlives this process" in str(json.loads(bytes(refused.body))["message"])


if __name__ == "__main__":  # pragma: no cover
    pytest.main([__file__])
