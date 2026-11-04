"""A step with a tool-call limit is warned as its calls near it, and a program's own calls count.

The warning is core's `WarnNearToolCallLimit`. These tests pin that `step_guards` mounts it with the step's own limit,
and that a briefing and a map turn both carry it once a `run_code` program has spent most of that limit.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from core.ai.tool_call_limit import WarnNearToolCallLimit
from core.ai.workflows.contracts import Briefing, FromNode, FromParameter, MapNode, ParameterSpec, Shape, Workflow, prepare, run_workflow
from core.ai.workflows.domain.vocabulary import Budget, SameAs
from pydantic_ai.messages import ModelRequest, ModelResponse, SystemPromptPart, ToolCallPart, UserPromptPart

from harness.adapters.environment import step_guards
from harness.session import ActiveRun
from tests.conftest import ApproveAll
from tests.support.environment import CALLED, spec_for, world_over
from tests.support.streaming import streaming_function_model

if TYPE_CHECKING:
    from collections.abc import Sequence

    from pydantic_ai.messages import ModelMessage
    from pydantic_ai.models.function import AgentInfo

def _searches(count: int) -> str:
    """A program that makes `count` tool calls inside one `run_code` call."""
    return f"for _ in range({count}):\n    await search(query='x')\n'done'"

_NOTE = {"type": "object", "title": "Note", "properties": {"note": {"type": "string"}}, "required": ["note"]}
_ITEM = {"type": "object", "title": "Item", "properties": {"title": {"type": "string"}}, "required": ["title"]}


def _warnings_seen(messages: Sequence[ModelMessage]) -> list[str]:
    """Every tool-call warning a request carried, in whichever part the model's profile rendered it as."""
    texts: list[str] = []
    for message in messages:
        if not isinstance(message, ModelRequest):
            continue
        for part in message.parts:
            content = part.content if isinstance(part, SystemPromptPart | UserPromptPart) else None
            if isinstance(content, str) and "Tool calls: " in content:
                texts.append(content)
    return texts


def test_step_guards_mount_the_tool_call_warning_with_the_steps_own_limit() -> None:
    (warning,) = [guard for guard in step_guards(24, tool_calls=160) if isinstance(guard, WarnNearToolCallLimit)]

    assert warning.max_tool_calls == 160
    assert not [guard for guard in step_guards(24) if isinstance(guard, WarnNearToolCallLimit)], "no limit, no warning"


async def test_a_briefings_program_calls_count_toward_its_tool_call_warning() -> None:
    seen: list[list[str]] = []

    def respond(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        seen.append(_warnings_seen(messages))
        if len(seen) == 1:
            return ModelResponse(parts=[ToolCallPart("run_code", {"code": _searches(8)})])
        return ModelResponse(parts=[ToolCallPart(info.output_tools[0].name, {"note": "ok"})])

    CALLED.clear()
    environment = await world_over(streaming_function_model(respond))
    task = Briefing(
        name="dig",
        instructions="Find what the knowledge base says about the question and report it in a single note.",
        inputs={"question": Shape(json_schema={"type": "string"})},
        output=Shape(json_schema=_NOTE),
        tools=("search",),
        budget=Budget(requests=4, tool_calls=10),
    )
    ready = prepare(spec_for(task), environment)
    assert ready.report.ok, ready.report.issues
    implementation = ready.registry.get("dig")
    assert implementation is not None

    await implementation.function(question="what does the knowledge base say?")

    assert CALLED == ["search"] * 8
    assert seen[0] == []
    (warning,) = seen[1]
    assert "CRITICAL: The run's tool call limit is approaching." in warning
    assert "Tool calls: 9/10 used (90%), 1 remaining." in warning, "eight calls inside the program and the run_code call itself"


async def test_a_map_turns_program_calls_count_toward_its_tool_call_warning() -> None:
    seen: list[list[str]] = []

    def respond(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        seen.append(_warnings_seen(messages))
        if len(seen) == 1:
            return ModelResponse(parts=[ToolCallPart(info.output_tools[0].name, {"note": "worth a look"})])
        if len(seen) == 2:
            return ModelResponse(parts=[ToolCallPart("run_code", {"code": _searches(10)})])
        return ModelResponse(parts=[ToolCallPart(info.output_tools[1].name, {"note": "found it"})])

    triage = Briefing(
        name="triage",
        instructions="Read the item and write one short triage note about it, nothing more.",
        inputs={"item": Shape(json_schema=_ITEM)},
        output=Shape(json_schema=_NOTE),
    )
    dig = Briefing(
        name="dig",
        instructions="Using your triage note, search the knowledge base and report what it says about the item.",
        inputs={"triage": SameAs(like="triage")},
        output=Shape(json_schema={**_NOTE, "title": "Finding"}),
        tools=("search",),
        budget=Budget(requests=4, tool_calls=15),
    )
    spec = Workflow(
        name="triage-then-dig",
        version=1,
        description="One worker triages an item, then digs into it with a program, as one conversation.",
        parameters={"items": ParameterSpec(description="items to dig into", json_schema={"type": "array", "items": _ITEM})},
        inline=(triage, dig),
        nodes=(MapNode(id="dig_each", turns=("triage", "dig"), over=FromParameter(parameter="items"), each="item"),),
        output=FromNode(node="dig_each"),
    )
    environment = await world_over(streaming_function_model(respond), active=ActiveRun())

    CALLED.clear()
    await run_workflow(spec, environment, params={"items": [{"title": "quantum computing"}]}, approval=ApproveAll())

    assert CALLED == ["search"] * 10
    assert seen[0] == []
    assert seen[1] == []
    (warning,) = seen[2]
    assert "URGENT: The run's tool call limit is approaching." in warning
    assert "Tool calls: 11/15 used (73%), 4 remaining." in warning, "ten calls inside the program and the run_code call itself"
