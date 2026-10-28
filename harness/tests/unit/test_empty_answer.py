"""`EmptyAnswerRetry`.

A blank-after-thinking response is retried with a message naming the mistake; a response carrying
a tool call or real text is left alone.

Driven through `Agent.run()` with `FunctionModel` rather than the hook directly: what matters is
the retry feedback the model actually sees, and the pattern (thinking + blank text + no tool call)
is defined over a whole `ModelResponse`, not a single part.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from core.ai.empty_answer import DEFAULT_MESSAGE, EmptyAnswerRetry
from pydantic_ai import Agent
from pydantic_ai.messages import ModelResponse, RetryFeedbackPart, TextPart, ThinkingPart, ToolCallPart, UserPromptPart
from pydantic_ai.models.function import FunctionModel

if TYPE_CHECKING:
    from collections.abc import Sequence

    from pydantic_ai.messages import ModelMessage
    from pydantic_ai.models.function import AgentInfo


def _retry_feedback(messages: Sequence[ModelMessage]) -> RetryFeedbackPart | None:
    """The first `RetryFeedbackPart` a run's history holds, or `None`."""
    for message in messages:
        for part in message.parts:
            if isinstance(part, RetryFeedbackPart):
                return part
    return None


def _sent_the_named_mistake(messages: Sequence[ModelMessage]) -> bool:
    """Whether a request the model was sent carries `DEFAULT_MESSAGE`, which reaches it as a fenced user prompt."""
    return any(
        isinstance(part, UserPromptPart) and DEFAULT_MESSAGE in str(part.content) for message in messages for part in message.parts
    )


async def test_a_blank_after_thinking_reply_is_retried_with_the_named_mistake() -> None:
    def function(messages: Sequence[ModelMessage], info: AgentInfo) -> ModelResponse:
        del info
        if _sent_the_named_mistake(messages):
            return ModelResponse(parts=[TextPart("orange")])
        return ModelResponse(parts=[ThinkingPart("The user wants the word orange."), TextPart("   ")])

    agent = Agent(FunctionModel(function), capabilities=[EmptyAnswerRetry()], retries={"output": 2})

    result = await agent.run("say the word orange and nothing else")

    assert result.output == "orange"
    feedback = _retry_feedback(result.all_messages())
    assert feedback is not None
    assert (feedback.content, feedback.retry.cause) == (DEFAULT_MESSAGE, "model_retry")


async def test_a_tool_call_alongside_blank_text_is_not_treated_as_an_empty_answer() -> None:
    def probe() -> str:
        return "probed"

    def function(messages: Sequence[ModelMessage], info: AgentInfo) -> ModelResponse:
        del info
        for message in messages:
            for part in message.parts:
                if part.part_kind == "tool-return":
                    return ModelResponse(parts=[TextPart("orange")])
        return ModelResponse(parts=[ThinkingPart("checking first"), ToolCallPart("probe", {}), TextPart("   ")])

    agent = Agent(FunctionModel(function), capabilities=[EmptyAnswerRetry()], tools=[probe], retries={"output": 2})

    result = await agent.run("call probe then answer")

    assert result.output == "orange"
    assert _retry_feedback(result.all_messages()) is None


async def test_a_non_blank_reply_after_thinking_is_not_retried() -> None:
    def function(messages: Sequence[ModelMessage], info: AgentInfo) -> ModelResponse:
        del info, messages
        return ModelResponse(parts=[ThinkingPart("straightforward"), TextPart("orange")])

    agent = Agent(FunctionModel(function), capabilities=[EmptyAnswerRetry()])

    result = await agent.run("say the word orange and nothing else")

    assert result.output == "orange"
    assert _retry_feedback(result.all_messages()) is None
