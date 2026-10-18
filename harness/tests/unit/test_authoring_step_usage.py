"""The usage tap: what one model response cost, and when it landed, recorded as it happens.

`StepUsageTap` wraps any model and drops one `StepUsage` into its sink for every response that
finishes cleanly -- streamed or not -- in the order the model made them. `to_chunk` is the one
place that shape becomes the `data-step` wire chunk.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING

import pytest

from core.ai.history.contracts import StepUsage, StepUsageTap, to_chunk
from pydantic_ai.messages import ModelResponse, TextPart, ToolCallPart
from pydantic_ai.models import ModelRequestParameters
from pydantic_ai.models.function import DeltaToolCall, FunctionModel
from pydantic_ai.usage import RequestUsage

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Sequence

    from pydantic_ai.messages import ModelMessage
    from pydantic_ai.models.function import AgentInfo, DeltaToolCalls

_TIMESTAMP = datetime(2026, 8, 28, 23, 4, 22, 206741, tzinfo=UTC)
_USAGE = RequestUsage(input_tokens=12615, output_tokens=51, cache_read_tokens=12608, cache_write_tokens=0)


def _response(*, tool_call_ids: Sequence[str] = (), text: str | None = None) -> ModelResponse:
    """One `ModelResponse` with a fixed usage and timestamp, so a recorded step is deterministic."""
    parts: list[ToolCallPart | TextPart] = [
        ToolCallPart(tool_name="poke", args={}, tool_call_id=tool_call_id) for tool_call_id in tool_call_ids
    ]
    if text is not None:
        parts.append(TextPart(text))
    return ModelResponse(parts=parts, usage=_USAGE, timestamp=_TIMESTAMP)


def _model(response: ModelResponse) -> FunctionModel:
    """A model that answers `response`, streamed or not, without estimating its own usage over it."""

    def function(messages: Sequence[ModelMessage], info: AgentInfo) -> ModelResponse:
        del messages, info
        return response

    async def stream_function(messages: Sequence[ModelMessage], info: AgentInfo) -> AsyncIterator[str | DeltaToolCalls]:
        del messages, info
        for part in response.parts:
            if isinstance(part, ToolCallPart):
                yield {0: DeltaToolCall(name=part.tool_name, json_args="{}", tool_call_id=part.tool_call_id)}
            elif isinstance(part, TextPart):
                yield part.content

    return FunctionModel(function=function, stream_function=stream_function)


async def test_a_non_streamed_request_records_its_response() -> None:
    sink: list[StepUsage] = []
    tap = StepUsageTap(_model(_response(text="hi")), sink)

    await tap.request([], None, ModelRequestParameters())

    assert len(sink) == 1
    assert sink[0].tool_call_ids == []
    assert sink[0].usage.input_tokens == 12615
    assert sink[0].timestamp == _TIMESTAMP


async def test_a_streamed_request_records_its_response_on_a_clean_exit() -> None:
    sink: list[StepUsage] = []
    tap = StepUsageTap(_model(_response(tool_call_ids=["call-1"])), sink)

    async with tap.request_stream([], None, ModelRequestParameters()) as stream:
        async for _ in stream:
            pass

    assert len(sink) == 1
    assert sink[0].tool_call_ids == ["call-1"]


async def _raise_inside_the_stream(tap: StepUsageTap) -> None:
    """Open `tap`'s stream and raise before its body finishes, for the clean-exit-only test."""
    async with tap.request_stream([], None, ModelRequestParameters()) as stream:
        del stream
        msg = "the caller never finished"
        raise RuntimeError(msg)


async def test_a_streamed_request_records_nothing_when_its_body_raises() -> None:
    sink: list[StepUsage] = []
    tap = StepUsageTap(_model(_response(tool_call_ids=["call-1"])), sink)

    with pytest.raises(RuntimeError, match="the caller never finished"):
        await _raise_inside_the_stream(tap)

    assert sink == []


async def test_the_recorded_step_names_every_tool_call_id_in_order() -> None:
    sink: list[StepUsage] = []
    tap = StepUsageTap(_model(_response(tool_call_ids=["call-1", "call-2"])), sink)

    await tap.request([], None, ModelRequestParameters())

    assert sink[0].tool_call_ids == ["call-1", "call-2"]


def test_to_chunk_dumps_the_wire_shape() -> None:
    step = StepUsage.model_validate(
        {
            "tool_call_ids": ["call-1"],
            "usage": {
                "input_tokens": 12615,
                "output_tokens": 51,
                "cache_read_tokens": 12608,
                "cache_write_tokens": 0,
            },
            "timestamp": _TIMESTAMP,
        }
    )

    chunk = to_chunk(step)

    assert chunk.type == "data-step"
    assert chunk.data == {
        "tool_call_ids": ["call-1"],
        "usage": {
            "input_tokens": 12615,
            "output_tokens": 51,
            "cache_read_tokens": 12608,
            "cache_write_tokens": 0,
        },
        "timestamp": "2026-08-28T23:04:22.206741Z",
    }
