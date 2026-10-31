"""A response a content filter cut raises `ContentFilterError` at once and names the filter, and every other response is untouched."""

from __future__ import annotations

import json

from typing import TYPE_CHECKING

import httpx2
import pytest

from openai import AsyncOpenAI
from pydantic import BaseModel, JsonValue
from pydantic_ai import Agent, PromptedOutput
from pydantic_ai.exceptions import ContentFilterError, UnexpectedModelBehavior
from pydantic_ai.messages import ModelResponse, TextPart
from pydantic_ai.models.function import AgentInfo, FunctionModel
from pydantic_ai.models.openai import OpenAIChatModel, OpenAIResponsesModel
from pydantic_ai.providers.openai import OpenAIProvider

from core.ai.content_filter import ContentFilterGuard, FilterReportingResponsesModel
from core.ai.models import create_azure_model, create_gateway_model

if TYPE_CHECKING:
    from collections.abc import AsyncIterable

    from pydantic_ai import RunContext
    from pydantic_ai.messages import AgentStreamEvent, ModelMessage
    from pydantic_ai.models import Model

STOCK_REFUSAL = "I'm sorry, but I cannot assist with that request."
VIOLENCE: list[JsonValue] = [
    {
        "blocked": True,
        "source_type": "completion",
        "content_filter_results": {
            "violence": {"filtered": True, "severity": "medium"},
            "hate": {"filtered": False, "severity": "safe"},
            "protected_material_text": {"filtered": True, "detected": True},
            "jailbreak": {"filtered": False, "detected": False},
        },
        "content_filter_offsets": {"start_offset": 0, "end_offset": 49, "check_offset": 0},
    },
    {"blocked": False, "source_type": "prompt", "content_filter_results": {"violence": {"filtered": False, "severity": "safe"}}},
]
"""Azure's `content_filters` list for a completion the violence and protected material filters cut, and a clean prompt."""


class Verdict(BaseModel):
    """The structured answer the test agent asks for, so plain text fails output validation."""

    answer: str


class Answer(BaseModel):
    """One scripted model answer: its text and how it ends."""

    text: str
    status: str = "completed"
    incomplete_reason: str | None = None
    content_filters: list[JsonValue] | None = None


class ScriptedResponses(httpx2.AsyncBaseTransport):
    """Answers every Responses API request with the same scripted answer, streamed or whole as the request asks."""

    def __init__(self, answer: Answer) -> None:
        self.answer = answer
        self.sent = 0

    async def handle_async_request(self, request: httpx2.Request) -> httpx2.Response:
        self.sent += 1
        streamed = bool(json.loads(await request.aread()).get("stream"))
        if streamed:
            body = "".join(f"event: {event['type']}\ndata: {json.dumps(event)}\n\n" for event in _events(self.answer))
            return httpx2.Response(200, headers={"content-type": "text/event-stream"}, content=body.encode(), request=request)
        return httpx2.Response(200, json=_response(self.answer, finished=True), request=request)


def _response(answer: Answer, *, finished: bool) -> dict[str, JsonValue]:
    message: dict[str, JsonValue] = {
        "id": "msg_1",
        "type": "message",
        "role": "assistant",
        "status": "completed" if answer.status == "completed" else "incomplete",
        "content": [{"type": "output_text", "text": answer.text, "annotations": []}],
    }
    body: dict[str, JsonValue] = {
        "id": "resp_1",
        "object": "response",
        "created_at": 1,
        "model": "some-deployment",
        "output": [message] if finished else [],
        "parallel_tool_calls": True,
        "tool_choice": "auto",
        "tools": [],
        "status": answer.status if finished else "in_progress",
        "incomplete_details": {"reason": answer.incomplete_reason} if finished and answer.incomplete_reason else None,
    }
    if finished and answer.content_filters is not None:
        body["content_filters"] = answer.content_filters
    return body


def _events(answer: Answer) -> list[dict[str, JsonValue]]:
    item: dict[str, JsonValue] = {"id": "msg_1", "type": "message", "role": "assistant", "status": "in_progress", "content": []}
    part: dict[str, JsonValue] = {"type": "output_text", "text": answer.text, "annotations": []}
    located: dict[str, JsonValue] = {"item_id": "msg_1", "output_index": 0, "content_index": 0}
    terminal = "response.completed" if answer.status == "completed" else "response.incomplete"
    events: list[dict[str, JsonValue]] = [
        {"type": "response.created", "response": _response(answer, finished=False)},
        {"type": "response.output_item.added", "output_index": 0, "item": item},
        {"type": "response.content_part.added", **located, "part": {**part, "text": ""}},
        {"type": "response.output_text.delta", **located, "delta": answer.text, "logprobs": []},
        {"type": "response.output_text.done", **located, "text": answer.text, "logprobs": []},
        {"type": "response.content_part.done", **located, "part": part},
        {"type": "response.output_item.done", "output_index": 0, "item": {**item, "status": "completed", "content": [part]}},
        {"type": terminal, "response": _response(answer, finished=True)},
    ]
    return [{**event, "sequence_number": number} for number, event in enumerate(events)]


def _provider(transport: ScriptedResponses) -> OpenAIProvider:
    client = AsyncOpenAI(
        api_key="unused", base_url="https://responses.example/v1/", http_client=httpx2.AsyncClient(transport=transport), max_retries=0
    )
    return OpenAIProvider(openai_client=client)


def _responses_model(transport: ScriptedResponses) -> FilterReportingResponsesModel:
    return FilterReportingResponsesModel("some-deployment", provider=_provider(transport))


async def _ignore(_: RunContext[None], events: AsyncIterable[AgentStreamEvent]) -> None:
    async for _event in events:
        pass


async def _run(model: Model, *, streamed: bool) -> Verdict:
    agent = Agent(model, deps_type=type(None), output_type=PromptedOutput(Verdict), retries={"output": 4})
    result = await agent.run("Judge this.", event_stream_handler=_ignore if streamed else None)
    return result.output


@pytest.mark.parametrize("streamed", [True, False], ids=["streamed", "whole"])
async def test_an_answer_the_filter_cut_raises_at_once_and_sends_no_output_retry(streamed: bool) -> None:
    transport = ScriptedResponses(Answer(text=STOCK_REFUSAL, status="incomplete", incomplete_reason="content_filter"))

    with pytest.raises(ContentFilterError) as raised:
        await _run(ContentFilterGuard(_responses_model(transport)), streamed=streamed)

    assert transport.sent == 1
    assert raised.value.message.startswith("Content filter triggered. Finish reason: 'content_filter'.")
    assert STOCK_REFUSAL in (raised.value.body or "")


@pytest.mark.parametrize("streamed", [True, False], ids=["streamed", "whole"])
async def test_the_error_names_each_category_azure_flagged_on_its_first_line(streamed: bool) -> None:
    answer = Answer(text=STOCK_REFUSAL, status="incomplete", incomplete_reason="content_filter", content_filters=VIOLENCE)

    with pytest.raises(ContentFilterError) as raised:
        await _run(ContentFilterGuard(_responses_model(ScriptedResponses(answer))), streamed=streamed)

    assert str(raised.value).splitlines()[0] == (
        "Content filter triggered. Finish reason: 'content_filter'. Filter results: completion violence filtered at severity "
        "medium, completion protected_material_text detected and filtered., body:"
    )


async def test_the_error_says_when_the_response_holds_no_filter_result() -> None:
    transport = ScriptedResponses(Answer(text=STOCK_REFUSAL, status="incomplete", incomplete_reason="content_filter"))

    with pytest.raises(ContentFilterError) as raised:
        await _run(ContentFilterGuard(_responses_model(transport)), streamed=True)

    assert raised.value.message == "Content filter triggered. Finish reason: 'content_filter'. The response holds no filter result."


@pytest.mark.parametrize("streamed", [True, False], ids=["streamed", "whole"])
async def test_the_responses_model_keeps_azures_content_filters_in_the_provider_details(streamed: bool) -> None:
    answer = Answer(text='{"answer": "fine"}', content_filters=VIOLENCE)
    agent = Agent(_responses_model(ScriptedResponses(answer)), deps_type=type(None), output_type=PromptedOutput(Verdict))

    result = await agent.run("Judge this.", event_stream_handler=_ignore if streamed else None)

    response = result.all_messages()[-1]
    assert isinstance(response, ModelResponse)
    assert response.provider_details is not None
    assert response.provider_details["content_filters"] == VIOLENCE


async def test_the_result_of_a_filter_that_refused_the_request_is_named_too() -> None:
    details = {"finish_reason": "content_filter", "content_filter_result": {"self_harm": {"filtered": True, "severity": "high"}}}

    def refused(_messages: list[ModelMessage], _info: AgentInfo) -> ModelResponse:
        return ModelResponse(parts=[TextPart(STOCK_REFUSAL)], finish_reason="content_filter", provider_details=details)

    with pytest.raises(ContentFilterError) as raised:
        await _run(ContentFilterGuard(FunctionModel(refused)), streamed=False)

    assert raised.value.message.endswith("Filter results: self_harm filtered at severity high.")


async def _outcome(model: Model, *, streamed: bool) -> tuple[str, str]:
    try:
        output = await _run(model, streamed=streamed)
    except UnexpectedModelBehavior as error:
        return type(error).__name__, error.message
    return "output", output.answer


@pytest.mark.parametrize("streamed", [True, False], ids=["streamed", "whole"])
@pytest.mark.parametrize(
    "answer",
    [
        Answer(text='{"answer": "fine"}'),
        Answer(text='{"answer": "fi', status="incomplete", incomplete_reason="max_output_tokens"),
        Answer(text='{"answer": "fine"}', status="incomplete", incomplete_reason="max_output_tokens"),
    ],
    ids=["normal", "length-cut", "length-whole"],
)
async def test_every_other_answer_ends_as_it_does_without_the_guard(answer: Answer, streamed: bool) -> None:
    plain, guarded = ScriptedResponses(answer), ScriptedResponses(answer)

    expected = await _outcome(OpenAIResponsesModel("some-deployment", provider=_provider(plain)), streamed=streamed)
    actual = await _outcome(ContentFilterGuard(_responses_model(guarded)), streamed=streamed)

    assert actual == expected
    assert guarded.sent == plain.sent


def test_the_azure_factory_guards_a_responses_model_that_keeps_the_filter_results() -> None:
    model = create_azure_model("some-deployment", endpoint="https://example.openai.azure.com")

    assert isinstance(model, ContentFilterGuard)
    assert isinstance(model.wrapped, FilterReportingResponsesModel)


@pytest.mark.parametrize(("use_responses", "inner"), [(True, FilterReportingResponsesModel), (False, OpenAIChatModel)])
def test_the_gateway_factory_guards_both_wires(use_responses: bool, inner: type[Model]) -> None:
    model = create_gateway_model("some-model", api_key="unused", base_url="https://gateway.example/proxy", use_responses=use_responses)

    assert isinstance(model, ContentFilterGuard)
    assert isinstance(model.wrapped, inner)
