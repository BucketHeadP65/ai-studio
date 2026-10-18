"""A briefing turn resumes its item's conversation: `run_defined_step`'s turn arm.

Every test drives a three-turn map (triage, then draft from the triage, then polish the draft)
through `build_environment`'s own `run_defined_step`, exactly as the indexer runs it -- `FunctionModel`
stands in for the provider, recording each request's messages, instructions and tool surface, and
answering each turn with that turn's own output tool.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from typing import TYPE_CHECKING, Literal
from uuid import UUID

import pytest

from core.ai.history.contracts import ContinuationAttempt, ContinuationCommit, ContinuationInFlightError, ContinuationRef
from core.ai.workflows.contracts import (
    AgentStep,
    Briefing,
    FromNode,
    FromParameter,
    MapNode,
    ParameterSpec,
    RunNode,
    Shape,
    StepConversationContinuation,
    StepIdentity,
    TurnFacts,
    Workflow,
    WorkflowArguments,
    performing_step,
    run_workflow,
)
from core.ai.workflows.domain.vocabulary import SameAs
from core.ai.workflows.infrastructure.temporal import continuing_step_conversation
from pydantic_ai import UnexpectedModelBehavior
from pydantic_ai.messages import ModelRequest, ModelResponse, RetryPromptPart, TextPart, ToolCallPart, ToolReturnPart, UserPromptPart
from pydantic_ai.usage import RequestUsage
from pydantic_ai_harness.step_persistence import ContinuableSnapshot
from pydantic_ai_harness.warn_on_cache_busts import CacheBustWarning

from harness.adapters import model_tiers
from harness.adapters.model_list import ModelList
from harness.adapters.model_tiers import TierSetting
from harness.composition import MissingTurnLineError, _shape_name, build_environment
from harness.features.running.infrastructure.durable import DurableTurns
from harness.session import ActiveRun, NoSessionLog
from harness.settings import WebSearchSettings
from tests.conftest import ApproveAll
from tests.support.environment import fake_engram
from tests.support.fakes.model_listing import FakeModelListing
from tests.support.fakes.step_store import FakeContinuationCheckpoints, FakeStepStore
from tests.support.state import records_under
from tests.support.streaming import streaming_function_model, usage_preserving_function_model
from tests.support.tiers import tiers_under

if TYPE_CHECKING:
    from collections.abc import Sequence
    from pathlib import Path

    from core.ai.workflows.contracts import Tier
    from core.thinking import Thinking
    from pydantic_ai.messages import ModelMessage
    from pydantic_ai.models.function import AgentInfo, FunctionModel
    from pydantic_ai.tools import ToolDefinition

    from harness.adapters.model_tiers import ModelTiers
    from harness.features.running.conversations import StepConversation

ITEM_SCHEMA = {"type": "object", "title": "Digest", "properties": {"title": {"type": "string"}}, "required": ["title"]}
TRIAGE_SCHEMA = {"type": "object", "title": "Triage", "properties": {"note": {"type": "string"}}, "required": ["note"]}
DRAFT_SCHEMA = {"type": "object", "title": "Draft", "properties": {"text": {"type": "string"}}, "required": ["text"]}
POLISH_SCHEMA = {"type": "object", "title": "Polish", "properties": {"summary": {"type": "string"}}, "required": ["summary"]}

TRIAGE_INSTRUCTIONS = "Read the item and write one short triage note about it, nothing more."
DRAFT_INSTRUCTIONS = "Using your triage note, write a short draft about the item."
POLISH_INSTRUCTIONS = "Polish the draft into its final form, ready to publish."

_PAYLOADS: tuple[dict[str, str], ...] = ({"note": "worth a look"}, {"text": "a short draft"}, {"summary": "final form"})


def _sequence_tasks(
    *, polish_tools: tuple[str, ...] = (), skills: tuple[tuple[str, ...], tuple[str, ...], tuple[str, ...]] = ((), (), ()), tier: Tier | None = None
) -> tuple[Briefing, Briefing, Briefing]:
    """Three briefings mirroring a digest pipeline: triage the item, draft from the triage, polish the draft.

    `skills` is one name tuple per turn, in turn order, and `tier` is the middle turn's own tier --
    what a turn declares for itself, so the sequence's surface can be told from any single turn's.
    """
    triage = Briefing(
        name="triage",
        instructions=TRIAGE_INSTRUCTIONS,
        inputs={"item": Shape(json_schema=ITEM_SCHEMA)},
        output=Shape(json_schema=TRIAGE_SCHEMA),
        skills=skills[0],
    )
    draft = Briefing(
        name="draft",
        instructions=DRAFT_INSTRUCTIONS,
        inputs={"triage": SameAs(like="triage")},
        output=Shape(json_schema=DRAFT_SCHEMA),
        skills=skills[1],
        tier=tier,
    )
    polish = Briefing(
        name="polish",
        instructions=POLISH_INSTRUCTIONS,
        inputs={"draft": SameAs(like="draft")},
        output=Shape(json_schema=POLISH_SCHEMA),
        tools=polish_tools,
        skills=skills[2],
    )
    return triage, draft, polish


def _sequence_spec(
    *,
    on_error: Literal["fail", "skip"] = "fail",
    polish_tools: tuple[str, ...] = (),
    skills: tuple[tuple[str, ...], tuple[str, ...], tuple[str, ...]] = ((), (), ()),
    node_tier: Tier | None = None,
    task_tier: Tier | None = None,
) -> Workflow:
    """One map, three turns per item -- one conversation carrying the item through triage, draft and polish."""
    triage, draft, polish = _sequence_tasks(polish_tools=polish_tools, skills=skills, tier=task_tier)
    return Workflow(
        name="digest-sequence",
        version=1,
        description="One worker takes an item through triage, draft and polish as one conversation.",
        parameters={"items": ParameterSpec(description="items to digest", json_schema={"type": "array", "items": ITEM_SCHEMA})},
        inline=(triage, draft, polish),
        nodes=(
            MapNode(
                id="digest_each",
                turns=("triage", "draft", "polish"),
                over=FromParameter(parameter="items"),
                each="item",
                on_error=on_error,
                tier=node_tier,
            ),
        ),
        output=FromNode(node="digest_each"),
    )


def _plain_spec() -> Workflow:
    """One briefing run once, outside any map -- the untouched no-turn arm."""
    triage, _draft, _polish = _sequence_tasks()
    return Workflow(
        name="plain-triage",
        version=1,
        description="One briefing run once, no map, no turn identity.",
        parameters={"item": ParameterSpec(description="the item", json_schema=ITEM_SCHEMA)},
        inline=(triage,),
        nodes=(RunNode(id="triage", work="triage", inputs={"item": FromParameter(parameter="item")}),),
        output=FromNode(node="triage"),
    )


def _worker_with_read_spec() -> Workflow:
    """One worker step granted a real read -- its one conversation takes two model requests: the read, then the answer."""
    worker = AgentStep(
        name="triage",
        agent={"instructions": TRIAGE_INSTRUCTIONS},
        inputs={"item": Shape(json_schema=ITEM_SCHEMA)},
        output=Shape(json_schema=TRIAGE_SCHEMA),
        tools=("search",),
    )
    return Workflow(
        name="worker-with-read",
        version=1,
        description="One worker step reads before it answers, all inside its one run.",
        parameters={"item": ParameterSpec(description="the item", json_schema=ITEM_SCHEMA)},
        inline=(worker,),
        nodes=(RunNode(id="triage", work="triage", inputs={"item": FromParameter(parameter="item")}),),
        output=FromNode(node="triage"),
    )


def _turn_index(messages: list[ModelMessage]) -> int:
    """How many user turns this request carries, zero-based -- the first turn of a conversation is 0."""
    return sum(1 for message in messages for part in message.parts if isinstance(part, UserPromptPart)) - 1


def _last_user_prompt(messages: list[ModelMessage]) -> str:
    """The newest user prompt this request carries -- this turn's own words, last in the history."""
    content = next(part.content for part in messages[-1].parts if isinstance(part, UserPromptPart))
    assert isinstance(content, str)
    return content


def _on_the_wire(tools: Sequence[ToolDefinition]) -> list[ToolDefinition]:
    """`tools` as a provider receives them: the run-local handle of the capability that contributed each is dropped.

    An unnamed capability gets a fresh handle every run, and no model request carries it, so comparing it
    across turns would compare something the provider never sees.
    """
    return [replace(tool, capability_id=None) for tool in tools]


@dataclass
class _Rendered:
    """Every request one run put on the wire, as the provider was actually asked it."""

    messages: list[list[ModelMessage]] = field(default_factory=list)
    instructions: list[str | None] = field(default_factory=list)
    function_tools: list[list[ToolDefinition]] = field(default_factory=list)
    output_tools: list[list[ToolDefinition]] = field(default_factory=list)
    answered_by: list[str] = field(default_factory=list)
    """Which tier's model answered each request, by the label the build seam gave it."""

    def model(self, label: str = "worker") -> FunctionModel:
        """A model that records the request it is asked and answers with that turn's own output tool."""

        def respond(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
            self.messages.append(list(messages))
            self.instructions.append(info.instructions)
            self.function_tools.append(_on_the_wire(info.function_tools))
            self.output_tools.append(_on_the_wire(info.output_tools))
            self.answered_by.append(label)
            index = _turn_index(messages)
            tool = info.output_tools[index]
            return ModelResponse(parts=[ToolCallPart(tool.name, _PAYLOADS[index])])

        return streaming_function_model(respond)


def _sequence_model() -> tuple[FunctionModel, list[list[ModelMessage]], list[str | None], list[list[ToolDefinition]]]:
    """Answers each turn with that turn's own output tool, recording every request's messages, instructions and tools."""
    rendered = _Rendered()
    return rendered.model(), rendered.messages, rendered.instructions, rendered.function_tools


async def test_turn_two_opens_on_turn_ones_messages(tmp_path: Path) -> None:
    model, calls, _instructions, _tool_defs = _sequence_model()
    environment = await build_environment(model, engram=fake_engram(), active=ActiveRun(), external_agents=dict, session_log=NoSessionLog(), custom=tmp_path / "custom", records=records_under(tmp_path), web_search=WebSearchSettings(), tiers=tiers_under(tmp_path))

    await run_workflow(_sequence_spec(), environment, params={"items": [{"title": "quantum computing"}]}, approval=ApproveAll())

    assert len(calls) == 3
    first_prompt = next(part.content for part in calls[0][-1].parts if isinstance(part, UserPromptPart))
    second_prompts = [part.content for message in calls[1] for part in message.parts if isinstance(part, UserPromptPart)]
    second_tool_calls = [part for message in calls[1] for part in message.parts if isinstance(part, ToolCallPart)]
    assert first_prompt in second_prompts, "the second request still carries the first turn's own prompt"
    assert len(second_tool_calls) == 1, "the second request carries exactly the first turn's own answer"


def _the_items_line(active: ActiveRun) -> StepConversation:
    """The one line this map's single item opened. Its key is minted from the item, not from a counter."""
    (line,) = [held for (node, _), held in active.conversations.conversations.items() if node == "digest_each"]
    return line


async def test_the_items_line_is_executing_at_the_moment_each_turns_model_is_asked(tmp_path: Path) -> None:
    active = ActiveRun()
    seen: list[bool] = []

    def respond(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        line = next((held for (node, _), held in active.conversations.conversations.items() if node == "digest_each"), None)
        seen.append(line is not None and line.executing)
        index = _turn_index(messages)
        tool = info.output_tools[index]
        return ModelResponse(parts=[ToolCallPart(tool.name, _PAYLOADS[index])])

    environment = await build_environment(streaming_function_model(respond), engram=fake_engram(), active=active, external_agents=dict, session_log=NoSessionLog(), custom=tmp_path / "custom", records=records_under(tmp_path), web_search=WebSearchSettings(), tiers=tiers_under(tmp_path))

    await run_workflow(_sequence_spec(), environment, params={"items": [{"title": "quantum computing"}]}, approval=ApproveAll())

    assert seen == [True, True, True], "the item's line was open and executing at every turn's own model call"
    assert _the_items_line(active).executing is False, "settled once the sequence ends"


async def test_a_raising_turn_still_clears_the_items_line_executing(tmp_path: Path) -> None:
    active = ActiveRun()

    def always_raises(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        del messages, info
        msg = "boom"
        raise RuntimeError(msg)

    environment = await build_environment(streaming_function_model(always_raises), engram=fake_engram(), active=active, external_agents=dict, session_log=NoSessionLog(), custom=tmp_path / "custom", records=records_under(tmp_path), web_search=WebSearchSettings(), tiers=tiers_under(tmp_path))

    with pytest.raises(RuntimeError):
        await run_workflow(_sequence_spec(), environment, params={"items": [{"title": "quantum computing"}]}, approval=ApproveAll())

    line = _the_items_line(active)
    assert line.executing is False, "the finally clears it even though the turn raised"


async def test_three_items_keep_three_conversations_of_three_turns(tmp_path: Path) -> None:
    model, _calls, _instructions, _tool_defs = _sequence_model()
    active = ActiveRun()
    environment = await build_environment(model, engram=fake_engram(), active=active, external_agents=dict, session_log=NoSessionLog(), custom=tmp_path / "custom", records=records_under(tmp_path), web_search=WebSearchSettings(), tiers=tiers_under(tmp_path))

    items: WorkflowArguments = {"items": [{"title": "a"}, {"title": "b"}, {"title": "c"}]}
    await run_workflow(_sequence_spec(), environment, params=items, approval=ApproveAll())

    assert len(active.conversations.conversations) == 3
    for line in active.conversations.conversations.values():
        user_turns = [part for message in line.messages for part in message.parts if isinstance(part, UserPromptPart)]
        assert len(user_turns) == 3


async def test_the_system_prompt_is_identical_bytes_on_every_turn_of_one_item(tmp_path: Path) -> None:
    model, _calls, instructions, _tool_defs = _sequence_model()
    environment = await build_environment(model, engram=fake_engram(), active=ActiveRun(), external_agents=dict, session_log=NoSessionLog(), custom=tmp_path / "custom", records=records_under(tmp_path), web_search=WebSearchSettings(), tiers=tiers_under(tmp_path))

    await run_workflow(_sequence_spec(), environment, params={"items": [{"title": "quantum computing"}]}, approval=ApproveAll())

    assert len(instructions) == 3
    assert instructions[0] == instructions[1] == instructions[2]
    assert instructions[0] is not None
    assert "quantum computing" in instructions[0]
    assert "answer tool" in instructions[0]


async def test_the_tool_surface_is_the_union_from_turn_one(tmp_path: Path) -> None:
    model, _calls, _instructions, tool_defs = _sequence_model()
    environment = await build_environment(model, engram=fake_engram(), active=ActiveRun(), external_agents=dict, session_log=NoSessionLog(), custom=tmp_path / "custom", records=records_under(tmp_path), web_search=WebSearchSettings(), tiers=tiers_under(tmp_path))

    await run_workflow(
        _sequence_spec(polish_tools=("run_sql",)), environment, params={"items": [{"title": "quantum computing"}]}, approval=ApproveAll()
    )

    run_code = next(tool for tool in tool_defs[0] if tool.name == "run_code")
    assert run_code.description is not None
    assert "run_sql" in run_code.description, "turn 1 already holds the sandbox for a tool only turn 3 declares"


SKILL_A = "saving-a-workflow"
"""What turn one of the sequence declares, and nothing else does."""

SKILL_B = "orchestrating-a-run"
"""What turn two declares. Turn three declares no skill at all."""

_SEQUENCE_SKILLS: tuple[tuple[str, ...], tuple[str, ...], tuple[str, ...]] = ((SKILL_A,), (SKILL_B,), ())


async def _sequence_over(rendered: _Rendered, tmp_path: Path, spec: Workflow, *, tiers: ModelTiers | None = None) -> None:
    """Run `spec` over one item through `build_environment`'s own `run_defined_step`."""
    environment = await build_environment(rendered.model(), engram=fake_engram(), active=ActiveRun(), external_agents=dict, session_log=NoSessionLog(), custom=tmp_path / "custom", records=records_under(tmp_path), web_search=WebSearchSettings(), tiers=tiers if tiers is not None else tiers_under(tmp_path))

    await run_workflow(spec, environment, params={"items": [{"title": "quantum computing"}]}, approval=ApproveAll())


async def test_every_turn_of_one_item_is_asked_with_the_sequences_whole_skill_and_tool_surface(tmp_path: Path) -> None:
    """Turn one declares skill A, turn two skill B, turn three none -- and all three are asked the same surface.

    A skill mounts as a deferred capability, so a per-turn selection rewrites the instruction block
    and the tool list between turns of one conversation. The union makes every request of the
    sequence carry the same bytes.
    """
    rendered = _Rendered()

    await _sequence_over(rendered, tmp_path, _sequence_spec(skills=_SEQUENCE_SKILLS, polish_tools=("run_sql",)))

    assert len(rendered.instructions) == 3
    assert rendered.instructions[0] == rendered.instructions[1] == rendered.instructions[2], "one instruction block for the sequence"
    opening = rendered.instructions[0]
    assert opening is not None
    assert SKILL_A in opening, "turn one's skill is offered on every turn"
    assert SKILL_B in opening, "turn two's skill is offered on turn one, which never declared it"
    assert rendered.function_tools[0] == rendered.function_tools[1] == rendered.function_tools[2], "the function tools stay identical"
    assert rendered.output_tools[0] == rendered.output_tools[1] == rendered.output_tools[2], "the output surface stays identical"
    assert any(tool.name == "load_capability" for tool in rendered.function_tools[2]), "the turn declaring no skill still holds the loader"


async def test_each_turns_own_task_instruction_reaches_only_its_newest_user_turn(tmp_path: Path) -> None:
    """A turn's briefing is this turn's words, never the sequence's standing instructions and never repeated."""
    rendered = _Rendered()

    await _sequence_over(rendered, tmp_path, _sequence_spec(skills=_SEQUENCE_SKILLS))

    own_words = (TRIAGE_INSTRUCTIONS, DRAFT_INSTRUCTIONS, POLISH_INSTRUCTIONS)
    for index, (request, words) in enumerate(zip(rendered.messages, own_words, strict=True)):
        prompts = [part.content for message in request for part in message.parts if isinstance(part, UserPromptPart)]
        assert sum(words in str(prompt) for prompt in prompts) == 1, f"turn {index} states its task once"
        assert words in _last_user_prompt(request), f"turn {index} states its task in the newest user turn"
        standing = rendered.instructions[index]
        assert standing is not None
        assert words not in standing, f"turn {index}'s task is not in the standing instructions"


ALONE_INSTRUCTIONS = "Read the item on its own and say in one line what kind of thing it is."


async def test_a_step_outside_the_sequence_holds_only_the_skills_it_declared(tmp_path: Path) -> None:
    """The union is one item sequence's own. A step feeding that sequence is unioned with nothing."""
    rendered = _Rendered()
    triage, draft, polish = _sequence_tasks(skills=_SEQUENCE_SKILLS)
    alone = Briefing(
        name="alone",
        instructions=ALONE_INSTRUCTIONS,
        inputs={"item": Shape(json_schema=ITEM_SCHEMA)},
        output=Shape(json_schema=TRIAGE_SCHEMA),
    )
    with_context = polish.model_copy(update={"inputs": {"draft": SameAs(like="draft"), "context": SameAs(like="alone")}})
    spec = Workflow(
        name="digest-sequence-and-one-more",
        version=1,
        description="One mapped sequence, and one step beside it that declares no skill of its own.",
        parameters={
            "items": ParameterSpec(description="items to digest", json_schema={"type": "array", "items": ITEM_SCHEMA}),
            "loose": ParameterSpec(description="one item to read alone", json_schema=ITEM_SCHEMA),
        },
        inline=(triage, draft, with_context, alone),
        nodes=(
            RunNode(id="alone", work="alone", inputs={"item": FromParameter(parameter="loose")}),
            MapNode(
                id="digest_each",
                turns=("triage", "draft", "polish"),
                over=FromParameter(parameter="items"),
                each="item",
                inputs={"context": FromNode(node="alone")},
            ),
        ),
        output=FromNode(node="digest_each"),
    )
    environment = await build_environment(rendered.model(), engram=fake_engram(), active=ActiveRun(), external_agents=dict, session_log=NoSessionLog(), custom=tmp_path / "custom", records=records_under(tmp_path), web_search=WebSearchSettings(), tiers=tiers_under(tmp_path))

    await run_workflow(
        spec,
        environment,
        params={"items": [{"title": "quantum computing"}], "loose": {"title": "quantum computing"}},
        approval=ApproveAll(),
    )

    standing = [text for text in rendered.instructions if text is not None]
    assert len(standing) == 4, "three turns of the sequence and the one step feeding it"
    (unrelated,) = [text for text in standing if ALONE_INSTRUCTIONS in text]
    assert SKILL_A not in unrelated, "the step outside the sequence never sees the sequence's skills"
    assert SKILL_B not in unrelated
    for turn in [text for text in standing if ALONE_INSTRUCTIONS not in text]:
        assert SKILL_A in turn
        assert SKILL_B in turn


def _tiers_labelled(home: Path, rendered: _Rendered) -> ModelTiers:
    """Tier resolution over a store of its own, whose every built model labels the setting it was built for."""

    def build(model_id: str, /, *, thinking: Thinking = "xhigh") -> FunctionModel:
        return rendered.model(f"{model_id}@{thinking}")

    return tiers_under(home, models=ModelList(listing=FakeModelListing()), build=build)


async def test_a_turn_runs_on_the_node_tier_then_its_own_tier_then_the_worker_default(tmp_path: Path) -> None:
    """The tier chain is unchanged by the union: the node's tier, else the turn's own, else worker."""
    on_the_node = _Rendered()
    tiers = _tiers_labelled(tmp_path / "node-tier", on_the_node)
    model_tiers.set_tier(tiers, "helper", TierSetting(model="deepseek-v4-flash-0731", thinking="low"))
    model_tiers.set_tier(tiers, "orchestrator", TierSetting(model="gpt-5.4", thinking="medium"))

    await _sequence_over(on_the_node, tmp_path, _sequence_spec(skills=_SEQUENCE_SKILLS, node_tier="helper", task_tier="orchestrator"), tiers=tiers)

    assert on_the_node.answered_by == ["deepseek-v4-flash-0731@low"] * 3, "the node's tier answers every turn, over the turn's own"

    on_its_own = _Rendered()
    own_tiers = _tiers_labelled(tmp_path / "own-tier", on_its_own)
    model_tiers.set_tier(own_tiers, "orchestrator", TierSetting(model="gpt-5.4", thinking="medium"))

    await _sequence_over(on_its_own, tmp_path, _sequence_spec(skills=_SEQUENCE_SKILLS, task_tier="orchestrator"), tiers=own_tiers)

    assert on_its_own.answered_by == ["worker", "gpt-5.4@medium", "worker"], "with no node tier, only the turn that named one leaves worker"

    plain = _Rendered()
    await _sequence_over(plain, tmp_path, _sequence_spec(skills=_SEQUENCE_SKILLS), tiers=_tiers_labelled(tmp_path / "no-tier", plain))

    assert plain.answered_by == ["worker"] * 3, "naming no tier anywhere keeps every turn on the worker default"


async def test_turn_one_recovers_after_first_answering_with_another_turns_shape(tmp_path: Path) -> None:
    """Turn 1 first answers with turn 2's shape, gets sent back, then answers with its own -- one extra model call."""
    calls: list[list[ModelMessage]] = []

    def respond(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        calls.append(list(messages))
        if len(calls) == 1:
            wrong = info.output_tools[1]
            return ModelResponse(parts=[ToolCallPart(wrong.name, _PAYLOADS[1])])
        index = _turn_index(messages)
        tool = info.output_tools[index]
        return ModelResponse(parts=[ToolCallPart(tool.name, _PAYLOADS[index])])

    active = ActiveRun()
    environment = await build_environment(streaming_function_model(respond), engram=fake_engram(), active=active, external_agents=dict, session_log=NoSessionLog(), custom=tmp_path / "custom", records=records_under(tmp_path), web_search=WebSearchSettings(), tiers=tiers_under(tmp_path))

    await run_workflow(_sequence_spec(), environment, params={"items": [{"title": "quantum computing"}]}, approval=ApproveAll())

    assert len(calls) == 4, "three turns plus the one retry turn one needed to answer with its own shape"
    line = _the_items_line(active)
    retries = [part for message in line.messages for part in message.parts if isinstance(part, RetryPromptPart)]
    assert len(retries) == 1, "the item's conversation carries the correction sent back for turn one's wrong shape"
    draft_prompt = _last_user_prompt(calls[2])
    assert "worth a look" in draft_prompt, "turn two's briefing carries turn one's corrected note, not the wrong shape it first tried"


async def test_every_turns_prompt_names_its_own_shape_and_the_tool_surface_stays_identical(tmp_path: Path) -> None:
    calls: list[list[ModelMessage]] = []
    tool_defs: list[tuple[list[ToolDefinition], list[ToolDefinition]]] = []

    def respond(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        calls.append(list(messages))
        tool_defs.append((_on_the_wire(info.function_tools), _on_the_wire(info.output_tools)))
        index = _turn_index(messages)
        tool = info.output_tools[index]
        return ModelResponse(parts=[ToolCallPart(tool.name, _PAYLOADS[index])])

    environment = await build_environment(streaming_function_model(respond), engram=fake_engram(), active=ActiveRun(), external_agents=dict, session_log=NoSessionLog(), custom=tmp_path / "custom", records=records_under(tmp_path), web_search=WebSearchSettings(), tiers=tiers_under(tmp_path))

    await run_workflow(_sequence_spec(), environment, params={"items": [{"title": "quantum computing"}]}, approval=ApproveAll())

    assert len(calls) == 3
    prompts = [_last_user_prompt(request) for request in calls]
    budget_line = "You have 24 model requests for this task. When they are nearly gone, answer with what you have."
    assert "Answer this task with Triage." in prompts[0], prompts[0]
    assert "Answer this task with Draft." in prompts[1], prompts[1]
    assert "Answer this task with Polish." in prompts[2], prompts[2]
    assert all(prompt.endswith(budget_line) for prompt in prompts), prompts
    assert tool_defs[0] == tool_defs[1] == tool_defs[2], "the tool list stays byte-identical across every turn"


async def test_a_turn_that_never_finds_its_own_shape_fails_after_the_retry_budget(tmp_path: Path) -> None:
    calls: list[list[ModelMessage]] = []

    def always_wrong(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        calls.append(list(messages))
        wrong = info.output_tools[1]
        return ModelResponse(parts=[ToolCallPart(wrong.name, _PAYLOADS[1])])

    environment = await build_environment(streaming_function_model(always_wrong), engram=fake_engram(), active=ActiveRun(), external_agents=dict, session_log=NoSessionLog(), custom=tmp_path / "custom", records=records_under(tmp_path), web_search=WebSearchSettings(), tiers=tiers_under(tmp_path))

    with pytest.raises(UnexpectedModelBehavior):
        await run_workflow(_sequence_spec(), environment, params={"items": [{"title": "quantum computing"}]}, approval=ApproveAll())

    assert len(calls) == 5, "the first attempt and the four corrections in the retry budget, then the library's own error"

    def respond(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        if info.instructions is not None and "stuck item" in info.instructions:
            wrong = info.output_tools[1]
            return ModelResponse(parts=[ToolCallPart(wrong.name, _PAYLOADS[1])])
        index = _turn_index(messages)
        tool = info.output_tools[index]
        return ModelResponse(parts=[ToolCallPart(tool.name, _PAYLOADS[index])])

    environment = await build_environment(streaming_function_model(respond), engram=fake_engram(), active=ActiveRun(), external_agents=dict, session_log=NoSessionLog(), custom=tmp_path / "custom", records=records_under(tmp_path), web_search=WebSearchSettings(), tiers=tiers_under(tmp_path))
    result = await run_workflow(
        _sequence_spec(on_error="skip"),
        environment,
        params={"items": [{"title": "stuck item"}, {"title": "quantum computing"}]},
        approval=ApproveAll(),
    )

    assert isinstance(result, list)
    assert len(result) == 1, "the stuck item was skipped after the retry budget, the second item still ran to completion"


async def test_a_later_turn_with_no_line_in_this_process_is_refused_by_name(tmp_path: Path) -> None:
    """Turn 2 has no line in `active.conversations` -- the process that ran turn 1 is not this one.

    So the call is refused by name, never a bare `KeyError` reading `inputs[turn.each]`. The
    turn's own inputs carry no `each` key at all here, since a later turn need not carry one either.
    """
    _triage, draft, _polish = _sequence_tasks()
    model, _calls, _instructions, _tool_defs = _sequence_model()
    environment = await build_environment(model, engram=fake_engram(), active=ActiveRun(), external_agents=dict, session_log=NoSessionLog(), custom=tmp_path / "custom", records=records_under(tmp_path), web_search=WebSearchSettings(), tiers=tiers_under(tmp_path))
    assert environment.perform is not None

    identity = StepIdentity(
        node_id="digest_each",
        item_key="0",
        turn=TurnFacts(work="draft", index=1, turns=("triage", "draft", "polish"), each="item", tools=(), outputs=(str,)),
    )
    with performing_step(identity), pytest.raises(MissingTurnLineError) as excinfo:
        await environment.perform(draft, dict, triage={"note": "worth a look"})

    message = str(excinfo.value)
    assert "digest_each" in message, message
    assert "2" in message, message


async def test_a_plain_run_step_is_unchanged(tmp_path: Path) -> None:
    seen_instructions: list[str | None] = []

    def respond(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        del messages
        seen_instructions.append(info.instructions)
        return ModelResponse(parts=[ToolCallPart(info.output_tools[0].name, _PAYLOADS[0])])

    active = ActiveRun()
    environment = await build_environment(streaming_function_model(respond), engram=fake_engram(), active=active, external_agents=dict, session_log=NoSessionLog(), custom=tmp_path / "custom", records=records_under(tmp_path), web_search=WebSearchSettings(), tiers=tiers_under(tmp_path))

    await run_workflow(_plain_spec(), environment, params={"item": {"title": "quantum computing"}}, approval=ApproveAll())

    assert seen_instructions == [TRIAGE_INSTRUCTIONS], "a one-shot briefing keeps its own instructions, never the turn sequence's"
    line = active.conversations.conversations[("triage", "")]
    assert line.messages, "a plain run step now steps its settled line behind"
    assert line.executing is False


async def test_a_worker_step_warns_when_its_cached_prefix_collapses(tmp_path: Path) -> None:
    """The item's first request reads a tool and establishes a cache prefix well above the monitor's floor.

    Its second request, still inside the same run, answers -- reading back almost none of what
    the first established. `build_worker_agent` mounts `WarnOnCacheBusts()` on every worker step,
    so that collapse is caught here without a live provider anywhere in the test.
    """
    established = RequestUsage(input_tokens=10, output_tokens=5, cache_read_tokens=0, cache_write_tokens=4000)
    collapsed = RequestUsage(input_tokens=10, output_tokens=5, cache_read_tokens=20, cache_write_tokens=0)

    def respond(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        already_read = any(
            isinstance(part, ToolReturnPart) and part.tool_name == "search" for message in messages for part in message.parts
        )
        if not already_read:
            return ModelResponse(parts=[ToolCallPart("search", {"query": "quantum computing"})], usage=established)
        return ModelResponse(parts=[ToolCallPart(info.output_tools[0].name, _PAYLOADS[0])], usage=collapsed)

    environment = await build_environment(usage_preserving_function_model(respond), engram=fake_engram(), active=ActiveRun(), external_agents=dict, session_log=NoSessionLog(), custom=tmp_path / "custom", records=records_under(tmp_path), web_search=WebSearchSettings(), tiers=tiers_under(tmp_path))

    with pytest.warns(CacheBustWarning):
        await run_workflow(_worker_with_read_spec(), environment, params={"item": {"title": "quantum computing"}}, approval=ApproveAll())


def test_a_turns_shape_name_covers_enums_optionals_and_generics() -> None:
    """The name a turn's prompt carries never fails on a grammar-legal output: an enum, an optional, a typed list."""
    assert _shape_name(Literal["requested", "seen"]) == "Literal[requested, seen]"
    assert _shape_name(list[Literal["a", "b"]]) == "list[Literal[a, b]]"
    assert _shape_name(int | None) == "UnionType[int, NoneType]"
    assert _shape_name(list[int]) == "list[int]"
    assert _shape_name(dict[str, int]) == "dict[str, int]"


A_DRAFT = "a short draft"
OPENED_WITH = "You are taking one item through a sequence of turns. The item: quantum computing"
A_LINE = UUID("33333333-3333-4333-8333-333333333333")
EARLIER_RUN = "run-turn-one"
LATER_RUN = "run-turn-two"


def _one_turn_model() -> tuple[FunctionModel, list[list[ModelMessage]], list[str | None]]:
    """Answers whatever turn it is asked with that turn's own output tool, recording the request it was asked in."""
    calls: list[list[ModelMessage]] = []
    instructions: list[str | None] = []

    def respond(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        calls.append(list(messages))
        instructions.append(info.instructions)
        return ModelResponse(parts=[TextPart(A_DRAFT)])

    return streaming_function_model(respond), calls, instructions


def _turn_one_checkpoint() -> ContinuableSnapshot:
    """What the worker that ran turn 1 committed: its own prompt, its own answer, under the line's system prompt."""
    return ContinuableSnapshot(
        run_id=EARLIER_RUN,
        step_index=0,
        conversation_id=str(A_LINE),
        messages=[
            ModelRequest(parts=[UserPromptPart(content=TRIAGE_INSTRUCTIONS)], instructions=OPENED_WITH),
            ModelResponse(parts=[ToolCallPart("final_result", _PAYLOADS[0], tool_call_id="call_triage")]),
            ModelRequest(parts=[ToolReturnPart(tool_name="final_result", content="Final result processed.", tool_call_id="call_triage")]),
        ],
    )


def _a_later_turn(*, revision: int = 1, attempt_id: str = "b" * 64) -> StepConversationContinuation:
    """Turn 2 of one item's line, scheduled against the ref turn 1 committed."""
    return StepConversationContinuation(
        attempt=ContinuationAttempt(
            conversation_id=A_LINE, attempt_id=attempt_id, expected_revision=revision, run_id=LATER_RUN
        ),
        given=ContinuationRef(
            conversation_id=A_LINE, revision=revision, run_id=EARLIER_RUN, snapshot_seq=0, checkpoint_digest="d" * 64
        ),
    )


def _a_carried_line(*, checkpoint: ContinuableSnapshot | None = None) -> tuple[DurableTurns, FakeContinuationCheckpoints]:
    """A ledger standing at revision 1 with turn 1's checkpoint in it, and the record beside it."""
    checkpoints = FakeContinuationCheckpoints()
    checkpoints.heads[A_LINE] = 1
    held = checkpoint if checkpoint is not None else _turn_one_checkpoint()
    checkpoints.checkpoints[EARLIER_RUN] = held
    return DurableTurns(checkpoints=checkpoints, record=FakeStepStore()), checkpoints


async def test_a_later_turn_resumes_the_checkpoint_the_record_holds(tmp_path: Path) -> None:
    """The whole point: the process that ran turn 1 is gone, and turn 2 continues its conversation anyway."""
    _triage, draft, _polish = _sequence_tasks()
    model, calls, instructions = _one_turn_model()
    turns, checkpoints = _a_carried_line()
    active = ActiveRun()
    environment = await build_environment(model, engram=fake_engram(), active=active, external_agents=dict, session_log=NoSessionLog(), custom=tmp_path / "custom", records=records_under(tmp_path), web_search=WebSearchSettings(), tiers=tiers_under(tmp_path), turns=turns)
    assert environment.perform is not None
    state = _a_later_turn()

    identity = StepIdentity(
        node_id="digest_each",
        item_key="0",
        turn=TurnFacts(work="draft", index=1, turns=("triage", "draft", "polish"), each="item", tools=(), outputs=(str,)),
    )
    with performing_step(identity), continuing_step_conversation(state):
        answer = await environment.perform(draft, str, triage={"note": "worth a look"})

    assert answer == A_DRAFT
    assert len(calls) == 1, "one model call, not a replay of the whole sequence"
    resumed = [part.content for message in calls[0] for part in message.parts if isinstance(part, UserPromptPart)]
    assert TRIAGE_INSTRUCTIONS in resumed, "turn 1's own prompt reached turn 2 out of the record"
    assert instructions == [OPENED_WITH], "the system prompt is the one the checkpoint was opened with, byte for byte"
    assert active.conversations.conversations == {}, "no line in this process: the record is carrying this one"
    assert [claim.attempt_id for claim in checkpoints.begun] == ["b" * 64]


async def test_a_later_turn_commits_its_output_and_moves_the_line_one_revision_on(tmp_path: Path) -> None:
    """What the next turn is scheduled against: the ref this one committed, one revision past its predecessor."""
    _triage, draft, _polish = _sequence_tasks()
    model, _calls, _instructions = _one_turn_model()
    turns, checkpoints = _a_carried_line()
    environment = await build_environment(model, engram=fake_engram(), active=ActiveRun(), external_agents=dict, session_log=NoSessionLog(), custom=tmp_path / "custom", records=records_under(tmp_path), web_search=WebSearchSettings(), tiers=tiers_under(tmp_path), turns=turns)
    assert environment.perform is not None
    state = _a_later_turn()

    identity = StepIdentity(
        node_id="digest_each",
        item_key="0",
        turn=TurnFacts(work="draft", index=1, turns=("triage", "draft", "polish"), each="item", tools=(), outputs=(str,)),
    )
    with performing_step(identity), continuing_step_conversation(state):
        await environment.perform(draft, str, triage={"note": "worth a look"})

    (committed,) = checkpoints.committed
    assert committed.ref.revision == 2
    assert committed.ref.conversation_id == A_LINE
    assert committed.ref.run_id == LATER_RUN, "the ref names the run this turn itself wrote"
    assert committed.output == A_DRAFT, "what is stored is the JSON the step's own output type validates"
    assert state.committed == committed.ref, "and the activity reads it back off the line it published"


async def test_a_turn_that_already_committed_answers_without_calling_a_model(tmp_path: Path) -> None:
    """A crash after the commit, before Temporal heard: the stored payload comes back, nothing runs again."""
    _triage, draft, _polish = _sequence_tasks()
    model, calls, _instructions = _one_turn_model()
    turns, checkpoints = _a_carried_line()
    settled = ContinuationCommit(
        ref=ContinuationRef(
            conversation_id=A_LINE, revision=2, run_id=LATER_RUN, snapshot_seq=1, checkpoint_digest="e" * 64
        ),
        output=A_DRAFT,
    )
    checkpoints.settled["b" * 64] = settled
    environment = await build_environment(model, engram=fake_engram(), active=ActiveRun(), external_agents=dict, session_log=NoSessionLog(), custom=tmp_path / "custom", records=records_under(tmp_path), web_search=WebSearchSettings(), tiers=tiers_under(tmp_path), turns=turns)
    assert environment.perform is not None
    state = _a_later_turn()

    identity = StepIdentity(
        node_id="digest_each",
        item_key="0",
        turn=TurnFacts(work="draft", index=1, turns=("triage", "draft", "polish"), each="item", tools=(), outputs=(str,)),
    )
    with performing_step(identity), continuing_step_conversation(state):
        answer = await environment.perform(draft, str, triage={"note": "worth a look"})

    assert answer == A_DRAFT
    assert calls == [], "no model call at all"
    assert checkpoints.committed == [], "and nothing committed a second time"
    assert state.committed == settled.ref


async def test_an_unfinished_turn_on_the_same_line_refuses_rather_than_running_again(tmp_path: Path) -> None:
    """An earlier attempt that never settled is a turn whose effects are unknown, so this one does not run."""
    _triage, draft, _polish = _sequence_tasks()
    model, calls, _instructions = _one_turn_model()
    turns, checkpoints = _a_carried_line()
    checkpoints.in_flight = "a" * 64
    environment = await build_environment(model, engram=fake_engram(), active=ActiveRun(), external_agents=dict, session_log=NoSessionLog(), custom=tmp_path / "custom", records=records_under(tmp_path), web_search=WebSearchSettings(), tiers=tiers_under(tmp_path), turns=turns)
    assert environment.perform is not None

    identity = StepIdentity(
        node_id="digest_each",
        item_key="0",
        turn=TurnFacts(work="draft", index=1, turns=("triage", "draft", "polish"), each="item", tools=(), outputs=(str,)),
    )
    with performing_step(identity), continuing_step_conversation(_a_later_turn()), pytest.raises(ContinuationInFlightError) as refusal:
        await environment.perform(draft, str, triage={"note": "worth a look"})

    assert "a" * 64 in str(refusal.value)
    assert calls == [], "the refusal came before any model call"


async def test_a_line_published_with_no_ledger_behind_it_still_uses_this_process(tmp_path: Path) -> None:
    """Withholding the ledger withholds the durable arm rather than degrading it: the local rule still applies."""
    _triage, draft, _polish = _sequence_tasks()
    model, _calls, _instructions = _one_turn_model()
    environment = await build_environment(model, engram=fake_engram(), active=ActiveRun(), external_agents=dict, session_log=NoSessionLog(), custom=tmp_path / "custom", records=records_under(tmp_path), web_search=WebSearchSettings(), tiers=tiers_under(tmp_path))
    assert environment.perform is not None

    identity = StepIdentity(
        node_id="digest_each",
        item_key="0",
        turn=TurnFacts(work="draft", index=1, turns=("triage", "draft", "polish"), each="item", tools=(), outputs=(str,)),
    )
    with performing_step(identity), continuing_step_conversation(_a_later_turn()), pytest.raises(MissingTurnLineError):
        await environment.perform(draft, str, triage={"note": "worth a look"})
