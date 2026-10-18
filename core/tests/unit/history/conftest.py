"""A two-step run with one tool call, the worked example every history test reuses."""

from datetime import UTC, datetime

import pytest

from pydantic_ai.messages import ModelMessage, ModelRequest, ModelResponse, TextPart, ToolCallPart, ToolReturnPart, UserPromptPart
from pydantic_ai_harness.step_persistence import ContinuableSnapshot, SnapshotState

AT = datetime(2026, 9, 4, 12, 0, tzinfo=UTC)
INSTRUCTIONS = "Answer in one sentence."
RESULT = "Paris is sunny and mild today."
CLEARED = "[tool result cleared]"


def turn_messages(*, cleared: bool = False) -> list[ModelMessage]:
    """Four messages: the question, the tool call, the tool result, the answer."""
    question = ModelRequest(parts=[UserPromptPart(content="What is the weather in Paris?", timestamp=AT)], instructions=INSTRUCTIONS)
    call = ModelResponse(parts=[ToolCallPart(tool_name="lookup_city", args={"name": "Paris"}, tool_call_id="call_1")], timestamp=AT)
    result = ModelRequest(
        parts=[ToolReturnPart(tool_name="lookup_city", content=CLEARED if cleared else RESULT, tool_call_id="call_1", timestamp=AT)],
        instructions=INSTRUCTIONS,
    )
    answer = ModelResponse(parts=[TextPart(content="It is sunny in Paris.")], timestamp=AT)
    return [question, call, result, answer]


@pytest.fixture
def messages() -> list[ModelMessage]:
    return turn_messages()


def snapshot(
    run_id: str,
    step_index: int,
    messages: list[ModelMessage],
    *,
    state: SnapshotState = "complete",
    conversation_id: str = "conv-1",
) -> ContinuableSnapshot:
    return ContinuableSnapshot(
        run_id=run_id,
        step_index=step_index,
        messages=messages,
        conversation_id=conversation_id,
        timestamp=AT,
        state=state,
    )
