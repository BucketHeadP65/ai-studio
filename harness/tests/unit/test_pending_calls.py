"""Tool results the browser posts, folded into pydantic-ai's deferred results for exactly the calls still pending."""

from __future__ import annotations

import pytest

from pydantic import ValidationError
from pydantic_ai.messages import ModelRequest, ModelResponse, TextPart, ToolCallPart, ToolReturnPart, UserPromptPart
from pydantic_ai.tools import DeferredToolResults, ToolDenied

from harness.features.authoring.api_models import ChatRequest
from harness.features.authoring.domain import ToolResultIn
from harness.features.authoring.pending_calls import answers_every_call, deferred_results, pending_calls
from harness.features.followme.domain import CONSOLE_ACTS, VERBS


def _folded(results: list[ToolResultIn], pending: dict[str, str]) -> DeferredToolResults | None:
    """The seam under its production vocabulary, which the caller hands in rather than the slice importing it."""
    return deferred_results(results, pending, verbs=VERBS, console_acts=CONSOLE_ACTS)


def _deferred_history() -> list[ModelRequest | ModelResponse]:
    return [
        ModelRequest(parts=[UserPromptPart(content="take me to chat")]),
        ModelResponse(parts=[ToolCallPart("navigate", {"url": "/chat"}, tool_call_id="c-1"), ToolCallPart("toggle_plugin", {"name": "sql", "on": True}, tool_call_id="c-2")]),
    ]


def test_pending_calls_names_the_unanswered_trailing_calls() -> None:
    assert pending_calls(_deferred_history()) == {"c-1": "navigate", "c-2": "toggle_plugin"}


def test_pending_calls_is_empty_once_the_calls_are_answered() -> None:
    history = [*_deferred_history(), ModelRequest(parts=[ToolReturnPart("navigate", "navigated to /chat", tool_call_id="c-1"), ToolReturnPart("toggle_plugin", "denied", tool_call_id="c-2")])]
    assert pending_calls(history) == {}


def test_pending_calls_is_empty_for_a_settled_history() -> None:
    assert pending_calls([ModelRequest(parts=[UserPromptPart(content="hi")]), ModelResponse(parts=[TextPart("hello")])]) == {}


def test_an_output_and_an_approval_fold_into_their_own_slots() -> None:
    results = [
        ToolResultIn(tool_call_id="c-1", tool_name="navigate", output="navigated to /chat"),
        ToolResultIn(tool_call_id="c-2", tool_name="toggle_plugin", approved=False, reason="superseded"),
    ]
    folded = _folded(results, pending_calls(_deferred_history()))
    assert folded is not None
    assert folded.calls == {"c-1": "navigated to /chat"}
    assert folded.approvals == {"c-2": ToolDenied(message="superseded")}


def test_an_approval_without_a_reason_is_a_bare_verdict() -> None:
    folded = _folded([ToolResultIn(tool_call_id="c-2", tool_name="toggle_plugin", approved=True)], {"c-2": "toggle_plugin"})
    assert folded is not None
    assert folded.approvals == {"c-2": True}


def test_a_result_for_a_call_that_is_not_pending_is_dropped() -> None:
    stale = [ToolResultIn(tool_call_id="old", tool_name="navigate", output="navigated to /chat")]
    assert _folded(stale, {"c-1": "navigate"}) is None


def test_an_approval_answer_for_a_client_executed_verb_is_dropped() -> None:
    answered_as_an_approval = [ToolResultIn(tool_call_id="c-1", tool_name="navigate", approved=True)]
    assert _folded(answered_as_an_approval, {"c-1": "navigate"}) is None


def test_an_outcome_answer_for_a_console_act_is_dropped() -> None:
    answered_as_an_outcome = [ToolResultIn(tool_call_id="c-2", tool_name="toggle_plugin", output="Plugin sql opened.")]
    assert _folded(answered_as_an_outcome, {"c-2": "toggle_plugin"}) is None


def test_the_bucket_follows_the_pending_name_and_not_the_posted_one() -> None:
    misnamed = [ToolResultIn(tool_call_id="c-2", tool_name="navigate", output="navigated to /chat")]
    assert _folded(misnamed, {"c-2": "toggle_plugin"}) is None


def test_a_pending_name_in_neither_vocabulary_is_dropped() -> None:
    unknown = [ToolResultIn(tool_call_id="c-3", tool_name="save_workflow", approved=True)]
    assert _folded(unknown, {"c-3": "save_workflow"}) is None


def test_a_mismatched_answer_is_dropped_while_its_neighbours_fold() -> None:
    results = [
        ToolResultIn(tool_call_id="c-1", tool_name="navigate", output="navigated to /chat"),
        ToolResultIn(tool_call_id="c-2", tool_name="toggle_plugin", output="Plugin sql opened."),
    ]
    folded = _folded(results, pending_calls(_deferred_history()))
    assert folded is not None
    assert folded.calls == {"c-1": "navigated to /chat"}
    assert folded.approvals == {}


def test_a_result_must_carry_exactly_one_answer() -> None:
    with pytest.raises(ValidationError, match="exactly one"):
        ToolResultIn(tool_call_id="c-1", tool_name="navigate")
    with pytest.raises(ValidationError, match="exactly one"):
        ToolResultIn(tool_call_id="c-1", tool_name="navigate", output="x", approved=True)


def test_a_request_without_tool_results_is_unchanged() -> None:
    assert ChatRequest(id="req-1", messages=[]).tool_results == []


def test_a_run_resumes_only_when_every_waiting_call_has_an_answer() -> None:
    pending = {"c-1": "navigate", "c-2": "toggle_plugin"}
    both = _folded(
        [ToolResultIn(tool_call_id="c-1", tool_name="navigate", output="went"), ToolResultIn(tool_call_id="c-2", tool_name="toggle_plugin", approved=True)],
        pending,
    )
    assert both is not None
    assert answers_every_call(both, pending)

    half = _folded([ToolResultIn(tool_call_id="c-1", tool_name="navigate", output="went")], pending)
    assert half is not None
    assert not answers_every_call(half, pending), "one answer of two does not resume a run"

    # A call whose own name neither vocabulary knows is dropped by the fold, so it stays unanswered
    # and the run must not resume on it.
    unknown = {"c-1": "navigate", "c-2": "a_tool_from_nowhere"}
    dropped = _folded(
        [ToolResultIn(tool_call_id="c-1", tool_name="navigate", output="went"), ToolResultIn(tool_call_id="c-2", tool_name="a_tool_from_nowhere", approved=True)],
        unknown,
    )
    assert dropped is not None
    assert not answers_every_call(dropped, unknown)

    # The pending call decides which slot an answer fills, so the name the caller puts on it is not
    # read at all. A caller cannot answer for a call the run is not actually waiting on.
    mislabelled = _folded(
        [ToolResultIn(tool_call_id="c-1", tool_name="navigate", output="went"), ToolResultIn(tool_call_id="c-2", tool_name="approve", approved=True)],
        pending,
    )
    assert mislabelled is not None
    assert mislabelled.approvals == {"c-2": True}, "the history says c-2 is toggle_plugin, and the history is what counts"

    assert answers_every_call(both, {}), "nothing waiting is trivially answered"
