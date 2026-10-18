"""The inspector: what it is asked, what it may answer, and how an answer becomes a finding."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from uuid import UUID

import pytest

from pydantic_ai.messages import ModelMessage, ModelResponse, ToolCallPart, ToolReturnPart, UserPromptPart
from pydantic_ai.models.function import AgentInfo, FunctionModel
from pydantic_ai.models.test import TestModel
from pydantic_ai_harness.step_persistence import InMemoryStepStore, StepPersistence

from core.ai.inspection.domain.leads import Lead
from core.ai.inspection.domain.taxonomy import SurfaceKind
from core.ai.inspection.inspector import (
    INSPECTOR_INSTRUCTIONS,
    Attribution,
    BlameNames,
    InspectorDeps,
    NoIssue,
    Undecided,
    attribute,
    inspector_agent,
    resolve,
)
from core.ai.inspection.scan_run import SCAN_INSTRUCTIONS
from core.ai.transcripts.contracts import RunSummary, Span, TranscriptLoaders
from tests.support.fakes.inspection import FakeRunsOfConversation, Opens, answer, call, question, result, transcript
from tests.unit.inspection.test_inspect_run import A_TAXONOMY

A_LEAD = Lead(
    source="test:run",
    symptom="retried_after_clear_error",
    spans=(Span(step=0, message=1, part=0), Span(step=0, message=3, part=0)),
    found_by="retried_after_clear_error",
)
A_CONVERSATION = UUID("11111111-1111-4111-8111-111111111111")
DECIDED_ARGS = {
    "symptom": "retried_after_clear_error",
    "blames": [{"root_cause": "agent_reasoning", "surface_kind": "model_tier", "surface_name": "worker", "why": "it repeated the call"}],
    "explanation": "The agent saw the same failure twice. It retried the call instead of stopping. Stop on a clear error.",
}


def a_transcript():
    return transcript(
        question(),
        call("c1"),
        result("c1", "the knowledge base declares no page type for videos", outcome="failed"),
        call("c2"),
        result("c2", "same", outcome="failed"),
        answer(),
    )


def deps_for(given, *, earlier: list[RunSummary] | None = None) -> InspectorDeps:
    """Deps over one transcript, a loader that can reopen it, and the conversation's earlier runs, scripted by the test."""
    return InspectorDeps(
        transcript=given,
        loaders=TranscriptLoaders(by_key={"test": Opens(run=given)}),
        conversation=FakeRunsOfConversation(by_source={given.source: earlier or []}),
    )


def test_resolve_turns_names_into_a_finding_the_taxonomy_accepts() -> None:
    given = Attribution(
        symptom="retried_after_clear_error",
        blames=[
            BlameNames(
                root_cause="instructions_say_nothing",
                surface_kind=SurfaceKind.SKILL,
                surface_name="digesting",
                why="nothing says to stop on a refusal",
            )
        ],
        explanation="The agent saw a clear refusal. It retried the same call anyway. Tell it to stop on a refusal.",
    )

    finding = resolve(given, spans=A_LEAD.spans)

    assert finding.symptom.name == "retried_after_clear_error"
    assert finding.blames[0].what.name == "instructions_say_nothing"
    assert finding.blames[0].where.name == "digesting"
    assert finding.found_by == "inspector"


def test_resolve_carries_the_explanation() -> None:
    given = Attribution(
        symptom="retried_after_clear_error",
        blames=[BlameNames(root_cause="agent_reasoning", surface_kind=SurfaceKind.MODEL_TIER, surface_name="worker", why="it retried")],
        explanation="The agent saw a clear refusal. It retried the same call anyway. Tell it to stop on a refusal.",
    )

    finding = resolve(given, spans=A_LEAD.spans)

    assert finding.explanation == "The agent saw a clear refusal. It retried the same call anyway. Tell it to stop on a refusal."


def test_resolve_refuses_an_unknown_root_cause_name() -> None:
    given = Attribution(
        symptom="retried_after_clear_error",
        blames=[BlameNames(root_cause="cosmic_rays", surface_kind=SurfaceKind.SKILL, why="x")],
        explanation="x" * 10,
    )

    with pytest.raises(ValueError, match="cosmic_rays"):
        resolve(given, spans=A_LEAD.spans)


def test_resolve_refuses_a_blame_the_symptom_does_not_allow() -> None:
    given = Attribution(
        symptom="retried_after_clear_error",
        blames=[BlameNames(root_cause="tool_schema", surface_kind=SurfaceKind.TOOL, surface_name="skeleton", why="x")],
        explanation="x" * 10,
    )

    with pytest.raises(ValueError, match="cannot be behind"):
        resolve(given, spans=A_LEAD.spans)


async def test_attribute_returns_the_finding_the_model_named() -> None:
    model = TestModel(
        custom_output_args={
            "symptom": "retried_after_clear_error",
            "blames": [
                {
                    "root_cause": "agent_reasoning",
                    "surface_kind": "model_tier",
                    "surface_name": "worker",
                    "why": "the refusal named the fix",
                }
            ],
            "explanation": "The agent saw the refusal name the fix. It retried the call instead. Read the refusal before retrying.",
        }
    )

    given = a_transcript()
    outcome = await attribute(inspector_agent(model), A_LEAD, deps=deps_for(given), conversation_id=A_CONVERSATION)

    assert not isinstance(outcome, Undecided | NoIssue)
    assert outcome.blames[0].what.name == "agent_reasoning"


async def test_attribute_passes_an_undecided_answer_through() -> None:
    def undecided(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        tool = next(tool for tool in info.output_tools if "Undecided" in tool.name)
        return ModelResponse(parts=[ToolCallPart(tool_name=tool.name, args={"reason": "the window does not show the instructions"})])

    given = a_transcript()
    outcome = await attribute(inspector_agent(FunctionModel(undecided)), A_LEAD, deps=deps_for(given), conversation_id=A_CONVERSATION)

    assert isinstance(outcome, Undecided)
    assert "instructions" in outcome.reason


async def test_output_validation_uses_the_callers_custom_taxonomy() -> None:
    agent = inspector_agent(TestModel(custom_output_args={
        "symptom": "improvised_page",
        "blames": [{"root_cause": "hand_written_page", "surface_kind": "tool", "surface_name": "writer", "why": "It wrote by hand."}],
        "explanation": "The agent wrote the page by hand.",
    }))

    outcome = await attribute(agent, A_LEAD, deps=deps_for(a_transcript()), conversation_id=A_CONVERSATION, taxonomy=A_TAXONOMY)

    assert not isinstance(outcome, NoIssue | Undecided)
    assert outcome.symptom.name == "improvised_page"


async def test_attribute_can_explicitly_clear_a_false_positive_without_calling_it_uncertain() -> None:
    def no_issue(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        names = [tool.name for tool in info.output_tools]
        assert any(name.endswith("NoIssue") for name in names)
        tool = next(tool for tool in info.output_tools if tool.name.endswith("NoIssue"))
        return ModelResponse(
            parts=[ToolCallPart(tool_name=tool.name, args={"reason": "The second call follows corrected input and succeeds."})]
        )

    outcome = await attribute(
        inspector_agent(FunctionModel(no_issue)), A_LEAD, deps=deps_for(a_transcript()), conversation_id=A_CONVERSATION
    )

    assert isinstance(outcome, NoIssue)
    assert outcome.reason == "The second call follows corrected input and succeeds."
    assert not isinstance(outcome, Undecided)


class ReadsThenAnswers:
    """A model function that calls `read` on its first turn and, once it sees the tool's own answer, answers the attribution."""

    def __init__(self) -> None:
        self.tools_called: list[str] = []

    def __call__(self, messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        if not self.tools_called:
            self.tools_called.append("read")
            return ModelResponse(parts=[ToolCallPart(tool_name="read", args={"step": 0, "start": 0, "end": 3})])
        returned = next(
            part for message in messages for part in message.parts if isinstance(part, ToolReturnPart) and part.tool_name == "read"
        )
        assert "0.0.0" in str(returned.content)
        tool = next(tool for tool in info.output_tools if tool.name.endswith("Attribution"))
        return ModelResponse(
            parts=[
                ToolCallPart(
                    tool_name=tool.name,
                    args={
                        "symptom": "retried_after_clear_error",
                        "blames": [
                            {
                                "root_cause": "agent_reasoning",
                                "surface_kind": "model_tier",
                                "surface_name": "worker",
                                "why": "it repeated the call",
                            }
                        ],
                        "explanation": "The agent saw the same error twice. It repeated the call instead of stopping. Stop after one retry.",
                    },
                )
            ]
        )


async def test_the_inspector_can_read_the_run_before_it_answers() -> None:
    given = a_transcript()
    reader = ReadsThenAnswers()

    outcome = await attribute(
        inspector_agent(FunctionModel(reader, model_name="reader")), A_LEAD, deps=deps_for(given), conversation_id=A_CONVERSATION
    )

    assert reader.tools_called == ["read"]
    assert not isinstance(outcome, Undecided | NoIssue)
    assert outcome.blames[0].what.name == "agent_reasoning"


class AsksAboutARunTheLoadersDoNotKnow:
    """A model function that calls `outline` on a run the loaders cannot open, then answers with what it was told."""

    def __init__(self) -> None:
        self.called = False

    def __call__(self, messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        if not self.called:
            self.called = True
            return ModelResponse(parts=[ToolCallPart(tool_name="outline", args={"run": "test:no-such-run"})])
        returned = next(
            part for message in messages for part in message.parts if isinstance(part, ToolReturnPart) and part.tool_name == "outline"
        )
        tool = next(tool for tool in info.output_tools if tool.name.endswith("Undecided"))
        return ModelResponse(parts=[ToolCallPart(tool_name=tool.name, args={"reason": str(returned.content)})])


async def test_a_run_the_loaders_cannot_open_answers_the_model_in_words() -> None:
    given = a_transcript()
    asker = AsksAboutARunTheLoadersDoNotKnow()

    outcome = await attribute(
        inspector_agent(FunctionModel(asker, model_name="a")), A_LEAD, deps=deps_for(given), conversation_id=A_CONVERSATION
    )

    assert isinstance(outcome, Undecided)
    assert "test:no-such-run" in outcome.reason


class AsksAboutARunWithNoScheme:
    """A model function that calls `outline` on a source with no scheme, then answers with what it was told."""

    def __init__(self) -> None:
        self.called = False

    def __call__(self, messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        if not self.called:
            self.called = True
            return ModelResponse(parts=[ToolCallPart(tool_name="outline", args={"run": "nonsense"})])
        returned = next(
            part for message in messages for part in message.parts if isinstance(part, ToolReturnPart) and part.tool_name == "outline"
        )
        tool = next(tool for tool in info.output_tools if tool.name.endswith("Undecided"))
        return ModelResponse(parts=[ToolCallPart(tool_name=tool.name, args={"reason": str(returned.content)})])


async def test_a_run_with_no_scheme_answers_the_model_in_words_instead_of_crashing() -> None:
    given = a_transcript()
    asker = AsksAboutARunWithNoScheme()

    outcome = await attribute(
        inspector_agent(FunctionModel(asker, model_name="a")), A_LEAD, deps=deps_for(given), conversation_id=A_CONVERSATION
    )

    assert isinstance(outcome, Undecided)
    assert "nonsense" in outcome.reason


class AsksForAStepTheRunDoesNotHave:
    """A model function that calls `read` on a step the run does not have, then answers with what it was told."""

    def __init__(self) -> None:
        self.called = False

    def __call__(self, messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        if not self.called:
            self.called = True
            return ModelResponse(parts=[ToolCallPart(tool_name="read", args={"step": 99, "start": 0, "end": 3})])
        returned = next(
            part for message in messages for part in message.parts if isinstance(part, ToolReturnPart) and part.tool_name == "read"
        )
        tool = next(tool for tool in info.output_tools if tool.name.endswith("Undecided"))
        return ModelResponse(parts=[ToolCallPart(tool_name=tool.name, args={"reason": str(returned.content)})])


async def test_a_step_the_run_does_not_have_answers_the_model_in_words_instead_of_crashing() -> None:
    given = a_transcript()
    asker = AsksForAStepTheRunDoesNotHave()

    outcome = await attribute(
        inspector_agent(FunctionModel(asker, model_name="a")), A_LEAD, deps=deps_for(given), conversation_id=A_CONVERSATION
    )

    assert isinstance(outcome, Undecided)
    assert "no step 99" in outcome.reason


class RemembersThePrompt:
    """A model function that answers undecided and keeps the first thing it was asked."""

    def __init__(self) -> None:
        self.prompt = ""

    def __call__(self, messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        first = messages[0].parts[0]
        self.prompt = first.content if isinstance(first, UserPromptPart) and isinstance(first.content, str) else ""
        tool = next(tool for tool in info.output_tools if tool.name.endswith("Undecided"))
        return ModelResponse(parts=[ToolCallPart(tool_name=tool.name, args={"reason": "not looking"})])


@dataclass
class RecordsConversationCalls:
    """A `RunsOfConversation` that records every `before()` call it answers, by the source it was asked about."""

    by_source: dict[str, list[RunSummary]] = field(default_factory=dict)
    calls: list[str] = field(default_factory=list)

    async def before(self, source: str) -> list[RunSummary]:
        self.calls.append(source)
        return self.by_source.get(source, [])


async def test_the_leads_horizon_chooses_the_view_the_inspector_opens_on() -> None:
    given = a_transcript()
    earlier = [
        RunSummary(
            source="record:run-0",
            agent="harness-author",
            started_at=datetime(2026, 9, 9, 9, 0, tzinfo=UTC),
            first_prompt="index the notes",
            last_event_type="run_completed",
        )
    ]
    of_the_conversation = Lead(
        source="test:run", symptom="call_for_known_fact", spans=(Span(step=0, message=1, part=0),), found_by="inspector"
    )
    watcher, other = RemembersThePrompt(), RemembersThePrompt()
    loaders = TranscriptLoaders(by_key={"test": Opens(run=given)})
    on_the_moment = RecordsConversationCalls()
    reaching_back = RecordsConversationCalls(by_source={given.source: earlier})

    await attribute(
        inspector_agent(FunctionModel(watcher, model_name="w")),
        A_LEAD,
        deps=InspectorDeps(transcript=given, loaders=loaders, conversation=on_the_moment),
        conversation_id=A_CONVERSATION,
    )
    await attribute(
        inspector_agent(FunctionModel(other, model_name="o")),
        of_the_conversation,
        deps=InspectorDeps(transcript=given, loaders=loaders, conversation=reaching_back),
        conversation_id=A_CONVERSATION,
    )

    assert "The run, one line per step:" in watcher.prompt
    assert "Earlier runs of this conversation" not in watcher.prompt
    assert on_the_moment.calls == []
    assert "Earlier runs of this conversation" in other.prompt
    assert "record:run-0" in other.prompt
    assert reaching_back.calls == [given.source]


class NamesTheTools:
    """A model function that records the tools it was offered and answers undecided."""

    def __init__(self) -> None:
        self.tools: list[str] = []

    def __call__(self, messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        del messages
        self.tools = [tool.name for tool in info.function_tools]
        tool = next(tool for tool in info.output_tools if tool.name.endswith("Undecided"))
        return ModelResponse(parts=[ToolCallPart(tool_name=tool.name, args={"reason": "just looking"})])


async def test_the_four_tools_are_the_inspectors_reach() -> None:
    given = a_transcript()
    watcher = NamesTheTools()

    await attribute(inspector_agent(FunctionModel(watcher, model_name="w")), A_LEAD, deps=deps_for(given), conversation_id=A_CONVERSATION)

    assert set(watcher.tools) == {"outline", "read", "find", "earlier_runs"}


class RemembersTheConversation:
    """A model function that answers undecided and keeps the conversation id of every request it was sent."""

    def __init__(self) -> None:
        self.conversations: list[str | None] = []

    def __call__(self, messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        self.conversations.append(messages[0].conversation_id)
        tool = next(tool for tool in info.output_tools if tool.name.endswith("Undecided"))
        return ModelResponse(parts=[ToolCallPart(tool_name=tool.name, args={"reason": "just looking"})])


async def test_the_question_is_asked_under_the_conversation_its_caller_minted() -> None:
    given = a_transcript()
    watcher = RemembersTheConversation()

    await attribute(inspector_agent(FunctionModel(watcher, model_name="w")), A_LEAD, deps=deps_for(given), conversation_id=A_CONVERSATION)

    assert watcher.conversations == [str(A_CONVERSATION)]


async def test_a_recording_capability_registers_the_question_as_a_run_of_that_conversation() -> None:
    given = a_transcript()
    store = InMemoryStepStore()
    agent = inspector_agent(
        TestModel(custom_output_args=DECIDED_ARGS), capabilities=[StepPersistence[InspectorDeps](store=store, agent_name="inspector")]
    )

    await attribute(agent, A_LEAD, deps=deps_for(given), conversation_id=A_CONVERSATION)

    (run,) = await store.list_runs(conversation_id=str(A_CONVERSATION))
    assert run.agent_name == "inspector"
    assert run.parent_run_id is None


def test_both_sets_of_instructions_hold_the_sentence_that_keeps_a_blame_off_the_instructions() -> None:
    sentence = "Blame the instructions only when nothing the agent could see already told it what to do."

    assert sentence in INSPECTOR_INSTRUCTIONS
    assert sentence in SCAN_INSTRUCTIONS
