"""The record loader against a real Postgres: what the store wrote comes back as a transcript."""

from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest

from pydantic_ai.messages import ModelMessage, ModelRequest, ToolReturnPart
from pydantic_ai.models import ModelRequestParameters
from pydantic_ai.tools import ToolDefinition
from pydantic_ai_harness.step_persistence import RunRecord, StepEvent
from pydantic_core import to_jsonable_python

from core.ai.history.contracts import PostgresStepStore
from core.ai.transcripts.contracts import RecordTranscriptLoader, RunEmptyError, Span
from tests.unit.history.conftest import CLEARED, INSTRUCTIONS, RESULT, snapshot, turn_messages

AT = datetime(2026, 9, 4, 12, 0, tzinfo=UTC)
TOOL = ToolDefinition(name="lookup_city", description="One city by name.", parameters_json_schema={"type": "object"})


def returned(message: ModelMessage) -> str:
    """The content of the tool return a message carries first."""
    part = message.parts[0]
    assert isinstance(part, ToolReturnPart)
    return str(part.content)


@pytest.fixture
def store(database):
    return PostgresStepStore(database)


@pytest.fixture
async def run_id(store) -> str:
    """A two-step run: the result whole in the first step, cleared in the second, one timed call, tools recorded for step 1."""
    conversation_id = str(uuid4())
    run = "run-t-" + conversation_id
    await store.register_run(RunRecord(run_id=run, conversation_id=conversation_id, started_at=AT))
    await store.save_snapshot(snapshot(run, 1, turn_messages()[:3], conversation_id=conversation_id))
    await store.save_snapshot(snapshot(run, 2, turn_messages(cleared=True), conversation_id=conversation_id))
    await store.append_event(
        StepEvent(run_id=run, kind="tool_call_started", step_index=1, timestamp=AT, tool_call_id="call_1", tool_name="lookup_city")
    )
    await store.append_event(
        StepEvent(
            run_id=run,
            kind="tool_call_completed",
            step_index=1,
            timestamp=AT + timedelta(seconds=2),
            tool_call_id="call_1",
            tool_name="lookup_city",
        )
    )
    await store.record_request_parameters(
        run_id=run,
        step_index=1,
        parameters={
            "model": "test-model",
            "provider": "test",
            "model_settings": None,
            "request_parameters": to_jsonable_python(ModelRequestParameters(function_tools=[TOOL])),
        },
    )
    return run


async def test_every_step_comes_back_with_its_ordinals_and_recorded_reads_the_first_form(database, run_id):
    transcript = await RecordTranscriptLoader(database)(run_id)

    recorded = list(transcript.recorded())

    assert transcript.source == f"record:{run_id}"
    assert [step.ordinals for step in transcript.steps] == [(0, 1, 2), (0, 1, 2, 3)]
    assert returned(transcript.steps[1].messages[2]) == CLEARED
    assert recorded[2][0] == Span(step=0, message=2, part=0)
    assert returned(recorded[2][1]) == RESULT


async def test_durations_and_tools_ride_beside_the_messages(database, run_id):
    transcript = await RecordTranscriptLoader(database)(run_id)

    first, second = transcript.steps

    assert transcript.durations_ms == {"call_1": 2_000}
    assert first.parameters is not None
    assert [tool.name for tool in first.parameters.function_tools] == ["lookup_city"]
    assert second.parameters is None


async def test_the_instructions_come_back_on_the_requests(database, run_id):
    transcript = await RecordTranscriptLoader(database)(run_id)

    first = transcript.messages[0]

    assert isinstance(first, ModelRequest)
    assert first.instructions == INSTRUCTIONS


async def test_an_unknown_run_is_refused(database):
    with pytest.raises(LookupError):
        await RecordTranscriptLoader(database)("run-nobody-wrote")


async def test_a_run_that_recorded_no_step_is_an_empty_run(database, store):
    conversation_id = str(uuid4())
    run = "run-t-" + conversation_id
    await store.register_run(RunRecord(run_id=run, conversation_id=conversation_id, started_at=AT))

    with pytest.raises(RunEmptyError, match=run):
        await RecordTranscriptLoader(database)(run)
