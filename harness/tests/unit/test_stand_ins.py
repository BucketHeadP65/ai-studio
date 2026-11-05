"""Stand-ins: a case run's write tools check a call, record it and land nothing.

A stand-in sits under the real tool's own name and shows the model the real tool's description and argument schema.
`write_note`'s runs `check_note` with the same arguments. Every other write's checks the arguments against the real
tool's schema. Each would fail if the stand-in called the real tool, skipped its check, or recorded a refused call.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from core.ai.workflows.contracts import Briefing, Program, Shape
from pydantic import BaseModel, JsonValue
from pydantic_ai import Agent
from pydantic_ai.messages import ModelMessage, ModelResponse, TextPart, ToolCallPart, ToolReturnPart
from pydantic_ai.models.function import AgentInfo, FunctionModel
from pydantic_ai.toolsets import FunctionToolset
from pydantic_monty import AsyncMonty

from harness.adapters.contracts import StandInCall, stand_in_caller, stand_in_toolset
from harness.domain.contracts import CASE_RUN_TOOLS
from tests.support.environment import fake_plugins, world_over
from tests.support.fakes.writing_surface import CHECKED, FakeWritingSurface
from tests.support.streaming import streaming_function_model

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Callable
    from pathlib import Path

    from harness.adapters.contracts import StandInCalls

PAGE: dict[str, JsonValue] = {"frontmatter": {"id": "talk.a-talk", "type": "talk"}, "sections": [], "claims": []}
"""A page payload. Its content does not matter here, only that it reaches `check_note` whole."""


def _calling(tool: str, arguments: dict[str, object], seen: list[AgentInfo] | None = None) -> FunctionModel:
    """A model that calls `tool` once with `arguments`, then answers in text."""

    def respond(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        if seen is not None:
            seen.append(info)
        if len(messages) == 1:
            return ModelResponse(parts=[ToolCallPart(tool, arguments)])
        return ModelResponse(parts=[TextPart("done")])

    return FunctionModel(respond)


def _answers(messages: list[ModelMessage], tool: str) -> list[str]:
    """What the model was handed back from `tool`, an answer or a refusal to retry, in order."""
    return [
        str(part.content)
        for message in messages
        for part in message.parts
        if isinstance(part, ToolReturnPart) and part.tool_name == tool
    ]


def skip_digest(knowledge_base: str, page_id: str, reason: str) -> str:
    """Mark one source as not worth a digest, with the reason."""
    return "skipped"


class TestStandInToolset:
    async def test_a_write_note_call_reaches_check_note_with_the_same_arguments_and_is_recorded(self) -> None:
        surface = FakeWritingSurface()
        calls: StandInCalls = []
        arguments: dict[str, object] = {"knowledge_base": "research", "payload": PAGE}
        agent = Agent(_calling("write_note", arguments), toolsets=[stand_in_toolset(surface, calls)])

        result = await agent.run("write the page")

        assert surface.ran == ["check_note"], "the stand-in reached the real write_note, or skipped check_note"
        assert surface.direct_calls == [("check_note", arguments)]
        assert calls == [StandInCall(tool="write_note", arguments={"knowledge_base": "research", "payload": PAGE}, answer=f"checked, not written: {CHECKED}")]
        assert _answers(result.all_messages(), "write_note") == [f"checked, not written: {CHECKED}"]

    async def test_the_model_sees_the_real_tools_description_and_argument_schema(self) -> None:
        surface = FakeWritingSurface()
        seen: list[AgentInfo] = []
        agent = Agent(_calling("write_note", {"knowledge_base": "research", "payload": PAGE}, seen), toolsets=[stand_in_toolset(surface, [])])

        await agent.run("write the page")

        offered = {tool.name: tool for tool in seen[0].function_tools}
        real = {tool.name: tool for tool in await surface.list_tools()}
        assert offered["write_note"].parameters_json_schema == real["write_note"].input_schema
        assert offered["write_note"].description == real["write_note"].description
        assert offered["request_source"].parameters_json_schema == real["request_source"].input_schema
        assert "skip_digest" not in offered, "a write no surface offers is not shown"

    async def test_a_request_source_call_missing_a_required_field_is_refused_and_nothing_is_recorded(self) -> None:
        surface = FakeWritingSurface()
        calls: StandInCalls = []
        agent = Agent(_calling("request_source", {"knowledge_base": "research"}), toolsets=[stand_in_toolset(surface, calls)])

        result = await agent.run("queue the source")

        assert calls == [], "a call the schema refuses was recorded as done"
        assert surface.ran == []
        (refusal,) = _answers(result.all_messages(), "request_source")
        assert "'url' is a required property" in refusal

    async def test_a_request_source_call_that_fits_the_schema_is_recorded_and_not_done(self) -> None:
        surface = FakeWritingSurface()
        calls: StandInCalls = []
        arguments: dict[str, object] = {"knowledge_base": "research", "url": "https://example.org/paper"}
        agent = Agent(_calling("request_source", arguments), toolsets=[stand_in_toolset(surface, calls)])

        result = await agent.run("queue the source")

        assert surface.ran == [], "the stand-in reached the real request_source"
        assert calls == [StandInCall(tool="request_source", arguments={"knowledge_base": "research", "url": "https://example.org/paper"}, answer="recorded, not done")]
        assert _answers(result.all_messages(), "request_source") == ["recorded, not done"]

    async def test_a_write_from_the_grounding_tools_is_checked_against_their_schema(self) -> None:
        calls: StandInCalls = []
        grounding = FunctionToolset[object]([skip_digest])
        refused = Agent(_calling("skip_digest", {"knowledge_base": "research", "page_id": "src.a"}), toolsets=[stand_in_toolset(FakeWritingSurface(), calls, grounding=grounding)])
        fitting = Agent(
            _calling("skip_digest", {"knowledge_base": "research", "page_id": "src.a", "reason": "a duplicate"}),
            toolsets=[stand_in_toolset(FakeWritingSurface(), calls, grounding=grounding)],
        )

        refusal = _answers((await refused.run("skip it")).all_messages(), "skip_digest")
        await fitting.run("skip it")

        assert "'reason' is a required property" in refusal[0]
        assert [call.tool for call in calls] == ["skip_digest"]


    async def test_a_resolve_entities_call_is_recorded_and_creates_no_page(self) -> None:
        surface = FakeWritingSurface()
        calls: StandInCalls = []
        arguments: dict[str, object] = {"knowledge_base": "research", "entities": [{"name": "Kestrel Labs", "entity_type": "org"}]}
        agent = Agent(_calling("resolve_entities", arguments), toolsets=[stand_in_toolset(surface, calls)])

        result = await agent.run("resolve the names")

        assert surface.ran == [], "the stand-in reached the real resolve_entities"
        assert [(call.tool, call.answer) for call in calls] == [("resolve_entities", "recorded, not done")]
        assert _answers(result.all_messages(), "resolve_entities") == ["recorded, not done"]

    def test_every_write_a_case_run_reaches_has_a_stand_in(self) -> None:
        toolset = stand_in_toolset(FakeWritingSurface(), [])

        assert set(toolset.tools) == {
            "write_note",
            "request_source",
            "decide_page",
            "skip_digest",
            "resolve_note",
            "remember_belief",
            "resolve_entities",
        }


class TestStandInCaller:
    async def test_a_write_goes_to_its_stand_in_and_a_read_goes_to_the_surface(self) -> None:
        surface = FakeWritingSurface()
        calls: StandInCalls = []
        call = stand_in_caller(surface, calls)

        written = await call("write_note", {"knowledge_base": "research", "payload": PAGE})
        found = await call("search", {"query": "kestrel"})

        assert written == f"checked, not written: {CHECKED}"
        assert found == "paper.one :: a hit"
        assert surface.ran == ["check_note", "search"]
        assert [call.tool for call in calls] == ["write_note"]


class Note(BaseModel):
    note: str


def _briefing(tools: tuple[str, ...]) -> Briefing:
    return Briefing(
        name="write_page",
        instructions="Write the page the question asks for into the research knowledge base and report its id.",
        output=Shape(json_schema={"type": "object", "properties": {"note": {"type": "string"}}, "required": ["note"]}),
        tools=tools,
    )


def _program_then_answer(program: str) -> Callable[[list[ModelMessage], AgentInfo], ModelResponse]:
    """A model that runs `program` in `run_code` once, then answers with a note."""

    def respond(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        ran = any(isinstance(part, ToolReturnPart) and part.tool_name == "run_code" for message in messages for part in message.parts)
        if not ran:
            return ModelResponse(parts=[ToolCallPart("run_code", {"code": program})])
        return ModelResponse(parts=[ToolCallPart(info.output_tools[0].name, {"note": "written"})])

    return respond


WRITING = "r = await write_note(knowledge_base='research', payload={'frontmatter': {'id': 'talk.a-talk'}})\nr"


@pytest.fixture
async def sandbox() -> AsyncIterator[AsyncMonty]:
    """A fresh worker pool, entered on the test's own event loop."""
    async with AsyncMonty(request_timeout=30.0) as pool:
        yield pool


OUTSIDE_THE_TABLE = ("remove_source_artifacts", "write_policy", "source_queue_decide", "run_command")
"""Tools that act and have no stand-in, so a case run never reaches them."""


class TestAStandInRegistryServesNoRealWrite:
    async def test_it_grants_only_the_tools_a_case_run_reaches_and_runs_no_agent_step(self, tmp_path: Path) -> None:
        model = streaming_function_model(_program_then_answer(WRITING))
        real = await world_over(model, engram=FakeWritingSurface(), state=tmp_path / "real")
        stood_in = await world_over(model, engram=FakeWritingSurface(), stand_ins=[], state=tmp_path / "stood-in")

        assert set(OUTSIDE_THE_TABLE) <= set(real.grants), "the surface no longer offers the tools this test is about"
        assert not set(OUTSIDE_THE_TABLE) & set(stood_in.grants)
        assert set(stood_in.grants) <= set(CASE_RUN_TOOLS)
        assert real.run_agent is not None
        assert stood_in.run_agent is None

    async def test_a_briefing_holding_the_shell_is_offered_no_shell(self, tmp_path: Path) -> None:
        offered: list[set[str]] = []

        def respond(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
            offered.append({tool.name for tool in info.function_tools})
            return ModelResponse(parts=[ToolCallPart(info.output_tools[0].name, {"note": "done"})])

        real = await world_over(streaming_function_model(respond), engram=FakeWritingSurface(), state=tmp_path / "real")
        stood_in = await world_over(streaming_function_model(respond), engram=FakeWritingSurface(), stand_ins=[], state=tmp_path / "stood-in")
        assert real.perform is not None
        assert stood_in.perform is not None

        await real.perform(_briefing(("run_command", "search")), Note, question="list the files")
        await stood_in.perform(_briefing(("run_command", "search")), Note, question="list the files")

        assert "run_command" in offered[0], "the shell is no longer offered on the real path this test compares against"
        assert "run_command" not in offered[1]

    async def test_a_program_reaches_no_tool_outside_the_table(self, tmp_path: Path, sandbox: AsyncMonty) -> None:
        surface = FakeWritingSurface()
        registry = await world_over(streaming_function_model(_program_then_answer(WRITING)), engram=surface, stand_ins=[], sandbox=sandbox, state=tmp_path)
        assert registry.run_code is not None
        program = Program(
            name="tidy", source="await write_policy(knowledge_base='research', rules={}, guidance='none')", output=Shape(json_schema={"type": "string"}), tools=("write_policy",)
        )

        with pytest.raises(RuntimeError, match="cannot reach"):
            await registry.run_code(program, str)

        assert surface.ran == []


class TestBuildEnvironmentMountsStandIns:
    async def test_a_briefing_granted_write_note_holds_the_stand_in_under_the_tools_own_name(self, tmp_path: Path) -> None:
        surface = FakeWritingSurface()
        calls: StandInCalls = []
        registry = await world_over(streaming_function_model(_program_then_answer(WRITING)), engram=surface, stand_ins=calls, state=tmp_path)
        assert registry.perform is not None

        await registry.perform(_briefing(("write_note",)), Note, question="write the talk page")

        assert surface.ran == ["check_note"], "the briefing reached the real write_note"
        assert [call.tool for call in calls] == ["write_note"]
        assert calls[0].arguments == {"knowledge_base": "research", "payload": {"frontmatter": {"id": "talk.a-talk"}}}

    async def test_without_stand_ins_a_briefing_granted_write_note_reaches_the_real_tool(self, tmp_path: Path) -> None:
        surface = FakeWritingSurface()
        registry = await world_over(streaming_function_model(_program_then_answer(WRITING)), engram=surface, state=tmp_path)
        assert registry.perform is not None

        await registry.perform(_briefing(("write_note",)), Note, question="write the talk page")

        assert surface.ran == ["write_note"]

    async def test_a_program_granted_write_note_calls_the_stand_in(self, tmp_path: Path, sandbox: AsyncMonty) -> None:
        surface = FakeWritingSurface()
        calls: StandInCalls = []
        registry = await world_over(streaming_function_model(_program_then_answer(WRITING)), engram=surface, stand_ins=calls, sandbox=sandbox, state=tmp_path)
        assert registry.run_code is not None
        program = Program(name="write_one", source=WRITING, output=Shape(json_schema={"type": "string"}), tools=("write_note",))

        answered = await registry.run_code(program, str)

        assert answered == f"checked, not written: {CHECKED}"
        assert surface.ran == ["check_note"], "the program reached the real write_note"
        assert [call.tool for call in calls] == ["write_note"]

    async def test_a_programs_write_the_schema_refuses_fails_the_program_naming_the_refusal(self, tmp_path: Path, sandbox: AsyncMonty) -> None:
        surface = FakeWritingSurface()
        calls: StandInCalls = []
        registry = await world_over(streaming_function_model(_program_then_answer(WRITING)), engram=surface, stand_ins=calls, sandbox=sandbox, state=tmp_path)
        assert registry.run_code is not None
        program = Program(
            name="queue_one", source="await request_source(knowledge_base='research')", output=Shape(json_schema={"type": "string"}), tools=("request_source",)
        )

        with pytest.raises(RuntimeError, match="'url' is a required property"):
            await registry.run_code(program, str)

        assert calls == []
        assert surface.ran == []

    async def test_a_briefing_granted_skip_digest_holds_the_stand_in_in_place_of_the_grounding_tool(self, tmp_path: Path) -> None:
        calls: StandInCalls = []
        skipping = "r = await skip_digest(knowledge_base='research', page_id='src.a', reason='a duplicate')\nr"
        registry = await world_over(
            streaming_function_model(_program_then_answer(skipping)), engram=FakeWritingSurface(), plugins=fake_plugins(), stand_ins=calls, state=tmp_path
        )
        assert registry.perform is not None

        await registry.perform(_briefing(("skip_digest",)), Note, question="skip the duplicate source")

        assert calls == [StandInCall(tool="skip_digest", arguments={"knowledge_base": "research", "page_id": "src.a", "reason": "a duplicate"}, answer="recorded, not done")]
