"""Mid-turn steering: the inbox reaches the model inside the running turn, through upstream's queue.

Real `Agent` + `FunctionModel` runs, never a stubbed context: what is asserted is which request
actually carried the steered text, the same fact the person's chat depends on.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, cast

from core.ai.workflows.contracts import StepRegistry
from pydantic_ai import Agent
from pydantic_ai.messages import ModelResponse, SystemPromptPart, TextPart, ToolCallPart, UserPromptPart
from pydantic_ai.models.function import FunctionModel
from pydantic_ai.toolsets import FunctionToolset

from harness.features.authoring.steering import STEER_PREAMBLE, SteerDelivery
from harness.session import NoSessionLog, Runtime

if TYPE_CHECKING:
    from collections.abc import Sequence

    from pydantic_ai.messages import ModelMessage
    from pydantic_ai.models.function import AgentInfo

from tests.support.state import a_throwaway_state


def _session() -> Runtime:
    return Runtime(registry=StepRegistry(), library=cast("Any", None), agent=cast("Any", None), session_log=NoSessionLog(), state=a_throwaway_state())


def _texts(messages: Sequence[ModelMessage]) -> list[str]:
    return [str(part.content) for message in messages for part in message.parts if isinstance(part, UserPromptPart | SystemPromptPart)]


async def test_a_waiting_steer_is_delivered_into_the_turns_next_model_request() -> None:
    session = _session()
    session.steer_inbox.append("also check the tests")
    requests: list[list[str]] = []
    toolset = FunctionToolset[Runtime]()
    toolset.add_function(lambda: "ok", takes_ctx=False, name="look")

    def answer(messages: Sequence[ModelMessage], info: AgentInfo) -> ModelResponse:
        del info
        requests.append(_texts(messages))
        if len(requests) == 1:
            return ModelResponse(parts=[ToolCallPart(tool_name="look", args={})])
        return ModelResponse(parts=[TextPart("done")])

    agent = Agent(FunctionModel(answer), deps_type=Runtime, output_type=str, toolsets=[toolset], capabilities=[SteerDelivery()])

    result = await agent.run("start", deps=session)

    assert result.output == "done"
    assert not any("also check the tests" in text for text in requests[0]), "the first request was already being built"
    assert any("also check the tests" in text for text in requests[1])
    assert any(STEER_PREAMBLE in text for text in requests[1]), "the model is told it arrived mid-turn"
    assert session.steer_inbox == []


async def test_a_steer_arriving_on_the_final_answer_redirects_the_run_instead_of_ending_it() -> None:
    session = _session()
    requests: list[list[str]] = []

    def answer(messages: Sequence[ModelMessage], info: AgentInfo) -> ModelResponse:
        del info
        requests.append(_texts(messages))
        if len(requests) == 1:
            # Arrives while the final answer streams: nothing drained it before this response.
            session.steer_inbox.append("one more thing")
            return ModelResponse(parts=[TextPart("all done")])
        return ModelResponse(parts=[TextPart("and the one more thing, done too")])

    agent = Agent(FunctionModel(answer), deps_type=Runtime, output_type=str, capabilities=[SteerDelivery()])

    result = await agent.run("start", deps=session)

    assert result.output == "and the one more thing, done too"
    assert any("one more thing" in text for text in requests[1])
    assert session.steer_inbox == []


async def test_an_empty_inbox_steers_nothing() -> None:
    session = _session()
    requests: list[list[str]] = []

    def answer(messages: Sequence[ModelMessage], info: AgentInfo) -> ModelResponse:
        del info
        requests.append(_texts(messages))
        return ModelResponse(parts=[TextPart("done")])

    agent = Agent(FunctionModel(answer), deps_type=Runtime, output_type=str, capabilities=[SteerDelivery()])

    result = await agent.run("start", deps=session)

    assert result.output == "done"
    assert len(requests) == 1
    assert not any(STEER_PREAMBLE in text for text in requests[0])
