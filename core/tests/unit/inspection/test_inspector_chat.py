"""The follow-up: the same instructions, the same four tools, the conversation it already has, an answer in words."""

from __future__ import annotations

import ast
import re

from typing import TYPE_CHECKING
from uuid import UUID

import pytest

from pydantic_ai.messages import ModelMessage, ModelRequest, ModelResponse, TextPart, ToolCallPart, ToolReturnPart, UserPromptPart
from pydantic_ai.models import ModelRequestParameters
from pydantic_ai.models.function import AgentInfo, FunctionModel
from pydantic_ai.native_tools import WebSearchTool
from pydantic_ai.tools import ToolDefinition

from core.ai.inspection.inspector import INSPECTOR_INSTRUCTIONS, InspectorDeps, ask, inspector_chat_agent
from core.ai.transcripts.contracts import RunSummary, Step, Transcript, TranscriptLoaders
from tests.support.fakes.inspection import AT, FakeRunsOfConversation, Opens, answer, call, question, result, transcript

if TYPE_CHECKING:
    from pydantic import JsonValue


def next_reading_call(body: str) -> ToolCallPart | None:
    next_call = re.search(r"Continue with ((?:read|outline|earlier_runs)\(.*\))", body)
    if next_call is None:
        return None
    parsed = ast.parse(next_call[1], mode="eval").body
    assert isinstance(parsed, ast.Call)
    assert isinstance(parsed.func, ast.Name)
    arguments: dict[str, JsonValue] = {}
    for keyword in parsed.keywords:
        assert keyword.arg is not None
        arguments[keyword.arg] = ast.literal_eval(keyword.value)
    return ToolCallPart(tool_name=parsed.func.id, args=arguments)


A_CONVERSATION = UUID("33333333-3333-4333-8333-333333333333")
A_DIFFERENT_CONVERSATION = UUID("44444444-4444-4444-8444-444444444444")
EARLIER: list[ModelMessage] = [
    ModelRequest(parts=[UserPromptPart(content="Symptom: warning_ignored.", timestamp=AT)], conversation_id=str(A_DIFFERENT_CONVERSATION)),
    ModelResponse(parts=[TextPart(content="I blamed the tool description.")], timestamp=AT, conversation_id=str(A_DIFFERENT_CONVERSATION)),
]


def a_run():
    return transcript(question(), call("c1"), result("c1", "the page already exists"), answer("done"))


def deps_for(given) -> InspectorDeps:
    return InspectorDeps(
        transcript=given, loaders=TranscriptLoaders(by_key={"test": Opens(run=given)}), conversation=FakeRunsOfConversation()
    )


class RemembersWhatItWasHanded:
    """A model function that answers text and keeps every message and the conversation id stamped on the request `ask` built this turn."""

    def __init__(self, said: str) -> None:
        self.said = said
        self.seen: list[ModelMessage] = []
        self.conversations: list[str | None] = []
        self.tools: list[str] = []
        self.instructions: str | None = None

    def __call__(self, messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        self.seen = list(messages)
        self.conversations.append(messages[-1].conversation_id)
        self.tools = [tool.name for tool in info.function_tools]
        self.instructions = info.instructions
        return ModelResponse(parts=[TextPart(content=self.said)])


async def test_the_follow_up_is_handed_the_recorded_conversation_and_answers_words() -> None:
    given = a_run()
    watcher = RemembersWhatItWasHanded("Because the description promised a check the tool never made.")
    agent = inspector_chat_agent(FunctionModel(watcher, model_name="chatty"))

    said = await ask(agent, "why the tool description?", deps=deps_for(given), history=EARLIER, conversation_id=A_CONVERSATION)

    assert said == "Because the description promised a check the tool never made."
    assert watcher.conversations == [str(A_CONVERSATION)]
    texts = [part.content for message in watcher.seen for part in message.parts if isinstance(part, TextPart)]
    assert "I blamed the tool description." in texts
    prompts = [part.content for message in watcher.seen for part in message.parts if isinstance(part, UserPromptPart)]
    assert prompts[-1] == "why the tool description?"


async def test_the_follow_up_keeps_the_inspectors_instructions_and_its_four_tools() -> None:
    given = a_run()
    watcher = RemembersWhatItWasHanded("looked again")
    agent = inspector_chat_agent(FunctionModel(watcher, model_name="chatty"))

    await ask(agent, "look again", deps=deps_for(given), history=[], conversation_id=A_CONVERSATION)

    assert set(watcher.tools) == {"outline", "read", "find", "earlier_runs"}
    assert watcher.instructions == INSPECTOR_INSTRUCTIONS


class ReadsBeforeAnswering:
    """A model function that calls `find` on its first turn and answers with what the tool told it on its second."""

    def __init__(self) -> None:
        self.called = False

    def __call__(self, messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        del info
        if not self.called:
            self.called = True
            return ModelResponse(parts=[ToolCallPart(tool_name="find", args={"text": "already exists"})])
        returned = next(
            part for message in messages for part in message.parts if isinstance(part, ToolReturnPart) and part.tool_name == "find"
        )
        return ModelResponse(parts=[TextPart(content=str(returned.content))])


async def test_the_follow_up_still_reaches_the_run_it_is_asked_about() -> None:
    given = a_run()
    reader = ReadsBeforeAnswering()
    agent = inspector_chat_agent(FunctionModel(reader, model_name="chatty"))

    said = await ask(agent, "where does it say that?", deps=deps_for(given), history=[], conversation_id=A_CONVERSATION)

    assert reader.called
    assert "0.2.0" in said


async def test_actual_tools_find_and_read_every_recorded_resource_and_follow_continuations() -> None:
    given = Transcript(
        source="test:run",
        steps=(
            Step(
                messages=(
                    ModelRequest(parts=[UserPromptPart(content="p" * 2500 + "PART-END")], instructions="i" * 2500 + "INSTRUCTION-END"),
                ),
                ordinals=(0,),
                parameters=ModelRequestParameters(
                    function_tools=[
                        ToolDefinition(
                            name="lookup",
                            description="s" * 2500 + "SCHEMA-END",
                            parameters_json_schema={
                                "type": "object",
                                "properties": {"key": {"type": "string", "description": "SCHEMA-KEY"}},
                            },
                        )
                    ],
                    output_mode="tool",
                    output_tools=[
                        ToolDefinition(
                            name="final_result",
                            description="o" * 2500 + "OUTPUT-SCHEMA-END",
                            parameters_json_schema={"type": "object", "properties": {"verdict": {"const": "OUTPUT-VERDICT"}}},
                        )
                    ],
                    native_tools=[WebSearchTool(allowed_domains=["native-marker.example"])],
                ),
            ),
        ),
    )
    markers = ("PART-END", "INSTRUCTION-END", "SCHEMA-KEY", "OUTPUT-SCHEMA-END", "OUTPUT-VERDICT", "native-marker.example")
    pending = [ToolCallPart(tool_name="find", args={"text": marker}) for marker in markers]
    pending.extend(
        ToolCallPart(tool_name="read", args={"step": 0, "resource": resource}) for resource in ("messages", "instructions", "tools")
    )
    returned: list[str] = []
    read_pages: list[str] = []

    def reads_all(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        if messages and isinstance(messages[-1], ModelRequest):
            for part in messages[-1].parts:
                if not isinstance(part, ToolReturnPart):
                    continue
                body = str(part.content)
                returned.append(body)
                if part.tool_name == "read":
                    read_pages.append(body)
                next_call = next_reading_call(body)
                if next_call is not None:
                    pending.insert(0, next_call)
        if pending:
            return ModelResponse(parts=[pending.pop(0)])
        return ModelResponse(parts=[TextPart(content="read complete")])

    agent = inspector_chat_agent(FunctionModel(reads_all))
    said = await ask(agent, "Read the recorded evidence", deps=deps_for(given), history=[], conversation_id=A_CONVERSATION)

    assert said == "read complete"
    assert len(returned) >= 9
    for marker in (*markers, "SCHEMA-END"):
        assert any(marker in body for body in read_pages)
    assert all(len(body) < 2500 for body in returned)
    assert all("nothing holds" not in body for body in returned[: len(markers)])


async def test_actual_outline_and_earlier_runs_tools_follow_their_bounded_pages() -> None:
    given = transcript(*(answer("x" * 190) for _ in range(150)), answer("OUTLINE-END"))
    summaries = [
        RunSummary(source=f"test:earlier-{index}", agent="helper", started_at=AT, first_prompt="x" * 190, last_event_type="run_completed")
        for index in range(150)
    ]
    deps = InspectorDeps(
        transcript=given,
        loaders=TranscriptLoaders(by_key={"test": Opens(run=given)}),
        conversation=FakeRunsOfConversation(by_source={given.source: summaries}),
    )
    pending = [ToolCallPart(tool_name="outline", args={}), ToolCallPart(tool_name="earlier_runs", args={})]
    pages: list[str] = []

    def list_all(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        schemas = {tool.name: tool.parameters_json_schema for tool in info.function_tools}
        assert set(schemas) == {"outline", "read", "find", "earlier_runs"}
        for schema in schemas.values():
            assert schema["properties"]["offset"]["type"] == "integer"
            assert "ctx" not in schema["properties"]
        assert schemas["read"]["properties"]["resource"]["enum"] == ["messages", "instructions", "tools"]
        for part in messages[-1].parts:
            if isinstance(part, ToolReturnPart):
                pages.append(str(part.content))
                next_call = next_reading_call(str(part.content))
                if next_call is not None:
                    pending.insert(0, next_call)
        return ModelResponse(parts=[pending.pop(0)] if pending else [TextPart(content="listed")])

    assert (
        await ask(inspector_chat_agent(FunctionModel(list_all)), "List all", deps=deps, history=[], conversation_id=A_CONVERSATION)
        == "listed"
    )
    assert all(len(page) < 20200 for page in pages)
    assert any("OUTLINE-END" in page for page in pages)
    assert any("test:earlier-149" in page for page in pages)


@pytest.mark.parametrize("same_conversation", [True, False])
async def test_other_runs_are_read_only_when_listed_in_this_conversation(same_conversation: bool) -> None:
    given = a_run()
    other = transcript(question("OTHER-RUN-EVIDENCE"), source="test:other")
    summary = RunSummary(source=other.source, agent="helper", started_at=AT, first_prompt="q", last_event_type="run_completed")
    deps = InspectorDeps(
        transcript=given,
        loaders=TranscriptLoaders(by_key={"test": Opens(run=given, other=other)}),
        conversation=FakeRunsOfConversation(by_source={given.source: [summary] if same_conversation else []}),
    )

    def read_other(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        for part in messages[-1].parts:
            if isinstance(part, ToolReturnPart):
                return ModelResponse(parts=[TextPart(content=str(part.content))])
        return ModelResponse(parts=[ToolCallPart(tool_name="read", args={"step": 0, "run": "test:other"})])

    said = await ask(inspector_chat_agent(FunctionModel(read_other)), "Read other", deps=deps, history=[], conversation_id=A_CONVERSATION)

    assert ("OTHER-RUN-EVIDENCE" in said) is same_conversation
    if not same_conversation:
        assert "earlier_runs" in said
