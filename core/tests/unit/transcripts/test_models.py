"""The transcript: steps kept apart, messages known by ordinal, calls paired with their first answer."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from pydantic import ValidationError
from pydantic_ai.messages import (
    ModelMessage,
    ModelRequest,
    ModelResponse,
    RetryFeedbackPart,
    RetryInfo,
    TextPart,
    ToolCallPart,
    ToolReturnPart,
    UserPromptPart,
)
from pydantic_ai.models import ModelRequestParameters
from pydantic_ai.tools import ToolDefinition

from core.ai.transcripts.contracts import Span, Step, Transcript

AT = datetime(2026, 9, 4, 12, 0, tzinfo=UTC)
RESULT = "sunny and mild"
CLEARED = "[tool result cleared]"


def question() -> ModelRequest:
    return ModelRequest(parts=[UserPromptPart(content="What is the weather in Paris?", timestamp=AT)])


def call(tool_call_id: str = "call_1") -> ModelResponse:
    return ModelResponse(
        parts=[ToolCallPart(tool_name="lookup_city", args={"name": "Paris"}, tool_call_id=tool_call_id)], timestamp=AT
    )


def result(content: str = RESULT, tool_call_id: str = "call_1") -> ModelRequest:
    return ModelRequest(parts=[ToolReturnPart(tool_name="lookup_city", content=content, tool_call_id=tool_call_id, timestamp=AT)])


def answer() -> ModelResponse:
    return ModelResponse(parts=[TextPart(content="It is sunny in Paris.")], timestamp=AT)


def returned(message: ModelMessage) -> str:
    """The content of the tool return a message carries first."""
    part = message.parts[0]
    assert isinstance(part, ToolReturnPart)
    return str(part.content)


def step(*messages: ModelMessage, ordinals: tuple[int, ...] | None = None) -> Step:
    return Step(messages=messages, ordinals=tuple(range(len(messages))) if ordinals is None else ordinals)


def two_steps() -> tuple[Step, Step]:
    """The result is recorded whole in the first step and cleared in the second, the way compaction leaves it."""
    return step(question(), call(), result()), step(question(), call(), result(CLEARED), answer())


class TestStep:
    def test_refuses_ordinals_that_do_not_pair_one_to_one_with_its_messages(self) -> None:
        with pytest.raises(ValidationError, match="one ordinal per message"):
            Step(messages=(question(), call()), ordinals=(0,))

    def test_refuses_the_same_ordinal_twice(self) -> None:
        with pytest.raises(ValidationError, match="ordinal 0 twice"):
            Step(messages=(question(), call()), ordinals=(0, 0))

    def test_refuses_an_answer_with_no_earlier_call(self) -> None:
        with pytest.raises(ValidationError, match="no earlier call"):
            step(question(), result())

    def test_refuses_a_call_answered_twice(self) -> None:
        with pytest.raises(ValidationError, match="answered twice"):
            step(question(), call(), result(), result())

    def test_carries_the_parameters_the_model_was_sent_with(self) -> None:
        tools = [ToolDefinition(name="lookup_city", description="One city by name.", parameters_json_schema={"type": "object"})]

        held = Step(messages=(question(),), ordinals=(0,), parameters=ModelRequestParameters(function_tools=tools))

        assert held.parameters is not None
        assert [tool.name for tool in held.parameters.function_tools] == ["lookup_city"]

    def test_parameters_are_absent_when_the_source_recorded_none(self) -> None:
        assert step(question()).parameters is None


class TestRecorded:
    def test_reads_each_message_once_in_the_form_of_the_first_step_that_held_it(self) -> None:
        transcript = Transcript(source="test:run", steps=two_steps())

        recorded = list(transcript.recorded())

        assert [span for span, _ in recorded] == [
            Span(step=0, message=0, part=0),
            Span(step=0, message=1, part=0),
            Span(step=0, message=2, part=0),
            Span(step=1, message=3, part=0),
        ]
        assert returned(recorded[2][1]) == RESULT

    def test_follows_ordinals_when_a_step_drops_a_message(self) -> None:
        first = step(question(), call(), result())
        second = step(question(), call(), answer(), ordinals=(0, 1, 3))
        transcript = Transcript(source="test:run", steps=(first, second))

        recorded = list(transcript.recorded())

        assert [span for span, _ in recorded] == [
            Span(step=0, message=0, part=0),
            Span(step=0, message=1, part=0),
            Span(step=0, message=2, part=0),
            Span(step=1, message=2, part=0),
        ]
        assert isinstance(recorded[3][1].parts[0], TextPart)


class TestCalls:
    def test_pairs_each_call_with_its_first_recorded_answer_and_its_duration(self) -> None:
        transcript = Transcript(source="test:run", steps=two_steps(), durations_ms={"call_1": 250})

        (found,) = transcript.calls()

        assert found.at == Span(step=0, message=1, part=0)
        assert found.call.tool_call_id == "call_1"
        assert isinstance(found.answer, ToolReturnPart)
        assert found.answer.content == RESULT
        assert found.duration_ms == 250

    def test_a_retried_return_is_the_answer_of_a_call_that_has_to_be_made_again(self) -> None:
        retried = ModelRequest(
            parts=[
                ToolReturnPart(
                    tool_name="lookup_city",
                    content="name must be a string",
                    tool_call_id="call_1",
                    timestamp=AT,
                    outcome="retried",
                    retry=RetryInfo(cause="validation_error"),
                )
            ]
        )
        transcript = Transcript(source="test:run", steps=(step(question(), call(), retried),))

        (found,) = transcript.calls()

        assert found.answer is not None
        assert (found.answer.outcome, found.answer.retry) == ("retried", RetryInfo(cause="validation_error"))

    def test_retry_feedback_answers_no_call(self) -> None:
        feedback = ModelRequest(parts=[RetryFeedbackPart(content="Please return text.", retry=RetryInfo(cause="no_output"), timestamp=AT)])
        transcript = Transcript(source="test:run", steps=(step(question(), call(), feedback),))

        (found,) = transcript.calls()

        assert found.answer is None

    def test_a_call_nothing_answered_has_no_answer_and_no_duration(self) -> None:
        transcript = Transcript(source="test:run", steps=(step(question(), call()),))

        (found,) = transcript.calls()

        assert found.answer is None
        assert found.duration_ms is None

    def test_a_call_knows_the_span_of_its_answer(self) -> None:
        transcript = Transcript(source="test:run", steps=two_steps())

        (found,) = transcript.calls()

        assert found.answered_at == Span(step=0, message=2, part=0)

    def test_an_unanswered_call_has_no_answer_span(self) -> None:
        transcript = Transcript(source="test:run", steps=(step(question(), call()),))

        (found,) = transcript.calls()

        assert found.answered_at is None


class TestViews:
    def test_messages_is_what_the_model_sees_now(self) -> None:
        first, second = two_steps()

        assert Transcript(source="test:run", steps=(first, second)).messages == second.messages

    def test_seen_at_is_the_history_the_model_had_before_the_part(self) -> None:
        first, second = two_steps()

        seen = Transcript(source="test:run", steps=(first, second)).seen_at(Span(step=1, message=3, part=0))

        assert seen == second.messages[:3]

    def test_part_at_points_at_one_part(self) -> None:
        transcript = Transcript(source="test:run", steps=two_steps())

        assert isinstance(transcript.part_at(Span(step=0, message=1, part=0)), ToolCallPart)

    def test_a_source_names_the_scheme_of_its_loader(self) -> None:
        with pytest.raises(ValidationError):
            Transcript(source="run-1", steps=two_steps())
