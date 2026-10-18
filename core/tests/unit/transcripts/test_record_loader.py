"""The record loader against scripted rows: one step per stored step, ordinals from the entries, tools and durations beside."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from pydantic_ai.messages import ModelMessage, ModelRequest, ModelResponse, TextPart, ToolCallPart, ToolReturnPart, UserPromptPart
from pydantic_ai.models import ModelRequestParameters
from pydantic_ai.tools import ToolDefinition
from pydantic_core import to_jsonable_python

from core.ai.history.contracts import RecordedStep
from core.ai.history.conversation_record import STAMP_FIELDS
from core.ai.transcripts.contracts import RecordRunsOfConversation, RecordTranscriptLoader, RunSummary, Span, record_source
from core.ai.transcripts.infrastructure.record import _step
from tests.support.fakes.database import FakeDatabase

AT = datetime(2026, 9, 4, 12, 0, tzinfo=UTC)
CONVERSATION = "0f8c1d2e-1111-4222-8333-444455556666"
RESULT = "sunny and mild"
CLEARED = "[tool result cleared]"
RUN_ROW: dict[str, object] = {
    "run_id": "run-a",
    "conversation_id": CONVERSATION,
    "parent_run_id": None,
    "agent_name": None,
    "started_at": AT,
    "metadata": {},
    "registration_id": None,
}
STEP_ROWS: list[dict[str, object]] = [
    {"seq": 0, "step_index": 1, "at": AT, "state": "complete", "entries": [0, 0, 0, 1, 0, 0, 2, 0, 0]},
    {"seq": 1, "step_index": 2, "at": AT, "state": "complete", "entries": [0, 0, 0, 1, 0, 0, 2, 1, 0, 3, 0, 0]},
]
"""Two steps: the result enters in form 0 at the first, the second holds it in form 1 (cleared) and adds the answer."""
EVENT_ROWS: list[dict[str, object]] = [
    {
        "run_id": "run-a",
        "seq": 0,
        "kind": "tool_call_started",
        "step_index": 1,
        "at": AT,
        "tool_call_id": "call_1",
        "tool_name": "lookup_city",
        "error": None,
        "metadata": {},
    },
    {
        "run_id": "run-a",
        "seq": 1,
        "kind": "tool_call_completed",
        "step_index": 1,
        "at": AT + timedelta(minutes=1),
        "tool_call_id": "call_1",
        "tool_name": "lookup_city",
        "error": None,
        "metadata": {},
    },
]
TOOL = ToolDefinition(name="lookup_city", description="One city by name.", parameters_json_schema={"type": "object"})
PARAMETER_ROWS: list[dict[str, object]] = [
    {
        "step_index": 1,
        "parameters": {
            "model": "test-model",
            "provider": "test",
            "model_settings": None,
            "request_parameters": to_jsonable_python(ModelRequestParameters(function_tools=[TOOL])),
        },
    }
]


def question() -> ModelRequest:
    return ModelRequest(parts=[UserPromptPart(content="What is the weather in Paris?", timestamp=AT)])


def call() -> ModelResponse:
    return ModelResponse(parts=[ToolCallPart(tool_name="lookup_city", args={"name": "Paris"}, tool_call_id="call_1")], timestamp=AT)


def result(content: str) -> ModelRequest:
    return ModelRequest(parts=[ToolReturnPart(tool_name="lookup_city", content=content, tool_call_id="call_1", timestamp=AT)])


def answer() -> ModelResponse:
    return ModelResponse(parts=[TextPart(content="It is sunny in Paris.")], timestamp=AT)


def returned(message: ModelMessage) -> str:
    """The content of the tool return a message carries first."""
    part = message.parts[0]
    assert isinstance(part, ToolReturnPart)
    return str(part.content)


def piece(ordinal: int, form: int, message: ModelMessage) -> dict[str, object]:
    """One message as the record hands a step's entry back: its form and its stamps apart."""
    rendered = to_jsonable_python(message)
    stamps = {name: rendered[name] for name in STAMP_FIELDS if name in rendered}
    content = {name: value for name, value in rendered.items() if name not in STAMP_FIELDS}
    return {"ordinal": ordinal, "form": form, "version": 0, "content": content, "stamps": stamps}


FIRST_STEP_PIECES = [piece(0, 0, question()), piece(1, 0, call()), piece(2, 0, result(RESULT))]
SECOND_STEP_PIECES = [piece(0, 0, question()), piece(1, 0, call()), piece(2, 1, result(CLEARED)), piece(3, 0, answer())]


def a_recorded_run() -> FakeDatabase:
    """The reads in the order the loader runs them: the run, its steps, each step's messages, its events, its parameters."""
    return FakeDatabase([[RUN_ROW], STEP_ROWS, FIRST_STEP_PIECES, SECOND_STEP_PIECES, EVENT_ROWS, PARAMETER_ROWS])


async def test_the_source_names_the_run_in_the_record() -> None:
    transcript = await RecordTranscriptLoader(a_recorded_run())("run-a")

    assert transcript.source == "record:run-a"


async def test_one_step_per_stored_step_with_the_ordinals_its_entries_name() -> None:
    transcript = await RecordTranscriptLoader(a_recorded_run())("run-a")

    assert [step.ordinals for step in transcript.steps] == [(0, 1, 2), (0, 1, 2, 3)]
    assert returned(transcript.steps[1].messages[2]) == CLEARED


async def test_recorded_reads_the_result_in_the_form_the_first_step_held() -> None:
    transcript = await RecordTranscriptLoader(a_recorded_run())("run-a")

    recorded = list(transcript.recorded())

    assert len(recorded) == 4
    assert recorded[2][0] == Span(step=0, message=2, part=0)
    assert returned(recorded[2][1]) == RESULT


async def test_durations_come_from_the_runs_events() -> None:
    transcript = await RecordTranscriptLoader(a_recorded_run())("run-a")

    assert transcript.durations_ms == {"call_1": 60_000}


async def test_a_step_carries_the_tools_recorded_for_its_request() -> None:
    transcript = await RecordTranscriptLoader(a_recorded_run())("run-a")

    first, second = transcript.steps

    assert first.parameters is not None
    assert [tool.name for tool in first.parameters.function_tools] == ["lookup_city"]
    assert second.parameters is None


async def test_an_unknown_run_is_refused() -> None:
    with pytest.raises(LookupError, match="run-z"):
        await RecordTranscriptLoader(FakeDatabase([[]]))("run-z")


def test_record_source_names_a_run() -> None:
    assert record_source("r1") == "record:r1"


def test_a_step_carries_the_model_the_recorded_request_went_to() -> None:
    recorded = RecordedStep(
        seq=0, step_index=0, at=AT, state="complete", messages=[ModelRequest(parts=[UserPromptPart(content="hi", timestamp=AT)])],
        ordinals=(0,),
    )
    sent_with = {0: {"request_parameters": {"function_tools": [], "output_tools": [], "output_mode": "text"}, "model": "z-ai/glm-5.3"}}

    step = _step(recorded, sent_with)

    assert step.model == "z-ai/glm-5.3"
    assert _step(recorded, {}).model is None


def test_a_run_summary_ends_nowhere_unless_told() -> None:
    summary = RunSummary(source="record:r", agent=None, started_at=AT, first_prompt="", last_event_type="no events")

    assert summary.ended_at is None


@pytest.mark.parametrize("source", ["run-1", ":run-1", "claude-code:abc"])
async def test_a_source_naming_no_record_run_answers_nothing_and_runs_no_statement(source: str) -> None:
    database = FakeDatabase()
    runs = RecordRunsOfConversation(database)

    assert await runs.before(source) == []
    assert await runs.summary_of(source) is None
    assert database.opened.executed == []
