"""A model response that a content filter cut ends the turn at once, and `content_filter_message` names the filter that fired.

`ContentFilterGuard` makes every response that finishes on `content_filter`, whatever text it carries, streamed or not,
and every prompt Azure's content filter refuses with HTTP 400, end the turn with pydantic-ai's `ContentFilterError`, so
a filtered response never reaches output validation. `content_filter_message` reads the error back as one sentence
naming each category the filter flagged. `FilterReportingResponsesModel` keeps the content filter results an Azure
Responses API response carries in the response's provider details, under `content_filters`, where that sentence reads
them.
"""

from __future__ import annotations

import json

from contextlib import asynccontextmanager
from dataclasses import replace
from typing import TYPE_CHECKING, override

from pydantic import BaseModel, ConfigDict, JsonValue, TypeAdapter, ValidationError
from pydantic_ai.exceptions import ContentFilterError, ModelHTTPError
from pydantic_ai.messages import ModelMessagesTypeAdapter, ModelResponse
from pydantic_ai.models.openai import OpenAIResponsesModel
from pydantic_ai.models.wrapper import WrapperModel

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator, Mapping
    from typing import Any

    from openai.types import responses
    from pydantic_ai import RunContext
    from pydantic_ai.messages import ModelMessage
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


class _InnerError(BaseModel):
    """The `innererror` of Azure's HTTP 400 for a refused prompt, holding what each filter category found in it."""

    model_config = ConfigDict(extra="ignore")

    content_filter_result: dict[str, JsonValue] | None = None


class _RefusalError(BaseModel):
    """The error of an HTTP 400 body: Azure's code `content_filter` when its content filter refused the prompt."""

    model_config = ConfigDict(extra="ignore")

    code: str | None = None
    innererror: _InnerError | None = None
    content_filters: list[JsonValue] | None = None


_FILTER_LIST = TypeAdapter(list[_ContentFilter])
_CATEGORY_RESULTS = TypeAdapter(dict[str, _CategoryResult | None])
_REFUSAL_STATUS = 400
"""The HTTP status Azure refuses a prompt with when its content filter flags it."""
_REFUSAL_CODE = "content_filter"
"""The error code of that refusal."""


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
    """A model whose every response that finishes on `content_filter` ends an agent's turn with `ContentFilterError`.

    A streamed request raises the error once its body exits. A request returns the filtered response with its parts
    removed, so the agent records its usage like any other response's and then raises the error itself, and a caller
    outside an agent run gets that empty response. Either way the filtered response is never validated as output and
    no output retry is sent for it. A prompt Azure's content filter refuses with HTTP 400 raises the error at once. Every
    other response and every other error passes through unchanged. `content_filter_message` names the categories.
    """

    async def request(
        self, messages: list[ModelMessage], model_settings: ModelSettings | None, model_request_parameters: ModelRequestParameters
    ) -> ModelResponse:
        """The wrapped model's response, its parts removed when a content filter cut it.

        Args:
            messages: The messages to send.
            model_settings: The settings for this request.
            model_request_parameters: The request parameters.

        Returns:
            The wrapped model's response, with no parts when it finishes on `content_filter`.

        Raises:
            ContentFilterError: When Azure's content filter refused the prompt with HTTP 400.
        """
        try:
            response = await self.wrapped.request(messages, model_settings, model_request_parameters)
        except ModelHTTPError as error:
            _raise_if_refused(error)
            raise
        if response.finish_reason == "content_filter":
            return replace(response, parts=[])
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
            ContentFilterError: When the finished response finishes on `content_filter`, or when Azure's content filter
                refused the prompt with HTTP 400.
        """
        try:
            async with self.wrapped.request_stream(messages, model_settings, model_request_parameters, run_context) as response_stream:
                yield response_stream
        except ModelHTTPError as error:
            _raise_if_refused(error)
            raise
        _raise_if_filtered(response_stream.get())


def content_filter_message(error: ContentFilterError) -> str:
    """One sentence saying a content filter cut a response, why the provider says so, and what each filter category found.

    The sentence is read from the response the error's body carries, so it is the same whether the guard raised the error
    or the agent did. An error whose body holds no single response answers its own message.

    Args:
        error: The error a filtered response or a refused prompt ended the turn with.

    Returns:
        The sentence, on one line.
    """
    try:
        messages = ModelMessagesTypeAdapter.validate_json(error.body or "")
    except ValidationError:
        return error.message
    if len(messages) != 1 or not isinstance(response := messages[0], ModelResponse):
        return error.message
    return _content_filter_message(response)


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


def _raise_if_refused(error: ModelHTTPError) -> None:
    """Raise `ContentFilterError` from `error` when it is Azure's HTTP 400 for a prompt its content filter refused."""
    if error.status_code != _REFUSAL_STATUS:
        return
    body = error.body
    if isinstance(body, dict) and isinstance(nested := body.get("error"), dict):
        body = nested
    try:
        refusal = _RefusalError.model_validate(body)
    except ValidationError:
        return
    if refusal.code != _REFUSAL_CODE:
        return
    details: dict[str, JsonValue] = {"finish_reason": _REFUSAL_CODE}
    if refusal.innererror is not None and refusal.innererror.content_filter_result is not None:
        details[_RESULT_KEY] = refusal.innererror.content_filter_result
    if refusal.content_filters is not None:
        details[_FILTERS_KEY] = refusal.content_filters
    response = ModelResponse(parts=[], model_name=error.model_name, finish_reason="content_filter", provider_details=details)
    raise _filter_error(response) from error


def _raise_if_filtered(response: ModelResponse) -> None:
    """Raise `ContentFilterError` carrying the whole response when `response` finishes on `content_filter`."""
    if response.finish_reason == "content_filter":
        raise _filter_error(response)


def _filter_error(response: ModelResponse) -> ContentFilterError:
    """The `ContentFilterError` for `response`: the sentence naming its filter results, and the whole response as its body."""
    return ContentFilterError(_content_filter_message(response), body=ModelMessagesTypeAdapter.dump_json([response]).decode())


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
