"""The tool-call warning: silent below the threshold, URGENT past it, CRITICAL near the end, one request at a time."""

from __future__ import annotations

from types import NoneType
from typing import TYPE_CHECKING

import pytest

from pydantic_ai import Agent
from pydantic_ai.messages import ModelRequest, ModelResponse, SystemPromptPart, TextPart, ToolCallPart, UserPromptPart
from pydantic_ai.models.function import FunctionModel
from pydantic_ai.usage import UsageLimits

from core.ai.tool_call_limit import ToolCallLimitWarning

if TYPE_CHECKING:
    from collections.abc import Callable, Sequence

    from pydantic_ai.messages import ModelMessage
    from pydantic_ai.models.function import AgentInfo

_MARK = "Tool calls: "
"""The line every warning carries, and nothing else in these runs does."""


def _warnings_seen(messages: Sequence[ModelMessage]) -> list[str]:
    """Every warning text a request carried, in whichever part the model's profile rendered it as."""
    texts: list[str] = []
    for message in messages:
        if not isinstance(message, ModelRequest):
            continue
        for part in message.parts:
            content = part.content if isinstance(part, SystemPromptPart | UserPromptPart) else None
            if isinstance(content, str) and _MARK in content:
                texts.append(content)
    return texts


async def _run(calls_per_request: Sequence[int], *, limit: int | None) -> tuple[list[list[str]], list[ModelMessage]]:
    """Run an agent whose model makes the given parallel tool calls per request, then answers, under a tool-call limit.

    Returns the warnings each request carried, in request order, and the run's whole history.
    """
    seen: list[list[str]] = []

    def respond(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        del info
        seen.append(_warnings_seen(messages))
        index = len(seen) - 1
        if index < len(calls_per_request):
            return ModelResponse(parts=[ToolCallPart("look", {"n": n}, tool_call_id=f"c{index}-{n}") for n in range(calls_per_request[index])])
        return ModelResponse(parts=[TextPart("done")])

    agent = Agent(FunctionModel(respond), deps_type=NoneType, capabilities=[ToolCallLimitWarning[None]()])

    @agent.tool_plain
    def look(n: int) -> str:
        """Look once."""
        return f"seen {n}"

    result = await agent.run("go", usage_limits=UsageLimits(tool_calls_limit=limit))
    return seen, result.all_messages()


async def test_no_warning_below_seventy_percent_of_the_limit() -> None:
    seen, history = await _run([13], limit=20)

    assert seen == [[], []]
    assert _warnings_seen(history) == []


async def test_a_run_without_a_tool_call_limit_is_never_warned() -> None:
    seen, history = await _run([30], limit=None)

    assert seen == [[], []]
    assert _warnings_seen(history) == []


async def test_an_urgent_warning_follows_the_request_that_crossed_the_threshold() -> None:
    seen, _history = await _run([14], limit=20)

    assert seen[0] == []
    (warning,) = seen[1]
    assert "URGENT: The run's tool call limit is approaching." in warning
    assert "Tool calls: 14/20 used (70%), 6 remaining." in warning
    assert "`run_code` program makes counts" in warning
    assert "Finish the task with what you have" in warning


async def test_a_critical_warning_near_the_end_of_the_limit() -> None:
    seen, _history = await _run([18], limit=20)

    (warning,) = seen[1]
    assert "CRITICAL: The run's tool call limit is approaching." in warning
    assert "Tool calls: 18/20 used (90%), 2 remaining." in warning
    assert "Answer now with what you have." in warning


async def test_each_request_sees_only_its_own_warning_and_the_history_keeps_each_once() -> None:
    seen, history = await _run([14, 2], limit=20)

    assert len(seen[1]) == 1
    assert "14/20 used" in seen[1][0]
    assert len(seen[2]) == 1, "the earlier warning is no longer sent"
    assert "16/20 used" in seen[2][0]
    kept = [part for message in history if isinstance(message, ModelRequest) for part in message.parts if isinstance(part, SystemPromptPart)]
    assert [part.scope for part in kept] == ["request", "request"]
    assert ["14/20 used" in kept[0].content, "16/20 used" in kept[1].content] == [True, True]


@pytest.mark.parametrize(
    ("build", "message"),
    [
        (lambda: ToolCallLimitWarning[None](warning_threshold=0), "warning_threshold must be between"),
        (lambda: ToolCallLimitWarning[None](critical_remaining_tool_calls=-1), "critical_remaining_tool_calls must be non-negative"),
    ],
)
def test_a_setting_it_cannot_warn_by_is_refused(build: Callable[[], ToolCallLimitWarning[None]], message: str) -> None:
    with pytest.raises(ValueError, match=message):
        build()
