"""A single workflow step's model gets the same four output retries a map item's model gets, and no fifth."""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from core.ai.workflows.contracts import prepare
from core.ai.workflows.domain.vocabulary import Budget
from pydantic_ai.exceptions import UnexpectedModelBehavior
from pydantic_ai.messages import ModelMessage, ModelRequest, ModelResponse, RetryPromptPart, ToolCallPart

from tests.support.environment import defined_step, spec_for, world_over
from tests.support.streaming import streaming_function_model

if TYPE_CHECKING:
    from pathlib import Path

    from pydantic_ai.models.function import AgentInfo


async def test_a_single_step_survives_four_invalid_answers(tmp_path: Path) -> None:
    def respond(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        sent_back = sum(
            isinstance(part, RetryPromptPart) for message in messages if isinstance(message, ModelRequest) for part in message.parts
        )
        answer = {"note": "valid"} if sent_back >= 4 else {}
        return ModelResponse(parts=[ToolCallPart(info.output_tools[0].name, answer)])

    registry = await world_over(streaming_function_model(respond), state=tmp_path)
    step = defined_step(tools=()).model_copy(update={"budget": Budget(requests=8)})
    ready = prepare(spec_for(step), registry)
    assert ready.report.ok, ready.report.issues
    implementation = ready.registry.get("dig")
    assert implementation is not None

    result = await implementation.function(question="what is this base's purpose")

    assert "valid" in str(result)


async def test_a_single_step_fails_on_its_fifth_invalid_answer(tmp_path: Path) -> None:
    calls: list[int] = []

    def respond(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        calls.append(len(messages))
        return ModelResponse(parts=[ToolCallPart(info.output_tools[0].name, {})])

    registry = await world_over(streaming_function_model(respond), state=tmp_path)
    step = defined_step(tools=()).model_copy(update={"budget": Budget(requests=8)})
    ready = prepare(spec_for(step), registry)
    assert ready.report.ok, ready.report.issues
    implementation = ready.registry.get("dig")
    assert implementation is not None

    with pytest.raises(UnexpectedModelBehavior, match=r"Exceeded maximum output retries \(4\)"):
        await implementation.function(question="what is this base's purpose")

    assert len(calls) == 5, "the first answer and the four answers the budget sends back, then the step fails"
