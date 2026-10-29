"""A chat-started run is a run the whole app can see, and the turn that starts it does not wait.

`run_workflow` answers the moment the run is going: the graph runs in the background, the session
fields read exactly as a button launch's would (waiting, running, paused), and when the run
settles, one headless turn of the session's own agent carries the outcome back to the
conversation. `session.active.settlement` is the book-closer -- evidence record, archive, session log --
awaited here so every after-the-run assertion reads a closed book.
"""

from __future__ import annotations

import asyncio
import json

from typing import TYPE_CHECKING, Any, cast

from core.ai.workflows.contracts import FromNode, FromParameter, ParameterSpec, RunEvidence, RunNode, StepRegistry, Workflow
from pydantic_ai import Agent
from pydantic_ai.messages import ModelResponse, TextPart, UserPromptPart
from pydantic_ai.models.function import FunctionModel
from pydantic_ai.models.test import TestModel
from pydantic_ai.tools import RunContext
from pydantic_ai.usage import RunUsage

from harness.features.running import service
from harness.features.running.controller import _values
from harness.features.running.domain.progress import GateVerdict
from harness.features.running.infrastructure.run_log import load_waiting
from harness.features.running.records import RunCollaborators
from harness.features.running.tools import check_runs, run_workflow
from harness.session import NoSessionLog, Runtime, SessionPool, run_log_path
from harness.settings import RunSettings
from tests.conftest import CallLog, SalesData, register_quarterly_steps
from tests.support.controllers import a_client_of
from tests.support.fakes.broadcaster import FakeBroadcaster
from tests.support.fakes.sessions import APool
from tests.support.running import a_running_controller
from tests.support.runtime import EmptyLibrary, a_runtime
from tests.support.state import a_state, records_under

if TYPE_CHECKING:
    from collections.abc import Sequence
    from pathlib import Path

    from pydantic_ai.messages import ModelMessage
    from pydantic_ai.models.function import AgentInfo


def _session(registry: StepRegistry, tmp_path: Path, agent: object | None = None) -> Runtime:
    return Runtime(registry=registry, library=cast("Any", None), agent=cast("Any", agent if agent is not None else object()), session_log=NoSessionLog(), state=a_state(tmp_path))


def _capturing_agent() -> tuple[Agent[Runtime, str], list[str]]:
    """An agent that answers once and remembers the user prompt text it was asked."""
    prompts: list[str] = []

    def respond(messages: Sequence[ModelMessage], info: AgentInfo) -> ModelResponse:
        del info
        part = messages[-1].parts[-1]
        prompts.append(str(part.content) if isinstance(part, UserPromptPart) else "")
        return ModelResponse(parts=[TextPart(content="the run settled; told the person")])

    return Agent(FunctionModel(respond), deps_type=Runtime, output_type=str), prompts


async def _delivered(prompts: list[str]) -> str:
    """Wait out the outcome turn the settlement fires; the prompt it carried."""
    for _ in range(500):
        if prompts:
            return prompts[0]
        await asyncio.sleep(0.01)
    msg = "the settled run's outcome turn never fired"
    raise AssertionError(msg)


async def _settled(session: Runtime) -> str:
    """Await the run's own book-closer and the outcome turn it hands back. Every after-the-run assertion reads a closed book."""
    settlement = session.active.settlement
    assert settlement is not None, "run_workflow left no settlement task behind"
    answer = await settlement
    delivery = session.active.delivery
    assert delivery is not None, "run_workflow left no delivery task behind"
    await delivery
    return answer


def _ctx(session: Runtime) -> RunContext[Runtime]:
    return RunContext(deps=session, model=TestModel(), usage=RunUsage())


async def _waiting_decision(session: Runtime) -> asyncio.Future[GateVerdict]:
    """See the gate out; the future the person's click resolves."""
    for _ in range(500):
        if session.active.decision is not None:
            return session.active.decision
        await asyncio.sleep(0.01)
    msg = "the gate never waiting"
    raise AssertionError(msg)


async def test_a_chat_run_starts_without_blocking_and_settles_into_an_outcome_turn(quarterly_report: Workflow, registry: StepRegistry, tmp_path: Path) -> None:
    """The tool answers at start; waiting it reads `waiting`; settled, the outcome turn says finished."""
    records = records_under(tmp_path)
    agent, prompts = _capturing_agent()
    session = _session(registry, tmp_path, agent)
    session.conversation.last_workflow = quarterly_report

    answer = await run_workflow(_ctx(session), "draft", {"quarter": "Q2"}, run=RunCollaborators(records=records, run_settings=RunSettings(), sessions=APool(SessionPool(session)), topics=FakeBroadcaster()))

    assert "running in the background" in answer, "the tool must answer at start, not at the end"
    decision = await _waiting_decision(session)

    assert service.run_phase(session) == "waiting", "a waiting chat run must read as waiting, like a button run"
    assert session.active.workflow_id == "draft"
    assert session.active.pending is not None
    assert session.active.progress.done, "the steps before the gate lit up and finished"

    decision.set_result(GateVerdict(approve=True))
    await _settled(session)

    assert service.run_phase(session) is None, "a finished run steps no live card"
    assert session.active.result.startswith("Finished.")
    assert "reconcile" in session.active.values, "each node's output stays readable after the run"
    body = json.loads(bytes(_values(session).body))
    assert body["workflow"] == "draft"
    assert isinstance(body["values"]["reconcile"], str)

    delivered = await _delivered(prompts)
    assert "finished" in delivered, "the outcome turn carries the finished account"

    archived = records.archive.recent(10)
    assert len(archived) == 1, "the ended run rests in the archive instead of vanishing with its session log"
    assert archived[0].outcome == "completed"
    record = records.archive.get(archived[0].run_id)
    assert record is not None
    assert "reconcile" in record.values, "the archive keeps every node's output, not just the envelope"


async def test_a_chat_run_runs_under_the_gate_mode_it_was_asked_for(quarterly_report: Workflow, registry: StepRegistry, tmp_path: Path) -> None:
    """`auto` passes every gate with nobody asked, and the evidence names those gates apart from approvals."""
    records = records_under(tmp_path)
    agent, _ = _capturing_agent()
    session = _session(registry, tmp_path, agent)
    session.conversation.last_workflow = quarterly_report
    run = RunCollaborators(records=records, run_settings=RunSettings(), sessions=APool(SessionPool(session)), topics=FakeBroadcaster())

    await run_workflow(_ctx(session), "draft", {"quarter": "Q2"}, run=run, gate_mode="auto")
    await _settled(session)

    assert session.active.gate_mode == "auto"
    assert session.active.result.startswith("Finished.")
    (evidence,) = [event for event in await records.evidence.events_for("draft") if isinstance(event, RunEvidence)]
    assert evidence.gates_auto, "the gates passed with nobody asked"
    assert evidence.gates_approved == (), "an automatic pass is never a person's approval"


async def test_whoever_started_a_chat_run_can_wait_out_its_outcome_turn(quarterly_report: Workflow, registry: StepRegistry, tmp_path: Path) -> None:
    """The delivery task is on the session, so the outcome turn ends before anyone moves on, never mid-run."""
    agent, prompts = _capturing_agent()
    session = _session(registry, tmp_path, agent)
    session.conversation.last_workflow = quarterly_report
    run = RunCollaborators(records=records_under(tmp_path), run_settings=RunSettings(), sessions=APool(SessionPool(session)), topics=FakeBroadcaster())

    await run_workflow(_ctx(session), "draft", {"quarter": "Q2"}, run=run, gate_mode="auto")
    delivery = session.active.delivery
    assert delivery is not None
    await delivery

    assert len(prompts) == 1
    assert "finished" in prompts[0], "the outcome turn ran to its end before the delivery task did"


async def test_a_chat_run_logs_the_gate_settings_and_runner_it_runs_with(
    quarterly_report: Workflow, registry: StepRegistry, tmp_path: Path
) -> None:
    """A restore of the chat run's log runs it the way the chat launched it."""
    session = _session(registry, tmp_path)
    session.conversation.last_workflow = quarterly_report
    run = RunCollaborators(
        records=records_under(tmp_path), run_settings=RunSettings(), sessions=APool(SessionPool(session)), topics=FakeBroadcaster()
    )

    await run_workflow(_ctx(session), "draft", {"quarter": "Q2"}, run=run)
    decision = await _waiting_decision(session)
    waiting = load_waiting(run_log_path(session))

    assert waiting is not None
    assert (waiting.gate_mode, waiting.gate_timeout, waiting.runner) == ("wait", session.active.gate_timeout, "app:chat")
    decision.set_result(GateVerdict(approve=True))
    await _settled(session)


async def test_a_panel_pause_parks_the_chat_run_and_the_outcome_turn_says_so(call_log: CallLog, tmp_path: Path) -> None:
    """Pausing from the run panel keeps the session log, and the outcome turn says who paused it."""
    records = records_under(tmp_path)
    registry = StepRegistry()
    register_quarterly_steps(registry, call_log, exclude=frozenset({"fetch_sales"}))
    hold = asyncio.Event()

    async def fetch_sales(quarter: str) -> SalesData:
        await hold.wait()
        return SalesData(quarter=quarter, total=1)

    registry.register(fetch_sales)
    agent, prompts = _capturing_agent()
    session = _session(registry, tmp_path, agent)
    session.conversation.last_workflow = Workflow(
        name="quarterly-report",
        version=4,
        description="Fetch and reconcile, with one fetch held open so the pause lands mid-run.",
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
    answer = await run_workflow(_ctx(session), "draft", {"quarter": "Q2"}, run=RunCollaborators(records=records, run_settings=RunSettings(), sessions=APool(SessionPool(session)), topics=FakeBroadcaster()))
    assert "running in the background" in answer
    for _ in range(500):
        if session.active.task is not None and session.active.progress.running:
            break
        await asyncio.sleep(0.01)

    assert await service.pause(session), "the chat run's task is the one the pause route cancels"
    await _settled(session)

    delivered = await _delivered(prompts)
    assert "paused by the person" in delivered, "the outcome turn says the person paused it"
    assert load_waiting(run_log_path(session)) is not None, "the session log is the waiting run a resume picks up"
    events = await records.evidence.events_for("draft")
    assert [event for event in events if isinstance(event, RunEvidence)] == [], "a paused run has not ended"


class SpecShelf(EmptyLibrary):
    """A library holding one spec, answered for whichever workflow id is asked for."""

    def __init__(self, spec: Workflow) -> None:
        super().__init__()
        self.spec = spec

    async def fetch_workflow(self, workflow_id: str) -> Workflow:
        """The one spec this shelf holds."""
        del workflow_id
        return self.spec


async def test_a_bare_launch_opens_its_own_runner_session(quarterly_report: Workflow, registry: StepRegistry, tmp_path: Path) -> None:
    """POST /api/run never joins the process session: the run rides a fresh runner session and says which."""
    default = a_runtime(registry=registry, library=SpecShelf(quarterly_report))
    pool = SessionPool(default)

    async with a_client_of(a_running_controller(pool, tmp_path)) as client:
        response = await client.post("/api/run", json={"workflow_id": "draft", "arguments": {"quarter": "Q2"}})

    body = response.json()
    assert body["ok"] is True, body
    run_session = next(held for held in pool.held_sessions() if held.session_id == body["session_id"])
    assert run_session is not default
    assert run_session.session_kind == "runner"
    assert default.active.task is None, "the process session was never touched"

    decision = await _waiting_decision(run_session)
    decision.set_result(GateVerdict(approve=True))
    task = run_session.active.task
    assert task is not None
    await task
    assert run_session.active.result.startswith("Finished.")


async def test_check_runs_names_the_waiting_gate_and_goes_quiet_after(quarterly_report: Workflow, registry: StepRegistry, tmp_path: Path) -> None:
    """The agent's own look at the pool: the waiting gate is named, a finished pool reads quiet."""
    records = records_under(tmp_path)
    session = _session(registry, tmp_path)
    session.conversation.last_workflow = quarterly_report
    sessions = APool(SessionPool(session))
    await run_workflow(_ctx(session), "draft", {"quarter": "Q2"}, run=RunCollaborators(records=records, run_settings=RunSettings(), sessions=sessions, topics=FakeBroadcaster()))
    decision = await _waiting_decision(session)

    report = await check_runs(_ctx(session), sessions=sessions)
    assert "this session" in report
    assert "waiting at gate 'approve'" in report

    decision.set_result(GateVerdict(approve=True))
    await _settled(session)
    assert await check_runs(_ctx(session), sessions=sessions) == "Nothing is running in any session right now."
