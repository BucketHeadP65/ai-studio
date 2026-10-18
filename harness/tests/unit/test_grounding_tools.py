"""`knowledge_base_profile`, `recall_beliefs`, `remember_belief`, `skip_digest`: grantable to a step, never on offer to chat.

`AuthoringTools.tools()` deliberately never bundles these four -- `studio_read_tools` mounts only the
four-tool bundle onto the knowledge base surface a step's reads are derived from. So an environment granting them
at all is this app's own doing: `build_environment`/`run_defined_step`/`build_worker_agent` build a
second, small toolset straight off the raw `AuthoringTools` instance, the same shape `web_search`
is granted in. These tests pin that wiring the same way `test_world_surface.py` pins `web_search`'s.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from core.ai.workflows.contracts import (
    AgentStep,
    FromNode,
    FromParameter,
    ParameterSpec,
    RunNode,
    Shape,
    Workflow,
    prepare,
    run_workflow,
    validate,
)
from pydantic_ai.messages import ModelMessage, ModelResponse, ToolCallPart

from harness.adapters.knowledge_base import GROUNDING_TOOLS, grounding_toolset
from harness.composition import CHAT_KNOWLEDGE_BASE_PLUGINS
from harness.domain.ports import JUDGE_TOOLS
from tests.support.environment import defined_step, fake_plugins, spec_for, world_over
from tests.support.streaming import streaming_function_model

if TYPE_CHECKING:
    from pydantic_ai.models.function import AgentInfo


def test_the_toolset_holds_exactly_the_tools_a_step_alone_may_be_granted() -> None:
    toolset = grounding_toolset(fake_plugins())
    assert set(toolset.tools) == set(GROUNDING_TOOLS)
    assert set(GROUNDING_TOOLS) == {"knowledge_base_profile", "recall_beliefs", "remember_belief", "skip_digest"}


def test_skip_digest_never_reaches_chat_or_a_judge() -> None:
    assert "skip_digest" not in CHAT_KNOWLEDGE_BASE_PLUGINS
    assert "skip_digest" not in JUDGE_TOOLS


async def test_without_doors_grounding_tools_are_not_grantable() -> None:
    """An environment with no `AuthoringTools`, every test environment until this file, never offers a tool nothing backs."""
    environment = await world_over(streaming_function_model(_respond_immediately))
    assert not set(GROUNDING_TOOLS) & set(environment.grants)


async def test_with_doors_grounding_tools_join_the_grantable_set() -> None:
    environment = await world_over(streaming_function_model(_respond_immediately), plugins=fake_plugins())
    assert set(GROUNDING_TOOLS) <= set(environment.grants)


class _NoGates:
    async def review(self, gate_id: str, value: object, *, note: str = "") -> bool:  # pragma: no cover - never reached
        del value, note
        raise AssertionError(gate_id)


def _respond_immediately(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
    del messages
    return ModelResponse(parts=[ToolCallPart(info.output_tools[0].name, {"note": "not called, only offered"})])


async def test_a_defined_step_granted_a_grounding_tool_gets_it_folded_into_run_code() -> None:
    """Same shape as `web_search`: a briefing's grant rides inside `run_code`, not on the wire."""
    captured: dict[str, AgentInfo] = {}

    def respond(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        del messages
        captured["info"] = info
        return ModelResponse(parts=[ToolCallPart(info.output_tools[0].name, {"note": "not called, only offered"})])

    environment = await world_over(streaming_function_model(respond), plugins=fake_plugins())
    ready = prepare(spec_for(defined_step(tools=("knowledge_base_profile",))), environment)
    assert ready.report.ok, ready.report.issues
    implementation = ready.registry.get("dig")
    assert implementation is not None

    await implementation.function(question="what is this base's purpose")

    run_code = next(tool for tool in captured["info"].function_tools if tool.name == "run_code")
    assert "knowledge_base_profile" in (run_code.description or "")


async def test_a_defined_step_granted_only_one_grounding_tool_does_not_get_the_others() -> None:
    """The narrowing is per tool, not all-or-nothing: `recall_beliefs` alone never carries its siblings."""
    captured: dict[str, AgentInfo] = {}

    def respond(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        del messages
        captured["info"] = info
        return ModelResponse(parts=[ToolCallPart(info.output_tools[0].name, {"note": "not called, only offered"})])

    environment = await world_over(streaming_function_model(respond), plugins=fake_plugins())
    ready = prepare(spec_for(defined_step(tools=("recall_beliefs",))), environment)
    assert ready.report.ok, ready.report.issues
    implementation = ready.registry.get("dig")
    assert implementation is not None

    await implementation.function(question="what does the owner already believe")

    run_code = next(tool for tool in captured["info"].function_tools if tool.name == "run_code")
    description = run_code.description or ""
    assert "recall_beliefs" in description
    assert "knowledge_base_profile" not in description
    assert "remember_belief" not in description


async def test_a_defined_step_granted_skip_digest_gets_it_and_no_other_write() -> None:
    """The narrowing is per tool: `skip_digest` alone never carries the grounding tools."""
    captured: dict[str, AgentInfo] = {}

    def respond(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        del messages
        captured["info"] = info
        return ModelResponse(parts=[ToolCallPart(info.output_tools[0].name, {"note": "not called, only offered"})])

    environment = await world_over(streaming_function_model(respond), plugins=fake_plugins())
    ready = prepare(spec_for(defined_step(tools=("skip_digest",))), environment)
    assert ready.report.ok, ready.report.issues
    implementation = ready.registry.get("dig")
    assert implementation is not None

    await implementation.function(question="which digest falls outside the purpose")

    run_code = next(tool for tool in captured["info"].function_tools if tool.name == "run_code")
    description = run_code.description or ""
    assert "skip_digest" in description
    assert "knowledge_base_profile" not in description
    assert "recall_beliefs" not in description
    assert "remember_belief" not in description


async def test_a_defined_step_not_granted_a_grounding_tool_does_not_get_it() -> None:
    captured: dict[str, AgentInfo] = {}

    def respond(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        del messages
        captured["info"] = info
        return ModelResponse(parts=[ToolCallPart(info.output_tools[0].name, {"note": "not called, only offered"})])

    environment = await world_over(streaming_function_model(respond), plugins=fake_plugins())
    ready = prepare(spec_for(defined_step(tools=("run_sql",))), environment)
    assert ready.report.ok, ready.report.issues
    implementation = ready.registry.get("dig")
    assert implementation is not None

    await implementation.function(question="what do the sources disagree about")

    run_code = next(tool for tool in captured["info"].function_tools if tool.name == "run_code")
    description = run_code.description or ""
    assert not any(name in description for name in GROUNDING_TOOLS)


async def test_a_worker_can_hold_a_grounding_tool_directly_unlike_the_shell() -> None:
    """Unlike `run_command`, a grounding tool behaves as a read for a worker: grantable and reachable."""
    answered: dict[str, AgentInfo] = {}

    def respond(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        del messages
        answered["info"] = info
        return ModelResponse(parts=[ToolCallPart(info.output_tools[0].name, {"note": "not called, only offered"})])

    environment = await world_over(streaming_function_model(respond), plugins=fake_plugins())
    spec = Workflow(
        name="ground-once",
        version=1,
        description="One worker granted knowledge_base_profile only.",
        parameters={"question": ParameterSpec(description="what to look into", json_schema={"type": "string"})},
        inline=(
            AgentStep(
                name="dig",
                agent={"instructions": "Read the base's own purpose before answering."},
                inputs={"question": Shape(json_schema={"type": "string"})},
                output=Shape(json_schema={"type": "object", "properties": {"note": {"type": "string"}}, "required": ["note"]}),
                tools=("knowledge_base_profile",),
            ),
        ),
        nodes=(RunNode(id="dig", work="dig", inputs={"question": FromParameter(parameter="question")}),),
        output=FromNode(node="dig"),
    )
    assert validate(spec, environment).ok, "the cold check passes -- a grounding tool behaves like a read here"

    await run_workflow(spec, environment, params={"question": "what is this base for"}, approval=_NoGates())

    assert "knowledge_base_profile" in {tool.name for tool in answered["info"].function_tools}


async def test_without_doors_a_step_asking_for_a_grounding_tool_is_refused_cold() -> None:
    """With no `AuthoringTools`, a grounding tool is not grantable at all -- refused at validation, not mid-run."""
    environment = await world_over(streaming_function_model(_respond_immediately))
    report = validate(spec_for(defined_step(tools=("knowledge_base_profile",))), environment)
    assert not report.ok
    assert any("knowledge_base_profile" in issue.detail for issue in report.issues)
