"""A step names its skills: `Briefing.skills` mounts a narrowed `Skills` capability on its worker.

`Skills(SKILLS, include=task.skills)` defers every named skill, which is what makes
`load_capability` appear on the model's tool listing at all, and the deferred capability
catalog rendered into the instructions names only the skills the step asked for.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from core.ai.workflows.contracts import Briefing, Shape, prepare
from pydantic_ai.messages import ModelResponse, ToolCallPart

from tests.support.environment import spec_for, world_over
from tests.support.streaming import streaming_function_model

if TYPE_CHECKING:
    from pydantic_ai.messages import ModelMessage
    from pydantic_ai.models.function import AgentInfo


def _task(*, skills: tuple[str, ...]) -> Briefing:
    return Briefing(
        name="dig",
        instructions="Find what the knowledge base says about the question and report it in a single note.",
        inputs={"question": Shape(json_schema={"type": "string"})},
        output=Shape(json_schema={"type": "object", "properties": {"note": {"type": "string"}}, "required": ["note"]}),
        skills=skills,
    )


async def test_a_step_naming_a_skill_holds_only_that_skill_s_capability() -> None:
    infos: list[AgentInfo] = []

    def respond(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        infos.append(info)
        return ModelResponse(parts=[ToolCallPart(info.output_tools[0].name, {"note": "ok"})])

    environment = await world_over(streaming_function_model(respond))
    ready = prepare(spec_for(_task(skills=("agent-ux", "documenting"))), environment)
    assert ready.report.ok, ready.report.issues
    implementation = ready.registry.get("dig")
    assert implementation is not None

    await implementation.function(question="what does the knowledge base say?")

    assert infos
    tool_names = {tool.name for tool in infos[-1].function_tools}
    assert "load_capability" in tool_names
    instructions = infos[-1].instructions or ""
    assert "agent-ux" in instructions
    assert "documenting" in instructions, "a step reaches engram's knowledge base skills by name too"
    assert "saving-a-workflow" not in instructions


async def test_a_step_naming_no_skills_holds_no_load_capability_tool() -> None:
    infos: list[AgentInfo] = []

    def respond(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        infos.append(info)
        return ModelResponse(parts=[ToolCallPart(info.output_tools[0].name, {"note": "ok"})])

    environment = await world_over(streaming_function_model(respond))
    ready = prepare(spec_for(_task(skills=())), environment)
    assert ready.report.ok, ready.report.issues
    implementation = ready.registry.get("dig")
    assert implementation is not None

    await implementation.function(question="what does the knowledge base say?")

    assert infos
    tool_names = {tool.name for tool in infos[-1].function_tools}
    assert "load_capability" not in tool_names
