"""The orchestrator decider: one structured turn answers a gate that names no person.

Two plugins reach it: an authored `OrchestratorGate` (core calls `registry.decide_gate` directly,
bypassing `ChatApproval` entirely -- so automode can never wave one through), and a `PersonGate`
under a run's own `gate_mode="orchestrate"` (`ChatApproval` converts it, through the same
decider). Both land in the same honest bucket, `gates_orchestrated`, never `gates_approved`.
"""

from __future__ import annotations

import asyncio

from dataclasses import fields
from typing import TYPE_CHECKING, Any, cast

import pytest

from core.ai.workflows.contracts import (
    CURRENT_STEP,
    Briefing,
    FromNode,
    OrchestratorGate,
    PersonGate,
    RunEvidence,
    RunNode,
    Shape,
    StepRegistry,
    Workflow,
)
from pydantic_ai import Agent
from pydantic_ai.messages import ModelResponse, ToolCallPart, UserPromptPart
from pydantic_ai.models.function import FunctionModel
from pydantic_ai.models.test import TestModel
from pydantic_ai.toolsets import FunctionToolset

from harness import session as session_state
from harness.features.followme.acts import ConsoleApprovals
from harness.features.running import service
from harness.features.running.api_models import RunRequest
from harness.features.running.domain.ended_run import RunArchiveRecord
from harness.features.running.feedback import orchestrator_decider
from harness.features.scheduling.api_models import ScheduleIn, ScheduleOut, SchedulePatch
from harness.features.scheduling.domain import Schedule, ScheduleUpdate
from harness.session import ActiveRun, NoSessionLog, Runtime, SessionPool, answerable
from harness.workspace import open_workspace
from tests.support.fakes.broadcaster import FakeBroadcaster
from tests.support.fakes.sessions import APool
from tests.support.state import a_state, a_throwaway_state, records_under

if TYPE_CHECKING:
    from collections.abc import Sequence
    from pathlib import Path

    from pydantic import BaseModel
    from pydantic_ai.messages import ModelMessage
    from pydantic_ai.models.function import AgentInfo

    from harness.features.running.records import RunRecords
    from tests.conftest import CallLog

CHARGE = "Check the draft names a real net figure and reject anything reading as a placeholder."


def _prompt_of(messages: Sequence[ModelMessage]) -> str:
    """The user prompt text the model's last request carried, or `""` when its last part is not one."""
    part = messages[-1].parts[-1]
    return str(part.content) if isinstance(part, UserPromptPart) else ""


def _orchestrated(quarterly_report: Workflow, **overrides: object) -> Workflow:
    """`quarterly_report` with its person gate replaced by an orchestrator gate, same id and wiring."""
    node: dict[str, object] = {
        "kind": "orchestrator-gate",
        "id": "approve",
        "charge": CHARGE,
        "reviews": {"node": "draft_report"},
        "rejected_to": "draft_report",
    }
    node.update(overrides)
    return quarterly_report.model_copy(
        update={"nodes": (*quarterly_report.nodes[:4], OrchestratorGate.model_validate(node), quarterly_report.nodes[5])}
    )


def _worker_spec() -> Workflow:
    """One step the artifact brings itself, reviewed by a declared orchestrator gate.

    `perform` (not `register`) builds this step, so the rerun reads the repair the gate filed off
    its own step identity.
    """
    return Workflow(
        name="worker-orchestrated",
        version=1,
        description="One brought step, a declared orchestrator gate reviewing it, nothing after.",
        inline=(
            Briefing(
                name="draft_task",
                instructions="Draft a one-line report naming the net figure, ready for a review to approve or send back.",
                output=Shape(json_schema={"type": "string"}),
            ),
        ),
        nodes=(
            RunNode(id="draft", work="draft_task"),
            OrchestratorGate(id="approve", charge=CHARGE, reviews=FromNode(node="draft"), rejected_to="draft"),
        ),
        output=FromNode(node="approve"),
    )


def _agent(model: TestModel | FunctionModel) -> Agent[Runtime, str]:
    return Agent(model, deps_type=Runtime, output_type=str)


def _session_with_decider(registry: StepRegistry, model: TestModel | FunctionModel, tmp_path: Path) -> tuple[Runtime, SessionPool]:
    """A session whose registry answers an orchestrator gate through this exact session's agent, and its pool."""
    session = Runtime(registry=registry, library=cast("Any", None), agent=_agent(model), session_log=NoSessionLog(), state=a_state(tmp_path))
    pool = SessionPool(session)
    session.registry.decide_gate = orchestrator_decider(session, APool(pool))
    return session, pool


def _final_result(*, approve: bool, feedback: str = "", call_id: str = "1") -> ModelResponse:
    args = {"approve": approve, "feedback": feedback}
    return ModelResponse(parts=[ToolCallPart(tool_name="final_result", args=args, tool_call_id=call_id)])


async def _events(workflow_id: str, records: RunRecords) -> list[object]:
    return list(await records.evidence.events_for(workflow_id))


def _attested(workflow_id_events: list[object]) -> list[RunEvidence]:
    return [event for event in workflow_id_events if isinstance(event, RunEvidence)]


# --- an authored gate decides properly, under every mode ---------------------------------


async def test_authored_orchestrator_gate_approves_and_the_value_passes_through(quarterly_report: Workflow, registry: StepRegistry, call_log: CallLog, tmp_path: Path) -> None:
    records = records_under(tmp_path)
    spec = _orchestrated(quarterly_report)
    session, _pool = _session_with_decider(registry, TestModel(custom_output_args={"approve": True, "feedback": ""}), tmp_path)

    service.launch(session, records=records, topics=FakeBroadcaster(), spec=spec, workflow_id="wf.orchestrated-approve", params={"quarter": "Q3"})
    task = session.active.task
    assert task is not None
    await task

    assert call_log.calls.count("publish") == 1
    assert "Finished" in session.active.result
    attested = _attested(await _events("wf.orchestrated-approve", records))
    assert [event.outcome for event in attested] == ["completed"]
    assert attested[0].gates_orchestrated == ("approve",)
    assert attested[0].gates_approved == (), "an orchestrator's decision is never read as a human's"


async def test_orchestrator_rejects_with_feedback_then_the_worker_reruns_and_the_second_decision_approves(quarterly_report: Workflow, registry: StepRegistry, call_log: CallLog, tmp_path: Path) -> None:
    """The full loop through the engine: reject with words, `draft_report` reruns, the next verdict releases it."""
    records = records_under(tmp_path)
    spec = _orchestrated(quarterly_report)
    heard: list[int] = []

    async def respond(messages: Sequence[ModelMessage], info: AgentInfo) -> ModelResponse:
        del info
        heard.append(len(heard) + 1)
        if len(heard) == 1:
            return _final_result(approve=False, feedback="Name the actual net figure.", call_id="c1")
        return _final_result(approve=True, call_id="c2")

    session, _pool = _session_with_decider(registry, FunctionModel(respond), tmp_path)
    service.launch(session, records=records, topics=FakeBroadcaster(), spec=spec, workflow_id="wf.orchestrated-rerun", params={"quarter": "Q3"})
    task = session.active.task
    assert task is not None
    await task

    assert call_log.calls.count("draft_report") == 2, call_log.calls
    assert call_log.calls.count("fetch_sales") == 1, "a rejection rewinds to the gate's target, not to the top"
    assert call_log.calls.count("publish") == 1
    assert heard == [1, 2]
    attested = _attested(await _events("wf.orchestrated-rerun", records))
    assert attested[0].gates_orchestrated == ("approve",)


async def test_a_rejected_workers_rerun_reads_the_orchestrators_own_feedback(tmp_path: Path) -> None:
    """The rerun reads the words off its own step identity -- the same channel a person's rejection uses."""
    records = records_under(tmp_path)
    spec = _worker_spec()
    seen: list[str | None] = []

    async def perform(task: Briefing, output_type: type, /, **inputs: object) -> object:
        del task, output_type, inputs
        identity = CURRENT_STEP.get()
        assert identity is not None
        owed = identity.retry_feedback[-1].feedback if identity.retry_feedback else None
        seen.append(owed)
        return "revised" if owed else "draft v1"

    registry = StepRegistry(perform=perform)
    decisions: list[int] = []

    async def respond(messages: Sequence[ModelMessage], info: AgentInfo) -> ModelResponse:
        del info
        decisions.append(len(decisions) + 1)
        if len(decisions) == 1:
            return _final_result(approve=False, feedback="Name the actual net figure.", call_id="c1")
        return _final_result(approve=True, call_id="c2")

    session, _pool = _session_with_decider(registry, FunctionModel(respond), tmp_path)
    service.launch(session, records=records, topics=FakeBroadcaster(), spec=spec, workflow_id="wf.worker-feedback", params={})
    task = session.active.task
    assert task is not None
    await task

    assert seen == [None, "Name the actual net figure."], "the second run of the worker reads the rejection's own words"


async def test_automode_does_not_bypass_an_authored_orchestrator_gate(quarterly_report: Workflow, registry: StepRegistry, call_log: CallLog, tmp_path: Path) -> None:
    """Automode is a `ChatApproval` term; an authored gate never reaches `ChatApproval` at all."""
    records = records_under(tmp_path)
    spec = _orchestrated(quarterly_report)
    heard: list[str] = []

    async def respond(messages: Sequence[ModelMessage], info: AgentInfo) -> ModelResponse:
        del info
        heard.append(_prompt_of(messages))
        return _final_result(approve=True)

    session, _pool = _session_with_decider(registry, FunctionModel(respond), tmp_path)
    service.launch(session, records=records, topics=FakeBroadcaster(), spec=spec, workflow_id="wf.orchestrated-auto", params={"quarter": "Q3"}, gate_mode="auto")
    task = session.active.task
    assert task is not None
    await task

    assert heard, "the decider must be asked -- automode does not wave an authored gate through"
    assert call_log.calls.count("publish") == 1
    attested = _attested(await _events("wf.orchestrated-auto", records))
    assert attested[0].gates_orchestrated == ("approve",)
    assert attested[0].gates_auto == (), "an authored gate is never automode's to claim"


async def test_the_decider_reads_the_whole_value_not_a_screen_preview(registry: StepRegistry, tmp_path: Path) -> None:
    """Three drafts run past any preview length; the reviewer is handed every character of them."""
    heard: list[str] = []

    async def respond(messages: Sequence[ModelMessage], info: AgentInfo) -> ModelResponse:
        del info
        heard.append(_prompt_of(messages))
        return _final_result(approve=True)

    session, _pool = _session_with_decider(registry, FunctionModel(respond), tmp_path)
    decide = session.registry.decide_gate
    assert decide is not None
    drafts = [f"draft {index}: " + "the decision and what follows from it, spelled out. " * 150 for index in range(3)]

    decision = await decide("check_adrs", CHARGE, drafts)

    assert decision.approve
    assert len(heard) == 1
    assert len(heard[0]) > 4_000
    assert heard[0].endswith(str(drafts))


# --- orchestrate mode converts a person gate ----------------------------------------------


async def test_orchestrate_mode_converts_a_human_gate(quarterly_report: Workflow, registry: StepRegistry, tmp_path: Path) -> None:
    records = records_under(tmp_path)
    heard: list[tuple[str, str]] = []

    async def respond(messages: Sequence[ModelMessage], info: AgentInfo) -> ModelResponse:
        del info
        heard.append(("call", _prompt_of(messages)))
        return _final_result(approve=True)

    session, _pool = _session_with_decider(registry, FunctionModel(respond), tmp_path)
    service.launch(session, records=records, topics=FakeBroadcaster(), spec=quarterly_report, workflow_id="wf.converted", params={"quarter": "Q2"}, gate_mode="orchestrate")
    task = session.active.task
    assert task is not None
    await task

    assert len(heard) == 1
    assert "Decide in place of the owner at gate 'approve'" in heard[0][1]
    attested = _attested(await _events("wf.converted", records))
    assert attested[0].gates_orchestrated == ("approve",)
    assert attested[0].gates_approved == ()
    assert attested[0].gates_auto == ()


async def test_orchestrate_mode_rejection_routes_feedback_like_a_humans(tmp_path: Path) -> None:
    """A converted gate's rejection still reaches a brought step's own memory."""
    records = records_under(tmp_path)
    spec = Workflow(
        name="worker-converted",
        version=1,
        description="One brought step, a person gate an orchestrate-mode run converts.",
        inline=(
            Briefing(
                name="draft_task",
                instructions="Draft a one-line report naming the net figure, ready for a review to approve or send back.",
                output=Shape(json_schema={"type": "string"}),
            ),
        ),
        nodes=(
            RunNode(id="draft", work="draft_task"),
            PersonGate(id="approve", reviews=FromNode(node="draft"), rejected_to="draft"),
        ),
        output=FromNode(node="approve"),
    )
    seen: list[str | None] = []

    async def perform(task: Briefing, output_type: type, /, **inputs: object) -> object:
        del task, output_type, inputs
        identity = CURRENT_STEP.get()
        assert identity is not None
        owed = identity.retry_feedback[-1].feedback if identity.retry_feedback else None
        seen.append(owed)
        return "revised" if owed else "draft v1"

    registry = StepRegistry(perform=perform)
    decisions: list[int] = []

    async def respond(messages: Sequence[ModelMessage], info: AgentInfo) -> ModelResponse:
        del info
        decisions.append(len(decisions) + 1)
        if len(decisions) == 1:
            return _final_result(approve=False, feedback="Say the quarter this covers.", call_id="c1")
        return _final_result(approve=True, call_id="c2")

    session, _pool = _session_with_decider(registry, FunctionModel(respond), tmp_path)
    service.launch(session, records=records, topics=FakeBroadcaster(), spec=spec, workflow_id="wf.converted-feedback", params={}, gate_mode="orchestrate")
    task = session.active.task
    assert task is not None
    await task

    assert seen == [None, "Say the quarter this covers."]


# --- the busy session: a gate needs an answer, so it waits --------------------------------


async def test_a_busy_session_waits_for_the_turn_lock_rather_than_dropping() -> None:
    heard: list[str] = []

    async def respond(messages: Sequence[ModelMessage], info: AgentInfo) -> ModelResponse:
        del messages, info
        heard.append("decided")
        return _final_result(approve=True)

    session = Runtime(registry=StepRegistry(), library=cast("Any", None), agent=_agent(FunctionModel(respond)), session_log=NoSessionLog(), state=a_throwaway_state())
    pool = SessionPool(session)
    decide = orchestrator_decider(session, APool(pool))
    hold = await pool.begin_default_turn()
    decision_task = asyncio.create_task(decide("approve", CHARGE, "the draft"))
    await asyncio.sleep(0.05)
    assert not decision_task.done(), "a gate needs an answer -- it must wait, not drop the decision"
    assert heard == [], "the turn cannot even start while the session is busy"
    hold.release()
    decision = await asyncio.wait_for(decision_task, timeout=5)
    assert decision.approve is True
    assert heard == ["decided"]
async def test_a_gate_timeout_of_none_keeps_waiting_past_a_would_be_short_ceiling() -> None:
    """`None` is "wait forever" everywhere else a gate timeout is read -- an authored gate's own busy-wait is no exception."""
    session = Runtime(
        registry=StepRegistry(), library=cast("Any", None), agent=_agent(TestModel(custom_output_args={"approve": True, "feedback": ""})), session_log=NoSessionLog(), state=a_throwaway_state()
    )
    session.active.gate_timeout = None
    pool = SessionPool(session)
    decide = orchestrator_decider(session, APool(pool))
    hold = await pool.begin_default_turn()
    decision_task = asyncio.create_task(decide("approve", CHARGE, "the draft"))
    await asyncio.sleep(0.2)  # much longer than the tiny ceiling the starvation test below uses
    assert not decision_task.done(), "gate_timeout=None must never time out the busy-wait"
    hold.release()
    decision = await asyncio.wait_for(decision_task, timeout=5)
    assert decision.approve is True
async def test_a_permanently_busy_session_fails_the_run_after_its_own_gate_timeout(quarterly_report: Workflow, registry: StepRegistry, tmp_path: Path) -> None:
    """A gate nobody can ever answer fails the run in words -- a fake rejection would only starve on the same lock again."""
    records = records_under(tmp_path)
    spec = _orchestrated(quarterly_report)
    session, pool = _session_with_decider(registry, TestModel(custom_output_args={"approve": True, "feedback": ""}), tmp_path)
    hold = await pool.begin_default_turn()  # never released: the session stays busy for good
    try:
        service.launch(session, records=records, topics=FakeBroadcaster(), spec=spec, workflow_id="wf.starved", params={"quarter": "Q3"}, gate_timeout=0.05)
        task = session.active.task
        assert task is not None
        await task

        attested = _attested(await _events("wf.starved", records))
        assert attested[-1].outcome == "failed"
        assert "never became available for gate 'approve'" in session.active.result
    finally:
        hold.release()


# --- stopping mid-decision attests honestly -----------------------------------------------


async def test_stop_mid_orchestrator_decision_attests_honestly(quarterly_report: Workflow, registry: StepRegistry, call_log: CallLog, tmp_path: Path) -> None:
    records = records_under(tmp_path)
    spec = _orchestrated(quarterly_report)
    blocked = asyncio.Event()

    async def respond(messages: Sequence[ModelMessage], info: AgentInfo) -> ModelResponse:
        del messages, info
        await blocked.wait()
        return _final_result(approve=True)

    session, _pool = _session_with_decider(registry, FunctionModel(respond), tmp_path)
    service.launch(session, records=records, topics=FakeBroadcaster(), spec=spec, workflow_id="wf.stopmid", params={"quarter": "Q3"})
    for _ in range(500):
        if "draft_report" in call_log.calls:
            break
        await asyncio.sleep(0.01)
    else:
        pytest.fail("the run never reached the gate")
    await asyncio.sleep(0.02)  # let the decider's own turn actually start awaiting the model

    assert await service.stop(session, records=records) is True
    blocked.set()

    attested = _attested(await _events("wf.stopmid", records))
    assert attested[-1].outcome == "stopped"
    assert "approve" not in attested[-1].gates_approved
    assert "approve" not in attested[-1].gates_orchestrated


# --- a turn nobody attends is never offered the verbs that ask -----------------------------


async def _scheduled(workflow_id: str) -> str:
    """Stands in for the schedule act, so a test can say whether it ran."""
    return f"scheduled {workflow_id}"


def _gated_agent(respond: FunctionModel, fired: list[str]) -> Agent[Runtime, str]:
    """An agent holding the one act that always waits for the person, behind the real gate."""

    async def schedule_workflow(workflow_id: str) -> str:
        """Stand up a schedule for a workflow. Needs the person's approval."""
        fired.append(workflow_id)
        return await _scheduled(workflow_id)

    toolset: FunctionToolset[Runtime] = FunctionToolset()
    toolset.add_function(schedule_workflow, name="schedule_workflow", takes_ctx=False)
    return Agent(respond, deps_type=Runtime, output_type=str, toolsets=[toolset], capabilities=[ConsoleApprovals()])


async def test_a_decider_turn_is_never_offered_the_verb_that_waits_for_the_person() -> None:
    """The gate decider answers with nobody in the room, so an act that asks is not in front of it.

    Decided twice, once plainly and once from inside a plugin that CAN answer: the ACP process holds
    no pool, so a turn there that awaits a run inline decides its gates inside that plugin's own
    block. The decider must say for itself that nobody attends it, rather than inherit the answer.
    """
    offered: list[list[str]] = []
    fired: list[str] = []

    async def respond(messages: Sequence[ModelMessage], info: AgentInfo) -> ModelResponse:
        del messages
        offered.append([tool.name for tool in info.function_tools])
        return _final_result(approve=True)

    session = Runtime(registry=StepRegistry(), library=cast("Any", None), agent=_gated_agent(FunctionModel(respond), fired), session_log=NoSessionLog(), state=a_throwaway_state())
    decide = orchestrator_decider(session, APool(SessionPool(session)))

    plain = await decide("approve", CHARGE, "the draft")
    with answerable():
        inside = await decide("approve", CHARGE, "the draft")

    assert plain.approve is True, "the turn answers rather than raising"
    assert inside.approve is True, "and answers the same way inside a plugin that can ask"
    assert len(offered) == 2, "both turns reached the model"
    assert not any("schedule_workflow" in names for names in offered), "an unattended turn is not given a verb that asks"
    assert fired == []


async def test_a_decider_turn_binds_the_sessions_own_workspace_root(tmp_path: Path) -> None:
    """The library's `search` reads this from `ACTIVE_WORKSPACE_ROOT`, not `Runtime.workspace` directly."""
    seen: list[Path | None] = []

    async def respond(messages: Sequence[ModelMessage], info: AgentInfo) -> ModelResponse:
        del messages, info
        seen.append(session_state.ACTIVE_WORKSPACE_ROOT.get())
        return _final_result(approve=True)

    workspace = open_workspace(tmp_path)
    session = Runtime(registry=StepRegistry(), library=cast("Any", None), agent=_agent(FunctionModel(respond)), workspace=workspace, session_log=NoSessionLog(), state=a_state(tmp_path))
    decide = orchestrator_decider(session, APool(SessionPool(session)))

    await decide("approve", CHARGE, "the draft")

    assert seen == [tmp_path]


# --- honest records: the legacy shapes still parse -----------------------------------------


def test_a_legacy_archive_record_without_gates_orchestrated_still_parses() -> None:
    legacy = RunArchiveRecord.model_validate_json(
        '{"run_id": "r1", "workflow_id": "wf", "outcome": "completed", "started_at": "t", '
        '"ended_at": "t", "runner": "app:harness", "session_id": "", "params": {}, '
        '"spec": {}, "values": {}, "events": []}'
    )
    assert legacy.gates_orchestrated == ()


# --- the three-way mode is the only gate switch a launch or a schedule takes --------------------


@pytest.mark.parametrize("model", [RunRequest, ScheduleIn, SchedulePatch, ScheduleOut, Schedule, ScheduleUpdate])
def test_gate_mode_is_the_only_gate_switch(model: type[BaseModel]) -> None:
    assert "gate_mode" in model.model_fields
    assert "auto_approve_gates" not in model.model_fields


def test_an_active_run_reads_automode_off_its_gate_mode() -> None:
    assert "auto_approve" not in {field.name for field in fields(ActiveRun)}
