"""Every harness agent that runs `run_code` carries the sandbox's rules, and the dialect names the function list by `CodeMode`'s heading.

`SandboxDiscipline` and its own tests live in core. These tests stay here because they run `CodeMode`, whose sandbox
core does not install. A step worker mounts it beside its `CodeMode`, so a briefing or a map turn with tools reads the
dialect up front and gets the way out of an unknown function refusal, and a step without tools reads neither. A step
reads no text about loading a capability or about a person, and a step whose `run_code` retries are spent still ends on
`UnexpectedModelBehavior`, so the run's own error handling decides.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, cast

import pytest

from core.ai.retrieval.contracts import RUN_CODE
from core.ai.sandbox import SandboxDiscipline
from core.ai.workflows.contracts import Briefing, FromNode, FromParameter, MapNode, ParameterSpec, Shape, Workflow, prepare, run_workflow
from core.ai.workflows.domain.vocabulary import SameAs
from pydantic_ai import Agent
from pydantic_ai.exceptions import UnexpectedModelBehavior
from pydantic_ai.messages import ModelMessage, ModelRequest, ModelResponse, TextPart, ToolCallPart, ToolReturnPart
from pydantic_ai.models.function import AgentInfo, FunctionModel
from pydantic_ai.toolsets import FunctionToolset
from pydantic_ai_harness import CodeMode

from harness.session import ActiveRun
from tests.conftest import ApproveAll
from tests.support.environment import spec_for, world_over
from tests.support.streaming import streaming_function_model

if TYPE_CHECKING:
    from collections.abc import Callable


def _dialect(*, chat: bool) -> str:
    """The instructions `SandboxDiscipline` adds to the chat agent, or to a step worker."""
    discipline = SandboxDiscipline[Any](loads_capabilities=chat, answers_a_person=chat)
    return discipline.get_instructions()(cast("Any", None))


_CATALOG_HEADING = "available inside the sandbox"
"""The words of `CodeMode`'s heading over the sandbox's functions, by which the dialect names that list."""

_CATALOG_BLOCK = '<context id="toolset:code_mode:catalog">'


async def _what_the_model_reads(*, dynamic_catalog: bool) -> tuple[str, str]:
    """`run_code`'s description and the catalog instruction block, as a model under a one-tool `CodeMode` receives them."""
    seen: dict[str, str] = {}

    async def ping() -> str:
        """Answer pong."""
        return "pong"

    def answer(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        seen["description"] = next(tool.description or "" for tool in info.function_tools if tool.name == RUN_CODE)
        last = messages[-1]
        seen["instructions"] = (last.instructions or "") if isinstance(last, ModelRequest) else ""
        return ModelResponse(parts=[TextPart("ok")])

    agent = Agent(FunctionModel(answer), toolsets=[FunctionToolset([ping])], capabilities=[CodeMode(dynamic_catalog=dynamic_catalog)])
    await agent.run("hi")
    instructions = seen["instructions"]
    catalog = instructions.split(_CATALOG_BLOCK, 1)[1].split("</context>", 1)[0] if _CATALOG_BLOCK in instructions else ""
    return seen["description"], catalog


@pytest.mark.parametrize("dynamic_catalog", [False, True])
async def test_codemode_heads_the_sandboxs_functions_with_the_words_the_dialect_names_them_by(dynamic_catalog: bool) -> None:
    """The dialect names the function list by `CodeMode`'s heading, so an upstream rewording of it must fail here first."""
    description, catalog = await _what_the_model_reads(dynamic_catalog=dynamic_catalog)

    assert _CATALOG_HEADING in _dialect(chat=dynamic_catalog), "the chat agent mounts the dynamic catalog, a step does not"
    if dynamic_catalog:
        assert _CATALOG_HEADING in catalog, "the chat agent's mount carries the list in the catalog instruction"
    else:
        assert _CATALOG_HEADING in description, "a briefing's mount carries the list in `run_code`'s description"


_DIALECT_HEADING = "## The sandbox's dialect"
"""The first line of the dialect a worker reads in its instructions."""

_UNKNOWN_CALL = "await adapt_workflow(ops=[])"
"""A program that calls a name the sandbox does not hold."""

_HINT = "`adapt_workflow` is not a function inside the sandbox"
"""The start of the way out the discipline appends to that refusal."""

_CHAT_ONLY = ("load_capability", "person")
"""Words true for the chat agent and never for a step: a step loads no capability and no person reads its answer."""

_NOTE = {"type": "object", "title": "Note", "properties": {"note": {"type": "string"}}, "required": ["note"]}
_ITEM = {"type": "object", "title": "Item", "properties": {"title": {"type": "string"}}, "required": ["title"]}


def _refusals(request: ModelRequest) -> list[str]:
    """The `run_code` refusals one request hands back to the model."""
    return [
        part.content
        for part in request.parts
        if isinstance(part, ToolReturnPart) and part.tool_name == RUN_CODE and part.outcome == "retried" and isinstance(part.content, str)
    ]


def _briefing(*, tools: tuple[str, ...]) -> Briefing:
    return Briefing(
        name="dig",
        instructions="Find what the knowledge base says about the question and report it in a single note.",
        inputs={"question": Shape(json_schema={"type": "string"})},
        output=Shape(json_schema=_NOTE),
        tools=tools,
    )


async def _run_briefing(task: Briefing, respond: Callable[[list[ModelMessage], AgentInfo], ModelResponse]) -> None:
    ready = prepare(spec_for(task), await world_over(streaming_function_model(respond)))
    assert ready.report.ok, ready.report.issues
    implementation = ready.registry.get("dig")
    assert implementation is not None
    await implementation.function(question="what does the knowledge base say?")


def _triage_then_dig(*, tools: tuple[str, ...]) -> Workflow:
    """A map whose item is triaged in one turn and dug into in a second turn holding `tools`."""
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
        tools=tools,
    )
    return Workflow(
        name="triage-then-dig",
        version=1,
        description="One worker triages an item, then digs into it, as one conversation.",
        parameters={"items": ParameterSpec(description="items to dig into", json_schema={"type": "array", "items": _ITEM})},
        inline=(triage, dig),
        nodes=(MapNode(id="dig_each", turns=("triage", "dig"), over=FromParameter(parameter="items"), each="item"),),
        output=FromNode(node="dig_each"),
    )


async def _run_map(spec: Workflow, respond: Callable[[list[ModelMessage], AgentInfo], ModelResponse]) -> None:
    environment = await world_over(streaming_function_model(respond), active=ActiveRun())
    await run_workflow(spec, environment, params={"items": [{"title": "quantum computing"}]}, approval=ApproveAll())


async def test_a_briefing_with_tools_reads_the_dialect_and_the_way_out_of_an_unknown_function() -> None:
    requests: list[ModelRequest] = []

    def respond(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        last = messages[-1]
        assert isinstance(last, ModelRequest)
        requests.append(last)
        if len(requests) == 1:
            return ModelResponse(parts=[ToolCallPart(RUN_CODE, {"code": _UNKNOWN_CALL})])
        return ModelResponse(parts=[ToolCallPart(info.output_tools[0].name, {"note": "ok"})])

    await _run_briefing(_briefing(tools=("search",)), respond)

    instructions = requests[0].instructions or ""
    assert _DIALECT_HEADING in instructions
    (refusal,) = _refusals(requests[1])
    assert _HINT in refusal, refusal
    assert not [word for word in _CHAT_ONLY if word in instructions or word in refusal]


async def test_a_briefing_without_tools_reads_no_sandbox_rules() -> None:
    requests: list[ModelRequest] = []
    tools_offered: list[str] = []

    def respond(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        last = messages[-1]
        assert isinstance(last, ModelRequest)
        requests.append(last)
        tools_offered.extend(tool.name for tool in info.function_tools)
        return ModelResponse(parts=[ToolCallPart(info.output_tools[0].name, {"note": "ok"})])

    await _run_briefing(_briefing(tools=()), respond)

    assert RUN_CODE not in tools_offered
    assert all(_DIALECT_HEADING not in (request.instructions or "") for request in requests)


async def test_a_map_turn_with_tools_reads_the_dialect_and_the_way_out_of_an_unknown_function() -> None:
    requests: list[ModelRequest] = []

    def respond(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        last = messages[-1]
        assert isinstance(last, ModelRequest)
        requests.append(last)
        if len(requests) == 1:
            return ModelResponse(parts=[ToolCallPart(info.output_tools[0].name, {"note": "worth a look"})])
        if len(requests) == 2:
            return ModelResponse(parts=[ToolCallPart(RUN_CODE, {"code": _UNKNOWN_CALL})])
        return ModelResponse(parts=[ToolCallPart(info.output_tools[1].name, {"note": "found it"})])

    await _run_map(_triage_then_dig(tools=("search",)), respond)

    instructions = requests[1].instructions or ""
    assert _DIALECT_HEADING in instructions, "the turn that holds the tools reads the dialect"
    (refusal,) = _refusals(requests[2])
    assert _HINT in refusal, refusal
    assert not [word for word in _CHAT_ONLY if word in instructions or word in refusal]


async def test_a_map_turn_without_tools_reads_no_sandbox_rules() -> None:
    requests: list[ModelRequest] = []
    tools_offered: list[str] = []

    def respond(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        last = messages[-1]
        assert isinstance(last, ModelRequest)
        requests.append(last)
        tools_offered.extend(tool.name for tool in info.function_tools)
        index = 0 if len(requests) == 1 else 1
        return ModelResponse(parts=[ToolCallPart(info.output_tools[index].name, {"note": "ok"})])

    await _run_map(_triage_then_dig(tools=()), respond)

    assert len(requests) == 2, "one request per turn"
    assert RUN_CODE not in tools_offered
    assert all(_DIALECT_HEADING not in (request.instructions or "") for request in requests)


async def test_a_briefing_whose_run_code_retries_are_spent_still_fails() -> None:
    calls = 0

    def respond(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        nonlocal calls
        del messages
        calls += 1
        if calls <= 4:
            return ModelResponse(parts=[ToolCallPart(RUN_CODE, {"code": _UNKNOWN_CALL})])
        return ModelResponse(parts=[ToolCallPart(info.output_tools[0].name, {"note": "the program could not call adapt_workflow"})])

    with pytest.raises(UnexpectedModelBehavior, match="exceeded max retries"):
        await _run_briefing(_briefing(tools=("search",)), respond)

    assert calls == 4, "the fourth refusal ends the step, and the model is never asked to answer"
