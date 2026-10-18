"""`rescue_history`: closing the calls a failed or cut-off turn left open.

A turn that dies mid-tool-cycle steps a `ToolCallPart` with nothing answering it -- a shape no
provider accepts back. `rescue_history` closes every such call with one honest `ToolReturnPart`,
so the history that survives into the next turn is the one the agent actually lived through.
"""

from __future__ import annotations

from pydantic_ai.messages import ModelRequest, ModelResponse, RetryPromptPart, TextPart, ToolCallPart, ToolReturnPart, UserPromptPart
from pydantic_ai_harness.step_persistence import is_provider_valid

from harness.session import TURN_CUT_OFF, TURN_FAILED, rescue_history


def test_an_open_tool_call_is_closed_with_the_failed_turn_sentence() -> None:
    history = [
        ModelRequest(parts=[UserPromptPart("do it")]),
        ModelResponse(parts=[TextPart("checking"), ToolCallPart(tool_name="poke", args={}, tool_call_id="call-1")]),
    ]

    rescued = rescue_history(history, ended=TURN_FAILED.format(error="boom"))

    assert len(rescued) == 3
    closing = rescued[-1]
    assert isinstance(closing, ModelRequest)
    assert len(closing.parts) == 1
    part = closing.parts[0]
    assert isinstance(part, ToolReturnPart)
    assert part.tool_name == "poke"
    assert part.tool_call_id == "call-1"
    assert part.content == (
        "not answered, the turn ended here (boom). The owner's next message follows, answer it before calling this again."
    )
    assert is_provider_valid(rescued)


def test_two_open_calls_on_one_response_are_both_closed_in_one_request_in_call_order() -> None:
    history = [
        ModelResponse(
            parts=[
                ToolCallPart(tool_name="first", args={}, tool_call_id="call-1"),
                ToolCallPart(tool_name="second", args={}, tool_call_id="call-2"),
            ]
        ),
    ]

    rescued = rescue_history(history, ended=TURN_CUT_OFF)

    assert len(rescued) == 2
    closing = rescued[-1]
    assert isinstance(closing, ModelRequest)
    tool_returns = [part for part in closing.parts if isinstance(part, ToolReturnPart)]
    assert len(tool_returns) == 2
    assert [part.tool_call_id for part in tool_returns] == ["call-1", "call-2"]
    assert [part.tool_name for part in tool_returns] == ["first", "second"]
    assert all(
        part.content == "not answered, the turn was cut off here. The owner's next message follows, answer it before calling this again."
        for part in tool_returns
    )
    assert is_provider_valid(rescued)


def test_a_call_answered_by_a_return_and_one_answered_by_a_tool_bound_retry_are_left_alone() -> None:
    history = [
        ModelResponse(
            parts=[
                ToolCallPart(tool_name="answered", args={}, tool_call_id="call-1"),
                ToolCallPart(tool_name="retried", args={}, tool_call_id="call-2"),
            ]
        ),
        ModelRequest(
            parts=[
                ToolReturnPart(tool_name="answered", content="ok", tool_call_id="call-1"),
                RetryPromptPart(content="try again", tool_name="retried", tool_call_id="call-2"),
            ]
        ),
    ]

    rescued = rescue_history(history, ended=TURN_CUT_OFF)

    assert rescued == history
    assert rescued is not history
    assert is_provider_valid(rescued)


def test_an_output_validation_retry_does_not_count_as_answering_an_open_call() -> None:
    history = [
        ModelResponse(parts=[ToolCallPart(tool_name="poke", args={}, tool_call_id="call-1")]),
        ModelRequest(parts=[RetryPromptPart(content="bad json", tool_name=None)]),
    ]

    rescued = rescue_history(history, ended=TURN_CUT_OFF)

    assert len(rescued) == 3
    closing = rescued[-1]
    assert isinstance(closing, ModelRequest)
    assert len(closing.parts) == 1
    part = closing.parts[0]
    assert isinstance(part, ToolReturnPart)
    assert part.tool_call_id == "call-1"
    assert is_provider_valid(rescued)


def test_the_request_a_failed_turn_was_assembling_takes_the_closing_returns_and_is_marked_complete() -> None:
    history = [
        ModelResponse(
            parts=[
                ToolCallPart(tool_name="done", args={}, tool_call_id="call-1"),
                ToolCallPart(tool_name="open", args={}, tool_call_id="call-2"),
            ]
        ),
        ModelRequest(parts=[ToolReturnPart(tool_name="done", content="ok", tool_call_id="call-1")], state="interrupted"),
    ]

    rescued = rescue_history(history, ended=TURN_CUT_OFF)

    assert len(rescued) == 2
    closing = rescued[-1]
    assert isinstance(closing, ModelRequest)
    assert closing.state == "complete"
    assert [part.tool_call_id for part in closing.parts if isinstance(part, ToolReturnPart)] == ["call-1", "call-2"]
    assert is_provider_valid(rescued)


def test_an_empty_interrupted_request_is_filled_rather_than_followed() -> None:
    history = [
        ModelResponse(parts=[ToolCallPart(tool_name="poke", args={}, tool_call_id="call-1")]),
        ModelRequest(parts=[], state="interrupted"),
    ]

    rescued = rescue_history(history, ended=TURN_FAILED.format(error="boom"))

    assert len(rescued) == 2
    closing = rescued[-1]
    assert isinstance(closing, ModelRequest)
    assert closing.state == "complete"
    assert [part.tool_call_id for part in closing.parts if isinstance(part, ToolReturnPart)] == ["call-1"]
    assert is_provider_valid(rescued)


def test_a_history_with_nothing_open_comes_back_equal_and_not_the_same_list() -> None:
    history = [ModelRequest(parts=[UserPromptPart("hi")]), ModelResponse(parts=[TextPart("hello")])]

    rescued = rescue_history(history, ended=TURN_CUT_OFF)

    assert rescued == history
    assert rescued is not history
    assert is_provider_valid(rescued)
