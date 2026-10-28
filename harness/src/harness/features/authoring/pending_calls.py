"""Which tool calls a session's history is still waiting on, and how the browser's answers reach the run."""

from __future__ import annotations

from typing import TYPE_CHECKING

from pydantic_ai.messages import ModelRequest, ModelResponse, ToolCallPart, ToolReturnPart
from pydantic_ai.tools import DeferredToolResults, ToolDenied

if TYPE_CHECKING:
    from collections.abc import Container, Mapping, Sequence

    from pydantic_ai.messages import ModelMessage

    from harness.features.authoring.domain import ToolResultIn


def pending_calls(history: Sequence[ModelMessage]) -> dict[str, str]:
    """The tool calls the newest model response made that no later request answered.

    Args:
        history: The session's messages, oldest first.

    Returns:
        Each unanswered call id with its tool name, empty when nothing is pending.
    """
    last_response = next((index for index in range(len(history) - 1, -1, -1) if isinstance(history[index], ModelResponse)), None)
    if last_response is None:
        return {}
    response = history[last_response]
    if not isinstance(response, ModelResponse):  # pragma: no cover (the index above found a response)
        return {}
    calls = {part.tool_call_id: part.tool_name for part in response.parts if isinstance(part, ToolCallPart)}
    for later in history[last_response + 1 :]:
        if isinstance(later, ModelRequest):
            for part in later.parts:
                if isinstance(part, ToolReturnPart):
                    calls.pop(part.tool_call_id, None)
    return calls


def _verdict(*, approved: bool, reason: str | None) -> bool | ToolDenied:
    """The person's answer in the shape an approvals map takes, a denial carrying its reason when it has one."""
    if approved:
        return True
    return ToolDenied(message=reason) if reason else False


def denying_unanswered(
    results: DeferredToolResults | None,
    pending: Mapping[str, str],
    *,
    verbs: Container[str],
    console_acts: Container[str],
    reason: str,
) -> DeferredToolResults | None:
    """`results` with every pending call it does not answer refused, so a new message can run.

    A verb takes `reason` as its outcome, and a console act takes it as a denial the model reads back.
    A pending call that is neither is left alone. A `results` that is passed in is filled in place.

    Args:
        results: The answers the browser sent, or `None` when it sent none.
        pending: Each waiting call id with its tool name.
        verbs: The client-executed tool names, which take an outcome.
        console_acts: The tool names that wait on the person's verdict.
        reason: What every refused call is answered with.

    Returns:
        The answers with the refusals folded in, or `None` when it holds no answer at all.
    """
    folded = results if results is not None else DeferredToolResults()
    for call_id, name in pending.items():
        if call_id in folded.calls or call_id in folded.approvals:
            continue
        if name in verbs:
            folded.calls[call_id] = reason
        elif name in console_acts:
            folded.approvals[call_id] = ToolDenied(message=reason)
    return folded if folded.calls or folded.approvals else None


def answers_every_call(results: DeferredToolResults, pending: Mapping[str, str]) -> bool:
    """Whether `results` answers every call still waiting, which is what resuming a run requires.

    Check here before resuming, and hold the turn when this is false, since the agent refuses a
    partial answer in words meant for a developer.

    Args:
        results: The answers folded so far.
        pending: Each waiting call id with its tool name.

    Returns:
        True when every pending id has an answer or a verdict.
    """
    return set(pending) <= set(results.calls) | set(results.approvals)


def deferred_results(
    results: Sequence[ToolResultIn],
    pending: Mapping[str, str],
    *,
    verbs: Container[str],
    console_acts: Container[str],
) -> DeferredToolResults | None:
    """The browser's answers for the calls actually pending, in the shape the run consumes.

    The pending call decides which slot its answer fills, never the answer's own shape. Three answers are dropped,
    never refused: one for an id that is not pending, one in the wrong shape for its call, and one for a name in
    neither `verbs` nor `console_acts`. A dropped answer leaves its call unanswered, so resume a run only when the
    result covers every pending id.

    Args:
        results: The browser's answers.
        pending: Each waiting call id with its tool name.
        verbs: The client-executed tool names, which take an outcome.
        console_acts: The tool names that wait on the person's verdict.

    Returns:
        The answers that apply, or `None` when none does.
    """
    folded = DeferredToolResults()
    for result in results:
        name = pending.get(result.tool_call_id)
        if name is None:
            continue
        if name in verbs and result.output is not None:
            folded.calls[result.tool_call_id] = result.output
        elif name in console_acts and result.approved is not None:
            folded.approvals[result.tool_call_id] = _verdict(approved=result.approved, reason=result.reason)
    return folded if folded.calls or folded.approvals else None
