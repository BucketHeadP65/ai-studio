"""A defined step's own budget line, and a brought judge's `cannot_judge` wait with the reviewer's note.

`run_defined_step` tells the model what it may spend before it starts, in the last line of its own
prompt; a judge that cannot decide escalates through the ordinary human-gate path, and the value
waiting for a person carries the judge's own words ahead of what it reviewed.
"""

from __future__ import annotations

import asyncio

from typing import TYPE_CHECKING, Any, cast

import pytest

from core.ai.workflows.contracts import Briefing, FromNode, JudgeGate, RunNode, Shape, Workflow, prepare
from core.ai.workflows.domain.vocabulary import VERDICTS, Budget
from pydantic_ai import Agent
from pydantic_ai.exceptions import UsageLimitExceeded
from pydantic_ai.messages import ModelResponse, ToolCallPart, UserPromptPart

from harness.features.running import service
from harness.features.running.domain.progress import GateVerdict
from harness.features.running.feedback import orchestrator_decider
from harness.session import NoSessionLog, Runtime, SessionPool
from tests.support.environment import spec_for, world_over
from tests.support.fakes.broadcaster import FakeBroadcaster
from tests.support.fakes.sessions import APool
from tests.support.state import a_state, records_under
from tests.support.streaming import streaming_function_model

if TYPE_CHECKING:
    from collections.abc import Sequence
    from pathlib import Path

    from pydantic_ai.messages import ModelMessage
    from pydantic_ai.models.function import AgentInfo

_VERDICT_SHAPE = Shape(
    json_schema={
        "type": "object",
        "properties": {"outcome": {"enum": list(VERDICTS)}, "feedback": {"type": "string"}},
        "required": ["outcome", "feedback"],
    }
)


def _prompt_of(messages: Sequence[ModelMessage]) -> str:
    """The user prompt text the model's last request carried, or `""` when its last part is not one."""
    part = messages[-1].parts[-1]
    return str(part.content) if isinstance(part, UserPromptPart) else ""


def _task(*, budget: Budget | None, tools: tuple[str, ...] = ()) -> Briefing:
    return Briefing(
        name="dig",
        instructions="Find what the knowledge base says about the question and report it in a single note.",
        inputs={"question": Shape(json_schema={"type": "string"})},
        output=Shape(json_schema={"type": "object", "properties": {"note": {"type": "string"}}, "required": ["note"]}),
        tools=tools,
        budget=budget,
    )


# --- the budget line ------------------------------------------------------------------------


async def test_a_steps_own_budget_is_named_at_the_end_of_its_prompt() -> None:
    prompts: list[str] = []

    def respond(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        prompts.append(_prompt_of(messages))
        return ModelResponse(parts=[ToolCallPart(info.output_tools[0].name, {"note": "ok"})])

    environment = await world_over(streaming_function_model(respond))
    ready = prepare(spec_for(_task(budget=Budget(requests=3, tool_calls=2))), environment)
    assert ready.report.ok, ready.report.issues
    implementation = ready.registry.get("dig")
    assert implementation is not None

    await implementation.function(question="what does the knowledge base say?")

    assert prompts
    assert prompts[-1].endswith(
        "You have 3 model requests and 2 tool calls for this task. When they are nearly gone, answer with what you have."
    )


async def test_a_tooled_step_with_no_budget_gets_the_defined_step_default() -> None:
    prompts: list[str] = []

    def respond(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        prompts.append(_prompt_of(messages))
        return ModelResponse(parts=[ToolCallPart(info.output_tools[0].name, {"note": "ok"})])

    environment = await world_over(streaming_function_model(respond))
    ready = prepare(spec_for(_task(budget=None, tools=("search",))), environment)
    assert ready.report.ok, ready.report.issues
    implementation = ready.registry.get("dig")
    assert implementation is not None

    await implementation.function(question="what does the knowledge base say?")

    assert prompts
    assert prompts[-1].endswith("You have 24 model requests for this task. When they are nearly gone, answer with what you have.")


async def test_a_step_that_never_answers_hits_its_own_requests_budget() -> None:
    def respond(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        del messages, info
        return ModelResponse(parts=[ToolCallPart("run_code", {"code": "await search(query='x')\n1"})])

    environment = await world_over(streaming_function_model(respond))
    ready = prepare(spec_for(_task(budget=Budget(requests=3), tools=("search",))), environment)
    assert ready.report.ok, ready.report.issues
    implementation = ready.registry.get("dig")
    assert implementation is not None

    with pytest.raises(UsageLimitExceeded, match="request_limit of 3"):
        await implementation.function(question="what does the knowledge base say?")


# --- the cannot_judge wait, with the reviewer's note ----------------------------------------


def _cannot_judge_respond(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
    del messages
    return ModelResponse(parts=[ToolCallPart(info.output_tools[0].name, {"outcome": "cannot_judge", "feedback": "The drafts are empty."})])


async def _empty_draft() -> str:
    return ""


def _judged_spec() -> Workflow:
    """One drafting step producing an empty draft, and a brought judge that reviews it."""
    task = Briefing(
        name="check",
        instructions="Check the drafts and decide whether they are ready to publish, in full detail and with care.",
        inputs={"drafts": Shape(json_schema={"type": "string"})},
        output=_VERDICT_SHAPE,
    )
    return Workflow(
        name="judged-drafts",
        version=1,
        description="One drafting step, and a brought judge that reviews what it produced.",
        inline=(task,),
        nodes=(
            RunNode(id="drafted", work="draft"),
            JudgeGate(id="check", work="check", reviews=FromNode(node="drafted"), approved_when="outcome", rejected_to="drafted"),
        ),
        output=FromNode(node="check"),
    )


async def _wait_for_park(session: Runtime) -> None:
    for _ in range(500):
        if session.active.pending is not None:
            return
        await asyncio.sleep(0.001)
    pytest.fail("the run never waiting")


async def test_a_cannot_judge_verdict_parks_with_the_reviewers_note(tmp_path: Path) -> None:
    environment = await world_over(streaming_function_model(_cannot_judge_respond))
    environment.register(_empty_draft, name="draft")
    session = Runtime(registry=environment, library=cast("Any", None), agent=cast("Any", None), waiting_run_file=tmp_path / "run-log.json", session_log=NoSessionLog(), state=a_state(tmp_path))

    service.launch(session, records=records_under(tmp_path), topics=FakeBroadcaster(), spec=_judged_spec(), workflow_id="wf.judge-wait", params={})
    task = session.active.task
    assert task is not None
    await _wait_for_park(session)

    pending = session.active.pending
    assert pending is not None
    assert pending.reviewing.startswith("The reviewer could not judge this and said: The drafts are empty.\n\n")
    assert pending.rejection_reruns == "drafted", "the card names the step a rejection reruns, for a judge as for a person"

    decision = session.active.decision
    assert decision is not None
    decision.set_result(GateVerdict(approve=True))
    await task

    assert "Finished" in session.active.result


async def test_the_orchestrate_mode_decider_hears_the_reviewers_note(tmp_path: Path) -> None:
    environment = await world_over(streaming_function_model(_cannot_judge_respond))
    environment.register(_empty_draft, name="draft")
    heard: list[str] = []

    def decide_respond(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        heard.append(_prompt_of(messages))
        return ModelResponse(parts=[ToolCallPart(info.output_tools[0].name, {"approve": True, "feedback": ""})])

    agent: Agent[Runtime, str] = Agent(streaming_function_model(decide_respond), deps_type=Runtime, output_type=str)
    session = Runtime(registry=environment, library=cast("Any", None), agent=agent, waiting_run_file=tmp_path / "run-log.json", session_log=NoSessionLog(), state=a_state(tmp_path))
    session.registry.decide_gate = orchestrator_decider(session, APool(SessionPool(session)))

    service.launch(session, records=records_under(tmp_path), topics=FakeBroadcaster(), spec=_judged_spec(), workflow_id="wf.judge-orchestrate", params={}, gate_mode="orchestrate")
    task = session.active.task
    assert task is not None
    await task

    assert len(heard) == 1
    assert "The reviewer could not judge it and said: The drafts are empty." in heard[0]
