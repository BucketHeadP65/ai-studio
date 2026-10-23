"""A capability that asks the model, by name, to answer again when it reasons and then answers with only whitespace.

A response with a thinking part, no tool call and only whitespace text is caught before output
validation runs. The model is sent retry feedback that names the mistake.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from pydantic_ai.capabilities import AbstractCapability
from pydantic_ai.exceptions import ModelRetry
from pydantic_ai.messages import TextPart, ThinkingPart, ToolCallPart
from pydantic_ai.tools import AgentDepsT

if TYPE_CHECKING:
    from pydantic_ai.messages import ModelResponse
    from pydantic_ai.models import ModelRequestContext
    from pydantic_ai.tools import RunContext

DEFAULT_MESSAGE = "Put the answer in your reply text, not in your reasoning."


def _is_blank_after_thinking(response: ModelResponse) -> bool:
    """`response` reasoned (a thinking part) but answered with only whitespace text and no tool call."""
    has_thinking = False
    has_tool_call = False
    text_parts: list[TextPart] = []
    for part in response.parts:
        if isinstance(part, ThinkingPart):
            has_thinking = True
        elif isinstance(part, ToolCallPart):
            has_tool_call = True
        elif isinstance(part, TextPart):
            text_parts.append(part)
    return has_thinking and not has_tool_call and bool(text_parts) and all(not part.content.strip() for part in text_parts)


@dataclass
class EmptyAnswerRetry(AbstractCapability[AgentDepsT]):
    """Ask the model to try again, by name, when it reasons and then answers with only whitespace.

    It runs before the output validator, so the model never gets generic "invalid JSON" retry
    feedback for this failure. Each retry spends one turn of the agent's output retry budget.
    """

    message: str = DEFAULT_MESSAGE
    """The retry feedback sent back to the model when the blank-after-thinking pattern is detected."""

    async def after_model_request(
        self,
        ctx: RunContext[AgentDepsT],  # noqa: ARG002 (required by `AbstractCapability.after_model_request`'s signature)
        *,
        request_context: ModelRequestContext,  # noqa: ARG002 (required by `AbstractCapability.after_model_request`'s signature)
        response: ModelResponse,
    ) -> ModelResponse:
        """Reject a blank-after-thinking response with `message`, and pass every other response through.

        Args:
            ctx: The run's context, which is not read.
            request_context: The request's context, which is not read.
            response: The model response.

        Returns:
            `response`, unchanged.

        Raises:
            ModelRetry: The response reasoned and then answered with only whitespace and no tool call.
        """
        if _is_blank_after_thinking(response):
            raise ModelRetry(self.message)
        return response
