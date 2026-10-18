"""A run that stops for the browser settles its history and nothing else."""

from __future__ import annotations

import asyncio

from typing import TYPE_CHECKING, cast

import pytest

from core.ai.workflows.contracts import StepRegistry
from pydantic_ai import Agent
from pydantic_ai.messages import ModelResponse, ToolCallPart
from pydantic_ai.models.function import FunctionModel
from pydantic_ai.tools import DeferredToolRequests, Tool, ToolDefinition
from pydantic_ai.toolsets import ExternalToolset

from harness.features.authoring import service as authoring
from harness.features.authoring.domain import Draft
from harness.session import NoSessionLog, Runtime

if TYPE_CHECKING:
    from collections.abc import Sequence
    from pathlib import Path

    from core.ai.workflows.contracts import WorkflowLibrary
    from pydantic_ai.messages import ModelMessage
    from pydantic_ai.models.function import AgentInfo
    from pydantic_ai.run import AgentRunResult

from tests.support.state import a_state, a_throwaway_state


def _model(messages: Sequence[ModelMessage], info: AgentInfo) -> ModelResponse:
    """Always answers with the navigate verb and the toggle_plugin act, never with text."""
    del messages, info
    return ModelResponse(
        parts=[
            ToolCallPart("navigate", {"url": "/chat"}, tool_call_id="c-1"),
            ToolCallPart("toggle_plugin", {"name": "sql", "on": True}, tool_call_id="c-2"),
        ]
    )


def _toggle_plugin(name: str, *, on: bool) -> str:
    """Stands in for the console act, named as the real one is. The call always ends deferred before this body runs."""
    return f"{name} {on}"


def _deferred_result() -> AgentRunResult[DeferredToolRequests]:
    """One run that calls the navigate verb and the toggle_plugin act, then stops for both."""
    agent = Agent(
        FunctionModel(_model),
        output_type=[str, DeferredToolRequests],
        toolsets=[ExternalToolset([ToolDefinition(name="navigate")], id="verbs")],
        tools=[Tool(_toggle_plugin, name="toggle_plugin", requires_approval=True)],
    )
    result = asyncio.run(agent.run("go"))
    assert isinstance(result.output, DeferredToolRequests)
    return cast("AgentRunResult[DeferredToolRequests]", result)


def test_deferred_names_lists_calls_then_approvals() -> None:
    assert authoring.deferred_names(_deferred_result().output) == ("navigate", "toggle_plugin")


def test_settle_deferred_keeps_the_history_and_leaves_the_draft_alone() -> None:
    session = Runtime(
        registry=StepRegistry(),
        library=cast("WorkflowLibrary", None),
        agent=cast("Agent[Runtime, str | DeferredToolRequests]", None),
        session_kind="master",
        session_log=NoSessionLog(),
        state=a_throwaway_state(),
    )
    session.conversation.draft = Draft(title="kept")
    result = _deferred_result()
    pending = authoring.settle_deferred(session, result, result.output)
    assert pending == ("navigate", "toggle_plugin")
    assert len(session.conversation.history) == 2
    assert session.conversation.draft.title == "kept"


def test_the_ordinary_settle_refuses_a_deferred_result(tmp_path: Path) -> None:
    session = Runtime(registry=StepRegistry(), library=cast("WorkflowLibrary", None), agent=cast("Agent[Runtime, str | DeferredToolRequests]", None), session_log=NoSessionLog(), state=a_state(tmp_path))
    with pytest.raises(TypeError, match="deferred"):
        authoring.settle_turn(session, _deferred_result(), draft_file=tmp_path / "draft.json")
