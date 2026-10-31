"""A model response that a content filter cut ends the turn at once, with an error that names the filter that fired.

`ContentFilterGuard` raises pydantic-ai's `ContentFilterError` for every response that finishes on `content_filter`,
whatever text it carries, streamed or not, so a filtered response never reaches output validation.
`FilterReportingResponsesModel` keeps the content filter results an Azure Responses API response carries in the
response's provider details, under `content_filters`, where the error reads them.
"""

from __future__ import annotations

import json

from contextlib import asynccontextmanager
from typing import TYPE_CHECKING, override

from pydantic import BaseModel, ConfigDict, JsonValue, TypeAdapter, ValidationError
from pydantic_ai.exceptions import ContentFilterError
from pydantic_ai.messages import ModelMessagesTypeAdapter
from pydantic_ai.models.openai import OpenAIResponsesModel
from pydantic_ai.models.wrapper import WrapperModel

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator, Mapping
    from typing import Any

    from openai.types import responses
    from pydantic_ai import RunContext
    from pydantic_ai.messages import ModelMessage, ModelResponse
    from pydantic_ai.models import ModelRequestParameters, StreamedResponse
    from pydantic_ai.settings import ModelSettings

_FILTERS_KEY = "content_filters"
"""The provider details key that holds Azure's content filter results, as the response carried them."""
_RESULT_KEY = "content_filter_result"
"""The provider details key under which pydantic-ai keeps the result of an Azure filter that refused the request."""
_JSON = TypeAdapter[JsonValue](JsonValue)


class _CategoryResult(BaseModel):
    """What one filter category reported: whether it filtered, whether it detected, and at which severity."""

    model_config = ConfigDict(extra="ignore")

    filtered: bool | None = None
    detected: bool | None = None
    severity: str | None = None


class _ContentFilter(BaseModel):
    """One entry of Azure's `content_filters` list: the prompt or the completion, and what each category found in it."""

    model_config = ConfigDict(extra="ignore")

    blocked: bool | None = None
    source_type: str | None = None
    content_filter_results: dict[str, _CategoryResult | None] | None = None


_FILTER_LIST = TypeAdapter(list[_ContentFilter])
_CATEGORY_RESULTS = TypeAdapter(dict[str, _CategoryResult | None])


class FilterReportingResponsesModel(OpenAIResponsesModel):
    """An OpenAI Responses model that keeps a response's Azure content filter results in its provider details.

    Azure sends them as a top-level `content_filters` list the OpenAI SDK has no field for. They land under
    `content_filters` as sent, on a streamed response and on a whole one alike. A response without them gains nothing.
    """

    @override
    def _process_provider_details(self, response: responses.Response) -> dict[str, JsonValue] | None:
        """The response's `content_filters` list under the same key, or `None` when it carries none."""
        extra = response.model_extra or {}
        if _FILTERS_KEY not in extra:
            return None
        return {_FILTERS_KEY: _JSON.validate_python(extra[_FILTERS_KEY])}


class ContentFilterGuard(WrapperModel):
    """A model that raises `ContentFilterError` for every response of the model it wraps that finishes on `content_filter`.

    A request is checked when it returns, and a streamed request when its body exits, so the filtered response is
    never validated as output and no output retry is sent for it. The error's first line names the content filter
    results the response holds, or says it holds none. Every other response passes through unchanged.
    """

    async def request(
        self, messages: list[ModelMessage], model_settings: ModelSettings | None, model_request_parameters: ModelRequestParameters
    ) -> ModelResponse:
        """The wrapped model's response, unless a content filter cut it.

        Args:
            messages: The messages to send.
            model_settings: The settings for this request.
            model_request_parameters: The request parameters.

        Returns:
            The wrapped model's response.

        Raises:
            ContentFilterError: When the response finishes on `content_filter`.
        """
        response = await self.wrapped.request(messages, model_settings, model_request_parameters)
        _raise_if_filtered(response)
        return response

    @asynccontextmanager
    async def request_stream(
        self,
        messages: list[ModelMessage],
        model_settings: ModelSettings | None,
        model_request_parameters: ModelRequestParameters,
        run_context: RunContext[Any] | None = None,
    ) -> AsyncGenerator[StreamedResponse]:
        """The wrapped model's streamed response, checked once the caller's body exits.

        Args:
            messages: The messages to send.
            model_settings: The settings for this request.
            model_request_parameters: The request parameters.
            run_context: The run's context, when there is one.

        Yields:
            The wrapped model's streamed response.

        Raises:
            ContentFilterError: When the finished response finishes on `content_filter`.
        """
        async with self.wrapped.request_stream(messages, model_settings, model_request_parameters, run_context) as response_stream:
            yield response_stream
        _raise_if_filtered(response_stream.get())


def _content_filter_message(response: ModelResponse) -> str:
    """One line saying a content filter cut `response`, why the provider says so, and what each filter category found."""
    details = response.provider_details or {}
    if reason := details.get("finish_reason"):
        opening = f"Content filter triggered. Finish reason: '{reason}'."
    elif reason := details.get("block_reason"):
        opening = f"Content filter triggered. Block reason: '{reason}'."
    elif refusal := details.get("refusal"):
        opening = f"Content filter triggered. Refusal: {refusal!r}."
    else:
        opening = "Content filter triggered."
    return f"{opening} {_filter_results(details)}"


def _raise_if_filtered(response: ModelResponse) -> None:
    """Raise `ContentFilterError` carrying the whole response when `response` finishes on `content_filter`."""
    if response.finish_reason == "content_filter":
        body = ModelMessagesTypeAdapter.dump_json([response]).decode()
        raise ContentFilterError(_content_filter_message(response), body=body)


def _filter_results(details: Mapping[str, JsonValue]) -> str:
    """The sentence naming what the content filter results in `details` flagged."""
    findings: list[str] = []
    seen = False
    if _FILTERS_KEY in details:
        seen = True
        try:
            filters = _FILTER_LIST.validate_python(details[_FILTERS_KEY])
        except ValidationError:
            return f"Filter results in a shape this message cannot read: {json.dumps(details[_FILTERS_KEY])}."
        for entry in filters:
            source = entry.source_type or "unnamed source"
            flagged = _flagged(entry.content_filter_results or {})
            findings.extend(f"{source} {finding}" for finding in flagged)
            if entry.blocked and not flagged:
                findings.append(f"{source} blocked with no category flagged")
    if _RESULT_KEY in details:
        seen = True
        try:
            findings.extend(_flagged(_CATEGORY_RESULTS.validate_python(details[_RESULT_KEY])))
        except ValidationError:
            return f"Filter result in a shape this message cannot read: {json.dumps(details[_RESULT_KEY])}."
    if findings:
        return f"Filter results: {', '.join(findings)}."
    if seen:
        return "Filter results: no category flagged."
    return "The response holds no filter result."


def _flagged(results: Mapping[str, _CategoryResult | None]) -> list[str]:
    """Each category in `results` that filtered or detected, with what it did and its severity when it has one."""
    flagged: list[str] = []
    for category, result in results.items():
        if result is None or not (result.filtered or result.detected):
            continue
        actions = [action for action, happened in (("detected", result.detected), ("filtered", result.filtered)) if happened]
        severity = f" at severity {result.severity}" if result.severity else ""
        flagged.append(f"{category} {' and '.join(actions)}{severity}")
    return flagged
