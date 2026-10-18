"""One model response's own cost, tapped as it happens and carried onto the chat wire.

A `data-step` chunk names one model response: the tool calls it made, its four token counts, and
when it landed. `StepUsageTap` wraps any model and records that shape for every response that
finishes cleanly, in the order the model made them.
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from datetime import datetime
from typing import TYPE_CHECKING

from pydantic import BaseModel
from pydantic_ai.messages import ToolCallPart
from pydantic_ai.models.wrapper import WrapperModel
from pydantic_ai.ui.vercel_ai.response_types import DataChunk

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator
    from typing import Any

    from pydantic_ai import RunContext
    from pydantic_ai.messages import ModelMessage, ModelResponse
    from pydantic_ai.models import Model, ModelRequestParameters, StreamedResponse
    from pydantic_ai.settings import ModelSettings


class StepUsageTokens(BaseModel):
    """The four token counts a model response reports, as the chat wire carries them."""

    input_tokens: int
    output_tokens: int
    cache_read_tokens: int
    cache_write_tokens: int


class StepUsage(BaseModel):
    """One model response, read down to what its `data-step` chunk shows for it."""

    tool_call_ids: list[str]
    """Every tool call this response made, in the order it made them. Empty for a text-only response."""
    usage: StepUsageTokens
    timestamp: datetime


def step_usage_of(response: ModelResponse) -> StepUsage:
    """`response` read into the shape its `data-step` chunk carries.

    Args:
        response: The model response.

    Returns:
        Its tool call ids, token counts and timestamp.
    """
    return StepUsage(
        tool_call_ids=[part.tool_call_id for part in response.parts if isinstance(part, ToolCallPart)],
        usage=StepUsageTokens(
            input_tokens=response.usage.input_tokens,
            output_tokens=response.usage.output_tokens,
            cache_read_tokens=response.usage.cache_read_tokens,
            cache_write_tokens=response.usage.cache_write_tokens,
        ),
        timestamp=response.timestamp,
    )


def to_chunk(step: StepUsage) -> DataChunk:
    """`step` as the `data-step` chunk the chat stream carries.

    Args:
        step: The model response's usage.

    Returns:
        The chunk.
    """
    return DataChunk(type="data-step", data=step.model_dump(mode="json"))


class StepUsageTap(WrapperModel):
    """A model that records every response of the model it wraps into a list, in the order they finish.

    Every request and streamed request passes through unchanged. Only a response that reaches a
    clean exit is recorded. A request that raises, or a streamed one abandoned before its body
    finishes, records nothing.
    """

    def __init__(self, wrapped: Model, sink: list[StepUsage]) -> None:
        """Wrap a model.

        Args:
            wrapped: The model every request goes to.
            sink: The list each finished response is appended to.
        """
        super().__init__(wrapped)
        self._sink = sink

    async def request(
        self,
        messages: list[ModelMessage],
        model_settings: ModelSettings | None,
        model_request_parameters: ModelRequestParameters,
    ) -> ModelResponse:
        """The wrapped model's answer to this request, recorded into the sink before it is returned.

        Args:
            messages: The messages to send.
            model_settings: The settings for this request.
            model_request_parameters: The request parameters.

        Returns:
            The wrapped model's response.
        """
        response = await self.wrapped.request(messages, model_settings, model_request_parameters)
        self._sink.append(step_usage_of(response))
        return response

    @asynccontextmanager
    async def request_stream(
        self,
        messages: list[ModelMessage],
        model_settings: ModelSettings | None,
        model_request_parameters: ModelRequestParameters,
        run_context: RunContext[Any] | None = None,
    ) -> AsyncGenerator[StreamedResponse]:
        """The wrapped model's streamed answer, recorded into the sink once the caller's body exits cleanly.

        Args:
            messages: The messages to send.
            model_settings: The settings for this request.
            model_request_parameters: The request parameters.
            run_context: The run's context, when there is one.

        Yields:
            The wrapped model's streamed response.
        """
        async with self.wrapped.request_stream(messages, model_settings, model_request_parameters, run_context) as response_stream:
            yield response_stream
        self._sink.append(step_usage_of(response_stream.get()))
