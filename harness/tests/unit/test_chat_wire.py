"""The protocol boundary: what the body carries in, and every chunk the studio reads back out."""

from __future__ import annotations

import json

from typing import TYPE_CHECKING

from core.ai.history.contracts import StepUsage, StepUsageTap
from pydantic_ai import Agent
from pydantic_ai.messages import ModelResponse, TextPart, UserPromptPart
from pydantic_ai.models.function import FunctionModel
from pydantic_ai.ui.vercel_ai.request_types import TextUIPart, UIMessage

from harness.features.authoring.api_models import ChatRequest
from harness.features.authoring.internal_contracts import Turn
from harness.features.authoring.wire import SDK_VERSION, messages_of, streaming_response

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Mapping, Sequence

    from pydantic import JsonValue
    from pydantic_ai.messages import ModelMessage
    from pydantic_ai.models.function import AgentInfo
    from starlette.responses import StreamingResponse

SETTLED: Mapping[str, JsonValue] = {"message": "a draft", "draft": {}, "issues": [], "valid": False, "graph": None}


def _body(text: str) -> ChatRequest:
    """One submit-message request, the shape both chat surfaces send."""
    return ChatRequest(id="req-1", messages=[UIMessage(id="m-1", role="user", parts=[TextUIPart(text=text)])])


def _model() -> FunctionModel:
    """A model that answers one line, streamed as the wire draws it."""

    def function(messages: Sequence[ModelMessage], info: AgentInfo) -> ModelResponse:
        del messages, info
        return ModelResponse(parts=[TextPart("ok")])

    async def stream_function(messages: Sequence[ModelMessage], info: AgentInfo) -> AsyncIterator[str]:
        del messages, info
        yield "ok"

    return FunctionModel(function=function, stream_function=stream_function)


def _answering() -> Agent[None, str]:
    """An agent over that model."""
    return Agent(_model())


async def _kinds(response: StreamingResponse) -> list[str]:
    """Every chunk type the response carries, in order, the closing frame dropped."""
    frames = [str(frame) async for frame in response.body_iterator]
    return [json.loads(frame.removeprefix("data: "))["type"] for frame in frames if frame.strip() != "data: [DONE]"]


def test_the_body_carries_the_new_message_and_the_studios_own_fields() -> None:
    """The request's messages become pydantic-ai messages, and the rail's extras ride beside them."""
    body = ChatRequest(id="req-1", messages=[UIMessage(id="m-1", role="user", parts=[TextUIPart(text="hello")])], tool_results=[])

    messages = messages_of(body)

    assert [part.content for message in messages for part in message.parts if isinstance(part, UserPromptPart)] == ["hello"]
    assert body.context is None
    assert body.tool_results == []


async def test_the_step_reading_rides_the_stream_and_the_settle_closes_it() -> None:
    """A real turn's own chunks, in the order the studio reads them, with the tap filling as it runs."""
    readings: list[StepUsage] = []

    async def settle() -> Mapping[str, JsonValue] | None:
        return SETTLED

    async with Agent(StepUsageTap(_model(), readings)).run_stream_events("hi") as events:
        kinds = await _kinds(streaming_response(_body("hi"), "text/event-stream", Turn(events=events, usage=readings, settle=settle)))

    assert kinds[0] == "start"
    assert "text-delta" in kinds
    # `data-settle` comes first: the run-result event dispatches the settle callback before the
    # handler that drains the readings, and the reading itself only exists once the response body
    # has closed, which is that same event.
    assert kinds[-5:] == ["data-settle", "data-step", "message-metadata", "finish-step", "finish"]
    assert not readings, "every reading the tap left is drained onto the wire"
    assert SDK_VERSION == 6


async def test_a_turn_that_settles_nothing_carries_no_settle_chunk() -> None:
    """A turn ending deferred has no reply to settle, so the studio reads none."""

    async def settle() -> Mapping[str, JsonValue] | None:
        return None

    async with _answering().run_stream_events("hi") as events:
        kinds = await _kinds(streaming_response(_body("hi"), "text/event-stream", Turn(events=events, usage=[], settle=settle)))

    assert "data-settle" not in kinds
