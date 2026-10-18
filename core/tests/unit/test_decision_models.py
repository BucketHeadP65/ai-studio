"""`decision_model` and `DecisionModelQuestions`: typed questions to a decision model, answered with probabilities."""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from pydantic_ai.exceptions import ModelAPIError, UnexpectedModelBehavior
from pydantic_ai.models.decision import ChoiceQuestion
from pydantic_ai.models.system_one import SystemOneModel
from pydantic_ai_harness.spend import SpendLimitExceeded

from core.ai.decision_models import DecisionModelQuestions, decision_model
from core.settings import OpenRouterSettings
from core.testing.fakes import FakeDecisionModel

if TYPE_CHECKING:
    from pydantic import JsonValue
    from pydantic_ai.models.decision import DecisionRequest


def test_decision_model_reaches_the_system_one_api_at_openrouters_address() -> None:
    model = decision_model("jev-1.13", OpenRouterSettings(api_key="k"))

    assert isinstance(model, SystemOneModel)
    assert model.model_name == "jev-1.13"
    assert model.base_url == "https://openrouter.ai/api/v1"


def test_decision_model_refuses_an_empty_key() -> None:
    with pytest.raises(ValueError, match=r"infrastructure\.openrouter\.api_key"):
        decision_model("jev-1.13", OpenRouterSettings())


_PASSAGES = {
    "h1": "A harness boot pauses any running workflow for the person to resume it.",
    "h2": "SpendLimits charges each model request against the process budget.",
}


def _restart_is_relevant(text: str) -> float:
    return 0.9 if "boot" in text else 0.1


async def test_per_hit_asks_one_request_per_passage_and_answers_the_raw_probability() -> None:
    model = FakeDecisionModel(yes=lambda request, _name, _question: _restart_is_relevant(str(request.state)))

    answered = await DecisionModelQuestions(model).relevance("what happens on restart?", _PASSAGES, deadline_seconds=5)

    assert answered == {"h1": 0.9, "h2": 0.1}
    assert len(model.requests) == 2
    assert all("what happens on restart?" in str(request.state) for request in model.requests)


async def test_all_hits_asks_one_request_whose_questions_each_carry_their_passage() -> None:
    model = FakeDecisionModel(yes=lambda _request, _name, question: _restart_is_relevant(str(question.instructions)))

    answered = await DecisionModelQuestions(model).relevance("what happens on restart?", _PASSAGES, requests="all_hits", deadline_seconds=5)

    assert answered == {"h1": 0.9, "h2": 0.1}
    assert len(model.requests) == 1
    (request,) = model.requests
    assert "boot" not in str(request.state), "the state is the query alone, each passage rides in its own question"
    assert sorted(request.questions) == ["relevant.h1", "relevant.h2"]


async def test_all_hits_with_one_passage_asks_it_on_its_own() -> None:
    model = FakeDecisionModel(yes=lambda _request, _name, _question: 0.7)

    answered = await DecisionModelQuestions(model).relevance("q", {"h1": "text"}, requests="all_hits", deadline_seconds=5)

    assert answered == {"h1": 0.7}
    assert list(model.requests[0].questions) == ["relevance"]


async def test_an_empty_passage_is_judged_on_a_placeholder() -> None:
    model = FakeDecisionModel()

    await DecisionModelQuestions(model).relevance("q", {"h1": ""}, deadline_seconds=5)

    assert "(no text)" in str(model.requests[0].state)


async def test_no_passages_ask_nothing() -> None:
    model = FakeDecisionModel()

    assert await DecisionModelQuestions(model).relevance("q", {}, deadline_seconds=5) == {}
    assert model.requests == []


@pytest.mark.parametrize(
    "error",
    [
        ModelAPIError(model_name="fake-decision", message="down"),
        SpendLimitExceeded("budget spent"),
        UnexpectedModelBehavior("no probabilities"),
    ],
)
async def test_a_failed_request_answers_none(error: Exception) -> None:
    answered = await DecisionModelQuestions(FakeDecisionModel(error=error)).relevance("q", _PASSAGES, deadline_seconds=5)

    assert answered is None


async def test_a_missed_deadline_answers_none() -> None:
    answered = await DecisionModelQuestions(FakeDecisionModel(delay_seconds=1)).relevance("q", _PASSAGES, deadline_seconds=0.05)

    assert answered is None


_OPTIONS = {"a": "the first option", "b": "the second option", "c": "the third option"}


def _first_listed_wins(_request: DecisionRequest, _name: str, question: ChoiceQuestion) -> dict[str, float]:
    labels = list(question.criteria)
    return {label: (0.6 if index == 0 else 0.4 / (len(labels) - 1)) for index, label in enumerate(labels)}


def _criteria_of(request: DecisionRequest) -> dict[str, JsonValue]:
    question = next(iter(request.questions.values()))
    assert isinstance(question, ChoiceQuestion)
    return question.criteria


async def test_pick_asks_each_permutation_in_a_different_order() -> None:
    model = FakeDecisionModel(choose=_first_listed_wins)

    await DecisionModelQuestions(model).pick("text", _OPTIONS, question="Which fits?", deadline_seconds=5)

    orders = [list(_criteria_of(request)) for request in model.requests]
    assert orders == [["a", "b", "c"], ["b", "c", "a"], ["c", "a", "b"]]


async def test_pick_averages_each_options_probability_across_the_orders() -> None:
    model = FakeDecisionModel(choose=lambda _r, _n, question: {label: (0.8 if label == "b" else 0.1) for label in question.criteria})

    picked = await DecisionModelQuestions(model).pick("text", _OPTIONS, question="Which fits?", deadline_seconds=5)

    assert picked == ("b", pytest.approx(0.8))


async def test_an_option_favored_only_by_its_position_averages_out() -> None:
    picked = await DecisionModelQuestions(FakeDecisionModel(choose=_first_listed_wins)).pick(
        "text", _OPTIONS, question="Which fits?", deadline_seconds=5
    )

    assert picked is not None
    assert picked[1] == pytest.approx((0.6 + 0.2 + 0.2) / 3), "each option led one order, so none stands out"
    assert picked[0] == "a", "a tie goes to the option listed first"


async def test_every_option_carries_its_description() -> None:
    model = FakeDecisionModel()

    await DecisionModelQuestions(model).pick("text", _OPTIONS, question="Which fits?", permutations=1, deadline_seconds=5)

    criteria = _criteria_of(model.requests[0])
    assert criteria == {"a": "the first option", "b": "the second option", "c": "the third option"}


async def test_a_failed_pick_answers_none() -> None:
    model = FakeDecisionModel(error=ModelAPIError(model_name="fake-decision", message="down"))

    assert await DecisionModelQuestions(model).pick("text", _OPTIONS, question="Which fits?", deadline_seconds=5) is None


async def test_a_pick_needs_two_options() -> None:
    with pytest.raises(ValueError, match="two or more options"):
        await DecisionModelQuestions(FakeDecisionModel()).pick("text", {"a": "only"}, question="Which fits?", deadline_seconds=5)


_FOUR_PASSAGES = {f"h{number}": f"passage {number}" for number in range(4)}


async def test_the_request_cap_limits_how_many_per_hit_requests_run_at_once() -> None:
    model = FakeDecisionModel(delay_seconds=0.05)

    answered = await DecisionModelQuestions(model, max_concurrent_requests=2).relevance("q", _FOUR_PASSAGES, deadline_seconds=5)

    assert answered is not None
    assert list(answered) == list(_FOUR_PASSAGES)
    assert model.most_in_flight == 2


async def test_without_a_cap_every_per_hit_request_runs_at_once() -> None:
    model = FakeDecisionModel(delay_seconds=0.05)

    await DecisionModelQuestions(model).relevance("q", _FOUR_PASSAGES, deadline_seconds=5)

    assert model.most_in_flight == 4
