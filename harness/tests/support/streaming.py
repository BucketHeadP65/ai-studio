"""`FunctionModel` doubles that answer streamed requests too, from the same non-streaming reply.

Every agentic step's agent now runs with `event_stream_handler` mounted (the step-conversation
relay in `harness.features.running.step_stream`), so `pydantic_ai`'s graph always calls
`Model.request_stream`, never `Model.request`, for a step's own model. `FunctionModel`'s
`request_stream` needs its own `stream_function`; a bare `function=` double built before the relay
existed answers `request` only.

`streaming_function_model` wraps such a double so it answers `request_stream` too, yielding its
whole response as one chunk -- text, or the tool calls it made, never both, matching
`FunctionModel`'s own streamed-response contract. It estimates `RequestUsage` from the streamed
content, the same as a real `FunctionModel` stream, so it never reports `cache_read_tokens` or
`cache_write_tokens`. `usage_preserving_function_model` is for a double a usage-reading capability
(`WarnOnCacheBusts`) watches: it carries `function`'s own `ModelResponse.usage` through unchanged.

`ScriptedStreamModel` is the same double with an ending a test chooses: a failure entering the
provider context, standing in for a request that never reached headers, or a failure part way
through the events, standing in for a stream that died mid-flight. Both leave the response
incomplete, which is what a caller pricing a stream has to be able to tell apart from a stream that
ran to the end.

`submit` is the request half of the same wire: the one body shape every caller of the chat path
posts, so no test spells it twice.
"""

from __future__ import annotations

import inspect

from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from pydantic_ai.messages import TextPart, ToolCallPart
from pydantic_ai.models import StreamedResponse
from pydantic_ai.models.function import AgentInfo, DeltaToolCall, FunctionModel

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator, AsyncIterator
    from datetime import datetime

    from pydantic_ai.messages import ModelMessage, ModelResponse, ModelResponseStreamEvent
    from pydantic_ai.models import ModelRequestParameters
    from pydantic_ai.models.function import DeltaToolCalls, FunctionDef
    from pydantic_ai.settings import ModelSettings
    from pydantic_ai.tools import RunContext


def submit(text: str, **extras: object) -> dict[str, object]:
    """One submit-message body, the shape every caller of the chat path sends."""
    return {
        "trigger": "submit-message",
        "id": "req-1",
        "messages": [{"id": "m-1", "role": "user", "parts": [{"type": "text", "text": text}]}],
        **extras,
    }


def streaming_function_model(function: FunctionDef) -> FunctionModel:
    """A `FunctionModel` whose `stream_function` replays `function`'s own answer, once, as one chunk."""

    async def stream_function(messages: list[ModelMessage], info: AgentInfo) -> AsyncIterator[str | DeltaToolCalls]:
        response = function(messages, info)
        if inspect.isawaitable(response):
            response = await response
        calls = {
            index: DeltaToolCall(name=part.tool_name, json_args=part.args_as_json_str(), tool_call_id=part.tool_call_id)
            for index, part in enumerate(response.parts)
            if isinstance(part, ToolCallPart)
        }
        if calls:
            yield calls
            return
        yield "".join(part.content for part in response.parts if isinstance(part, TextPart))

    return FunctionModel(function=function, stream_function=stream_function)


@dataclass
class _ReplayedStream(StreamedResponse):
    """A `StreamedResponse` that replays one already-built `ModelResponse` verbatim, usage included.

    `FunctionModel`'s own streamed path only ever estimates `RequestUsage` from streamed content
    length, so `cache_read_tokens`/`cache_write_tokens` -- and any other usage a test double sets
    explicitly -- never survive it. This one carries `response.usage` through unchanged.
    """

    _response: ModelResponse = field(repr=False)
    _iteration_error: BaseException | None = field(default=None, repr=False)

    async def _get_event_iterator(self) -> AsyncIterator[ModelResponseStreamEvent]:
        for index, part in enumerate(self._response.parts):
            yield self._parts_manager.handle_part(vendor_part_id=index, part=part)
            if self._iteration_error is not None:
                raise self._iteration_error
        self._usage = self._response.usage

    @property
    def model_name(self) -> str:
        return self._response.model_name or "usage-preserving-replay"

    @property
    def provider_name(self) -> str | None:
        return self._response.provider_name

    @property
    def provider_url(self) -> str | None:
        return self._response.provider_url

    @property
    def timestamp(self) -> datetime:
        return self._response.timestamp

    async def close_stream(self) -> None:
        """A replay holds no connection, so cancelling one has nothing to tear down."""
        return


class UsagePreservingFunctionModel(FunctionModel):
    """A `FunctionModel` whose streamed requests carry `function`'s own `ModelResponse.usage` verbatim.

    For a double a monitor like `WarnOnCacheBusts` reads `cache_read_tokens`/`cache_write_tokens`
    off of -- `streaming_function_model`'s estimate-only usage never reports those, so a test
    asserting on a cache-usage capability needs this one instead.
    """

    async def _answer(
        self, messages: list[ModelMessage], model_settings: ModelSettings | None, model_request_parameters: ModelRequestParameters
    ) -> ModelResponse:
        """The response `function` gives for this request, stamped with this model's own name."""
        assert self.function is not None
        model_settings, model_request_parameters = self.prepare_request(model_settings, model_request_parameters)
        info = AgentInfo(
            function_tools=model_request_parameters.declared_function_tools,
            allow_text_output=model_request_parameters.allow_text_output,
            output_tools=model_request_parameters.output_tools,
            model_settings=model_settings,
            model_request_parameters=model_request_parameters,
            instructions=None,
        )
        response = self.function(messages, info)
        if inspect.isawaitable(response):
            response = await response
        response.model_name = self.model_name
        return response

    @asynccontextmanager
    async def request_stream(
        self,
        messages: list[ModelMessage],
        model_settings: ModelSettings | None,
        model_request_parameters: ModelRequestParameters,
        run_context: RunContext[object] | None = None,
    ) -> AsyncGenerator[StreamedResponse]:
        response = await self._answer(messages, model_settings, model_request_parameters)
        yield _ReplayedStream(model_request_parameters=model_request_parameters, _response=response)


class ScriptedStreamModel(UsagePreservingFunctionModel):
    """A streamed double that ends where a test says it ends.

    `entry_error` is raised instead of entering the provider context at all. `iteration_error` is
    raised after the stream's first event, leaving the response incomplete with whatever it had
    built by then.
    """

    def __init__(self, function: FunctionDef, *, entry_error: BaseException | None = None, iteration_error: BaseException | None = None) -> None:
        super().__init__(function=function)
        self.entry_error = entry_error
        self.iteration_error = iteration_error

    @asynccontextmanager
    async def request_stream(
        self,
        messages: list[ModelMessage],
        model_settings: ModelSettings | None,
        model_request_parameters: ModelRequestParameters,
        run_context: RunContext[object] | None = None,
    ) -> AsyncGenerator[StreamedResponse]:
        if self.entry_error is not None:
            raise self.entry_error
        response = await self._answer(messages, model_settings, model_request_parameters)
        yield _ReplayedStream(model_request_parameters=model_request_parameters, _response=response, _iteration_error=self.iteration_error)


def usage_preserving_function_model(function: FunctionDef) -> FunctionModel:
    """A `FunctionModel` that answers both `request` and `request_stream` from `function`, usage included."""
    return UsagePreservingFunctionModel(function=function)
