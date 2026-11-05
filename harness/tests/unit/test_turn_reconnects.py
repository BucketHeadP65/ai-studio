"""What a step's line keeps when an attempt at its turn loses its model connection.

The engine performs such a turn again from the history it began with. The line keeps every broken
attempt apart from that history: the run archive still files what each attempt exchanged, each
attempt's model responses reach the run's spend rows, and a steer the broken attempt had already
taken in reaches the next attempt. Every test drives one map item through `build_environment`'s own
`run_defined_step`, with `FunctionModel` standing in for the provider.
"""

from __future__ import annotations

import asyncio
import json

from dataclasses import replace
from typing import TYPE_CHECKING, Any, cast

import httpx2
import pytest

from core.ai.workflows.contracts import (
    AgentStep,
    Briefing,
    FromNode,
    FromParameter,
    MapNode,
    Outcome,
    ParameterSpec,
    RunNode,
    Shape,
    StepRegistry,
    Workflow,
    WorkflowArguments,
    run_workflow,
)
from openai import AsyncOpenAI
from pydantic_ai.exceptions import ContentFilterError, ModelConnectionError
from pydantic_ai.messages import ModelRequest, ModelResponse, TextPart, UserPromptPart
from pydantic_ai.models.function import DeltaToolCall, FunctionModel
from pydantic_ai.models.openai import OpenAIResponsesModel
from pydantic_ai.providers.openai import OpenAIProvider
from pydantic_core import to_jsonable_python

from harness.composition import build_environment
from harness.features.running.contracts import DroppedAttempt, StepConversation
from harness.features.running.service import archive_run
from harness.session import ActiveRun, NoSessionLog, Runtime
from harness.settings import WebSearchSettings
from tests.conftest import ApproveAll
from tests.support.environment import fake_engram
from tests.support.fakes.spend import FakeSpendLog
from tests.support.state import a_state, records_under
from tests.support.tiers import tiers_under

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Sequence
    from pathlib import Path

    from pydantic_ai.messages import ModelMessage
    from pydantic_ai.models.function import AgentInfo, DeltaToolCalls

    from harness.features.running.contracts import RunRecords
    from harness.features.running.spend import SpendRow

ITEM_SCHEMA = {"type": "object", "title": "Digest", "properties": {"title": {"type": "string"}}, "required": ["title"]}
WORKING = "Working on it."
HALF_SAID = "half an answer"
STEER = "Use the short title."

DIGEST = Briefing(
    name="digest",
    instructions="Read the item and answer with its title, exactly as the item gives it.",
    inputs={"item": Shape(json_schema=ITEM_SCHEMA)},
    output=Shape(json_schema=ITEM_SCHEMA),
)
SPEC = Workflow(
    name="digest-each",
    version=1,
    description="One worker per item answers with the item's title.",
    parameters={"items": ParameterSpec(description="items to digest", json_schema={"type": "array", "items": ITEM_SCHEMA})},
    inline=(DIGEST,),
    nodes=(MapNode(id="each_item", turns=("digest",), over=FromParameter(parameter="items"), each="item", on_error="skip"),),
    output=FromNode(node="each_item"),
)
ITEMS: WorkflowArguments = {"items": [{"title": "quantum computing"}]}


class FiledRows(FakeSpendLog):
    """A spend ledger that keeps every row a step files, in order."""

    def __init__(self) -> None:
        super().__init__()
        self.rows: list[SpendRow] = []

    def record(self, run_id: str, workflow_id: str, rows: Sequence[SpendRow]) -> None:
        del run_id, workflow_id
        self.rows.extend(rows)


async def _no_wait(seconds: float) -> None:
    del seconds


def _answer(info: AgentInfo) -> DeltaToolCalls:
    """The item's title, through the turn's own output tool."""
    return {0: DeltaToolCall(name=info.output_tools[0].name, json_args=json.dumps({"title": "quantum computing"}))}


def _broken() -> ModelConnectionError:
    model_name = "test-model"
    return ModelConnectionError(model_name, "Connection error.", phase="read", in_stream=True)


def _the_items_line(active: ActiveRun) -> StepConversation:
    (line,) = [held for (node, _), held in active.conversations.conversations.items() if node == "each_item"]
    return line


def _active_run() -> ActiveRun:
    active = ActiveRun()
    active.run_id, active.workflow_id = "run-1", "wf.digest-each"
    return active


async def _environment(model: FunctionModel, active: ActiveRun, tmp_path: Path, records: RunRecords) -> StepRegistry:
    return await build_environment(
        model,
        engram=fake_engram(),
        active=active,
        external_agents=dict,
        session_log=NoSessionLog(),
        custom=tmp_path / "custom",
        records=records,
        web_search=WebSearchSettings(),
        tiers=tiers_under(tmp_path),
    )


def _kinds(messages: Sequence[ModelMessage]) -> list[str]:
    return [message.kind for message in messages]


def _user_prompts(messages: Sequence[ModelMessage]) -> list[str]:
    return [str(part.content) for message in messages for part in message.parts if isinstance(part, UserPromptPart)]


async def test_an_item_that_lost_all_three_attempts_archives_what_each_attempt_exchanged(tmp_path: Path) -> None:
    """Each attempt answers in text, is told to use its output tool, and loses its connection part way through the next answer."""
    calls: list[list[ModelMessage]] = []

    async def stream(messages: list[ModelMessage], info: AgentInfo) -> AsyncIterator[str | DeltaToolCalls]:
        del info
        calls.append(list(messages))
        if not any(isinstance(message, ModelResponse) for message in messages):
            yield WORKING
            return
        yield HALF_SAID
        raise _broken()

    active = _active_run()
    records = replace(records_under(tmp_path), spend=FiledRows())
    environment = await _environment(FunctionModel(stream_function=stream), active, tmp_path, records)

    result = await run_workflow(SPEC, environment, params=ITEMS, approval=ApproveAll(), sleep=_no_wait)

    assert result == []
    assert len(calls) == 6, "two requests in each of three attempts"
    line = _the_items_line(active)
    assert line.messages == [], "no attempt settled, and none is fed to a later one"
    exchanged = active.conversations.archived()[next(iter(active.conversations.conversations))]
    assert _kinds(exchanged) == ["request", "response", "request", "response"] * 3
    assert len(_user_prompts(exchanged)) == 3
    texts = [part.content for message in exchanged for part in message.parts if isinstance(part, TextPart)]
    assert texts == [WORKING, HALF_SAID] * 3, "each attempt's answer, and what it said before the break"

    session = Runtime(registry=StepRegistry(), library=cast("Any", None), agent=cast("Any", None), session_log=NoSessionLog(), state=a_state(tmp_path))
    session.active.workflow_id, session.active.started_at = "wf.digest-each", "2026-10-30T00:00:00+00:00"
    archive_run(
        session,
        archive=records.archive,
        outcome="completed",
        failure="",
        runner="app:harness",
        spec=SPEC,
        params=dict(ITEMS),
        values={},
        conversations=active.conversations.archived(),
        gates_approved=(),
        gates_rejected={},
        gates_auto=(),
        result=Outcome(status="unknown", summary="no result declared"),
    )
    record = records.archive.get(records.archive.recent(1)[0].run_id)
    assert record is not None
    ((key, filed),) = record.conversations.items()
    assert key.startswith("each_item[")
    assert [entry["kind"] for entry in filed] == ["request", "response", "request", "response"] * 3


async def test_a_dropped_attempts_responses_reach_the_spend_rows(tmp_path: Path) -> None:
    """The first attempt's answer and its broken answer are filed although that attempt failed, and so is the second attempt's."""
    calls: list[list[ModelMessage]] = []

    async def stream(messages: list[ModelMessage], info: AgentInfo) -> AsyncIterator[str | DeltaToolCalls]:
        calls.append(list(messages))
        if len(calls) == 1:
            yield WORKING
            return
        if len(calls) == 2:
            yield HALF_SAID
            raise _broken()
        yield _answer(info)

    active = _active_run()
    ledger = FiledRows()
    records = replace(records_under(tmp_path), spend=ledger)
    environment = await _environment(FunctionModel(stream_function=stream), active, tmp_path, records)

    result = await run_workflow(SPEC, environment, params=ITEMS, approval=ApproveAll(), sleep=_no_wait)

    assert isinstance(result, list)
    assert len(result) == 1, "the item settled on its second attempt"
    assert len(calls) == 3
    assert [row.step for row in ledger.rows] == ["digest"] * 3, "the dropped attempt's two responses, then the answer"
    assert all(row.output_tokens > 0 for row in ledger.rows)


async def test_a_steer_the_broken_attempt_took_in_reaches_the_next_attempt(tmp_path: Path) -> None:
    """The steer lands while the first attempt answers. Its next request takes it in, then breaks."""
    active = _active_run()
    calls: list[list[ModelMessage]] = []

    async def stream(messages: list[ModelMessage], info: AgentInfo) -> AsyncIterator[str | DeltaToolCalls]:
        calls.append(list(messages))
        if len(calls) == 1:
            _the_items_line(active).steer_inbox.append(STEER)
            yield WORKING
            return
        if len(calls) == 2:
            yield HALF_SAID
            raise _broken()
        yield _answer(info)

    environment = await _environment(FunctionModel(stream_function=stream), active, tmp_path, replace(records_under(tmp_path), spend=FiledRows()))

    await run_workflow(SPEC, environment, params=ITEMS, approval=ApproveAll(), sleep=_no_wait)

    def steered(messages: Sequence[ModelMessage]) -> bool:
        return any(isinstance(message, ModelRequest) and STEER in _user_prompts([message]) for message in messages)

    assert len(calls) == 3
    assert steered(calls[1]), "the broken attempt took the steer in"
    assert not any(isinstance(message, ModelResponse) for message in calls[2]), "the next attempt starts from the turn's own start"
    assert steered(calls[2]), "and the steer reaches it again"
    assert _the_items_line(active).steer_inbox == []


async def test_a_turn_a_content_filter_ended_files_its_responses_in_the_spend_rows(tmp_path: Path) -> None:
    """The item fails once its second answer is cut. Both answers were billed, so both reach the run's spend rows."""
    calls: list[list[ModelMessage]] = []

    async def stream(messages: list[ModelMessage], info: AgentInfo) -> AsyncIterator[str | DeltaToolCalls]:
        del info
        calls.append(list(messages))
        if len(calls) == 1:
            yield WORKING
            return
        yield HALF_SAID
        msg = "Content filter triggered. Finish reason: 'content_filter'"
        raise ContentFilterError(msg)

    active = _active_run()
    ledger = FiledRows()
    records = replace(records_under(tmp_path), spend=ledger)
    environment = await _environment(FunctionModel(stream_function=stream), active, tmp_path, records)

    result = await run_workflow(SPEC, environment, params=ITEMS, approval=ApproveAll(), sleep=_no_wait)

    assert result == [], "the item failed and was skipped"
    assert len(calls) == 2, "a content filter ends the turn at once"
    assert [row.step for row in ledger.rows] == ["digest"] * 2, "the answer refused as text, then the answer the filter cut"
    assert all(row.output_tokens > 0 for row in ledger.rows)


WORKER = AgentStep(
    name="digest",
    agent={"instructions": "Read the item and answer with its title, exactly as the item gives it."},
    inputs={"item": Shape(json_schema=ITEM_SCHEMA)},
    output=Shape(json_schema=ITEM_SCHEMA),
)
WORKER_SPEC = Workflow(
    name="digest-one",
    version=1,
    description="One worker step answers with the item's title.",
    parameters={"item": ParameterSpec(description="the item", json_schema=ITEM_SCHEMA)},
    inline=(WORKER,),
    nodes=(RunNode(id="digest", work="digest", inputs={"item": FromParameter(parameter="item")}),),
    output=FromNode(node="digest"),
)


async def test_a_turn_stopped_mid_answer_files_what_it_was_billed_and_its_resume_files_only_its_own(tmp_path: Path) -> None:
    """A stop cancels the turn after one finished answer. The resumed turn starts from that history and files only its own."""
    calls: list[list[ModelMessage]] = []
    stopping = asyncio.Event()

    async def stream(messages: list[ModelMessage], info: AgentInfo) -> AsyncIterator[str | DeltaToolCalls]:
        calls.append(list(messages))
        if len(calls) == 1:
            yield WORKING
            return
        if len(calls) == 2:
            stopping.set()
            await asyncio.Event().wait()
        yield _answer(info)

    active = _active_run()
    ledger = FiledRows()
    records = replace(records_under(tmp_path), spend=ledger)
    environment = await _environment(FunctionModel(stream_function=stream), active, tmp_path, records)

    running = asyncio.create_task(run_workflow(WORKER_SPEC, environment, params={"item": {"title": "quantum computing"}}, approval=ApproveAll(), sleep=_no_wait))
    await asyncio.wait_for(stopping.wait(), timeout=10)
    running.cancel()
    with pytest.raises(asyncio.CancelledError):
        await running

    assert [row.step for row in ledger.rows] == ["digest"], "the finished answer was billed, and the one the stop cut before its first word was not"
    assert ledger.rows[0].output_tokens > 0
    stopped = len(ledger.rows)

    result = await run_workflow(WORKER_SPEC, environment, params={"item": {"title": "quantum computing"}}, approval=ApproveAll(), sleep=_no_wait)

    assert to_jsonable_python(result) == {"title": "quantum computing"}
    assert any(isinstance(part, TextPart) and part.content == WORKING for message in calls[2] for part in message.parts), "the resume starts from the stopped turn's history"
    assert [row.step for row in ledger.rows[stopped:]] == ["digest"], "the resumed turn files only its own answer"


class _StreamCutBeforeItsLastEvent(httpx2.AsyncByteStream):
    """A Responses API stream that sends its opening events and one text delta, then waits for an end that never comes.

    Azure and OpenRouter report usage only in the last event, so a stream cut here leaves an answer with no usage at all.
    """

    def __init__(self, streaming: asyncio.Event) -> None:
        self.streaming = streaming

    async def __aiter__(self) -> AsyncIterator[bytes]:
        item: dict[str, object] = {"id": "msg_1", "type": "message", "role": "assistant", "status": "in_progress", "content": []}
        located: dict[str, object] = {"item_id": "msg_1", "output_index": 0, "content_index": 0}
        response: dict[str, object] = {
            "id": "resp_1", "object": "response", "created_at": 1, "model": "some-deployment", "output": [],
            "parallel_tool_calls": True, "tool_choice": "auto", "tools": [], "status": "in_progress",
        }
        events: list[dict[str, object]] = [
            {"type": "response.created", "response": response},
            {"type": "response.output_item.added", "output_index": 0, "item": item},
            {"type": "response.content_part.added", **located, "part": {"type": "output_text", "text": "", "annotations": []}},
            {"type": "response.output_text.delta", **located, "delta": HALF_SAID, "logprobs": []},
        ]
        for number, event in enumerate(events):
            yield f"event: {event['type']}\ndata: {json.dumps({**event, 'sequence_number': number})}\n\n".encode()
        self.streaming.set()
        await asyncio.Event().wait()


class _AnswersByStreamCutBeforeItsLastEvent(httpx2.AsyncBaseTransport):
    """Answers every Responses API request with a stream that never sends its last event."""

    def __init__(self) -> None:
        self.streaming = asyncio.Event()

    async def handle_async_request(self, request: httpx2.Request) -> httpx2.Response:
        return httpx2.Response(200, headers={"content-type": "text/event-stream"}, stream=_StreamCutBeforeItsLastEvent(self.streaming), request=request)


async def test_a_stop_during_a_streamed_answer_its_provider_had_not_yet_measured_files_one_unpriced_row(tmp_path: Path) -> None:
    """The provider would have reported the answer's usage in the last event, which the stop cut off. It was still billed."""
    transport = _AnswersByStreamCutBeforeItsLastEvent()
    client = AsyncOpenAI(api_key="unused", base_url="https://responses.example/v1/", http_client=httpx2.AsyncClient(transport=transport), max_retries=0)
    model = OpenAIResponsesModel("some-deployment", provider=OpenAIProvider(openai_client=client))
    active = _active_run()
    ledger = FiledRows()
    records = replace(records_under(tmp_path), spend=ledger)
    environment = await build_environment(model, engram=fake_engram(), active=active, external_agents=dict, session_log=NoSessionLog(), custom=tmp_path / "custom", records=records, web_search=WebSearchSettings(), tiers=tiers_under(tmp_path))

    running = asyncio.create_task(run_workflow(WORKER_SPEC, environment, params={"item": {"title": "quantum computing"}}, approval=ApproveAll(), sleep=_no_wait))
    await asyncio.wait_for(transport.streaming.wait(), timeout=10)
    await asyncio.sleep(0.1)
    running.cancel()
    with pytest.raises(asyncio.CancelledError):
        await running

    (row,) = ledger.rows
    assert (row.step, row.input_tokens, row.output_tokens, row.cost) == ("digest", 0, 0, None), "filed with no figure, so it reads as unpriced"


def _asked(prompt: str) -> ModelRequest:
    return ModelRequest(parts=[UserPromptPart(prompt)])


def _said(text: str) -> ModelResponse:
    return ModelResponse(parts=[TextPart(text)])


def test_a_dropped_attempt_is_placed_ahead_of_the_settled_attempt_that_asked_the_same() -> None:
    first, one, second, two = _asked("turn one"), _said("one"), _asked("turn two"), _said("two")
    broken, half = _asked("turn two"), _said(HALF_SAID)
    line = StepConversation(step=DIGEST, output_type=dict)
    line.messages = [first, one, second, two]
    line.dropped_attempts = [DroppedAttempt(messages=(broken, half), prompts_before=1)]

    assert line.exchanged == [first, one, broken, half, second, two]


def test_a_dropped_attempt_no_settled_attempt_asked_again_comes_after_everything_before_it() -> None:
    """A rerun whose every attempt broke follows the run before it, which asked something else."""
    earlier, answered = _asked("the first run"), _said("first")
    attempts = [DroppedAttempt(messages=(_asked("the rerun"), _said(HALF_SAID)), prompts_before=0) for _ in range(2)]
    line = StepConversation(step=DIGEST, output_type=dict)
    line.messages = [earlier, answered]
    line.dropped_attempts = attempts

    assert line.exchanged == [earlier, answered, *attempts[0].messages, *attempts[1].messages]
