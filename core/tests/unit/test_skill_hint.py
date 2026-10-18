"""`SkillHint`: the skill a decision model picks for a run's prompt, named on the run's first request only."""

from __future__ import annotations

import threading

from typing import TYPE_CHECKING

from pydantic_ai import Agent
from pydantic_ai.exceptions import ModelAPIError
from pydantic_ai.messages import ImageUrl, ModelMessage, ModelResponse, TextPart, ToolCallPart
from pydantic_ai.models.decision import ChoiceQuestion
from pydantic_ai.models.function import AgentInfo, FunctionModel
from pydantic_ai.models.test import TestModel
from pydantic_ai.tools import RunContext
from pydantic_ai.usage import RunUsage
from pydantic_ai_harness.system_reminders import SystemReminders

from core.ai.decision_models import DecisionModelQuestions
from core.ai.skill_hint import NO_SKILL, SkillHint
from core.testing.fakes import FakeDecisionModel

if TYPE_CHECKING:
    from collections.abc import Mapping

    from pydantic_ai.models.decision import DecisionRequest

_SKILLS = {"designing-workflows": "Use when designing a workflow graph.", "agent-ux": "Use when naming a tool an agent reads."}
_HINT = "The designing-workflows skill likely applies"


def _workflows_wins(_request: DecisionRequest, _name: str, question: ChoiceQuestion) -> Mapping[str, float]:
    return {label: (0.9 if label == "designing-workflows" else 0.1 / (len(question.criteria) - 1)) for label in question.criteria}


def _hint(model: FakeDecisionModel, *, threshold: float = 0.6) -> SkillHint[None]:
    return SkillHint[None](
        questions=DecisionModelQuestions(model),
        skills=lambda _ctx: _SKILLS,
        text=lambda ctx: ctx.prompt if isinstance(ctx.prompt, str) else None,
        threshold=threshold,
        deadline_seconds=5,
    )


def _ctx(prompt: str | list[str | ImageUrl], *, step: int = 1, loaded: frozenset[str] = frozenset()) -> RunContext[None]:
    return RunContext(deps=None, model=TestModel(), usage=RunUsage(), prompt=prompt, run_step=step, loaded_capability_ids=set(loaded))


async def test_the_hint_names_the_pick_on_the_first_request() -> None:
    said = await _hint(FakeDecisionModel(choose=_workflows_wins))(_ctx("draw me a workflow"))

    assert said is not None
    assert _HINT in said
    assert said.startswith("<system-reminder>")


async def test_the_hint_reaches_the_model_and_is_never_saved() -> None:
    seen: list[list[ModelMessage]] = []

    def _answer(messages: list[ModelMessage], _info: AgentInfo) -> ModelResponse:
        seen.append(list(messages))
        if len(seen) == 1:
            return ModelResponse(parts=[ToolCallPart("noop", {})])
        return ModelResponse(parts=[TextPart("done")])

    agent = Agent(
        FunctionModel(_answer),
        deps_type=type(None),
        capabilities=[SystemReminders[None](dynamic_reminders=[_hint(FakeDecisionModel(choose=_workflows_wins))])],
    )

    @agent.tool_plain
    def noop() -> str:
        return "ok"

    result = await agent.run("draw me a workflow")

    assert _HINT in str(seen[0]), "the first request carries the hint"
    assert _HINT not in str(seen[1]), "the second request does not, not even in the history it re-sends"
    assert _HINT not in str(result.all_messages()), "the saved history never holds it"


async def test_a_later_request_gets_no_hint() -> None:
    model = FakeDecisionModel(choose=_workflows_wins)

    assert await _hint(model)(_ctx("draw me a workflow", step=2)) is None
    assert model.requests == []


async def test_an_empty_or_non_text_prompt_gets_no_hint() -> None:
    model = FakeDecisionModel(choose=_workflows_wins)

    assert await _hint(model)(_ctx("   ")) is None
    assert await _hint(model)(_ctx(["look", ImageUrl("https://example.test/a.png")])) is None
    assert model.requests == []


async def test_loaded_skills_are_not_offered_and_all_loaded_asks_nothing() -> None:
    model = FakeDecisionModel(choose=_workflows_wins)

    await _hint(model)(_ctx("draw me a workflow", loaded=frozenset({"agent-ux"})))
    question = next(iter(model.requests[0].questions.values()))
    assert isinstance(question, ChoiceQuestion)
    offered = set(question.criteria)
    assert offered == {"designing-workflows", NO_SKILL}

    model.requests.clear()
    assert await _hint(model)(_ctx("draw me a workflow", loaded=frozenset(_SKILLS))) is None
    assert model.requests == []


async def test_a_weak_pick_none_of_them_or_a_failure_gets_no_hint() -> None:
    weak = FakeDecisionModel(choose=lambda _r, _n, q: {label: 1 / len(q.criteria) for label in q.criteria})
    no_skill = FakeDecisionModel(choose=lambda _r, _n, q: {label: (0.9 if label == NO_SKILL else 0.05) for label in q.criteria})
    failing = FakeDecisionModel(error=ModelAPIError(model_name="fake-decision", message="down"))

    assert await _hint(weak)(_ctx("draw me a workflow")) is None
    assert await _hint(no_skill)(_ctx("draw me a workflow")) is None
    assert await _hint(failing)(_ctx("draw me a workflow")) is None


async def test_the_skills_are_read_on_a_worker_thread() -> None:
    threads: list[int] = []

    def _skills(_ctx: RunContext[None]) -> Mapping[str, str]:
        threads.append(threading.get_ident())
        return _SKILLS

    hint = SkillHint[None](
        questions=DecisionModelQuestions(FakeDecisionModel(choose=_workflows_wins)),
        skills=_skills,
        text=lambda ctx: ctx.prompt if isinstance(ctx.prompt, str) else None,
        threshold=0.6,
        deadline_seconds=5,
    )

    said = await hint(_ctx("draw me a workflow"))

    assert threads
    assert threads[0] != threading.get_ident()
    assert said is not None
    assert _HINT in said
