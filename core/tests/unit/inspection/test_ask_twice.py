"""The shared two-try loop both inspector lanes ask their tiers through."""

from __future__ import annotations

import pytest

from pydantic import BaseModel, ValidationError
from pydantic_ai import ModelRetry
from pydantic_ai.exceptions import UnexpectedModelBehavior

from core.ai.inspection.inspector import Unresolved, ask_twice


class _Strict(BaseModel):
    count: int


def _validation_error() -> ValidationError:
    try:
        _Strict.model_validate({"count": "many"})
    except ValidationError as error:
        return error
    raise AssertionError


class Scripted:
    """An ask that raises or returns each scripted outcome in turn and keeps every refusal it was handed."""

    def __init__(self, *outcomes: str | BaseException) -> None:
        self.outcomes = list(outcomes)
        self.refusals: list[str | None] = []

    async def __call__(self, refusal: str | None) -> str:
        self.refusals.append(refusal)
        outcome = self.outcomes.pop(0)
        if isinstance(outcome, BaseException):
            raise outcome
        return outcome


def _exhausted(cause: BaseException) -> UnexpectedModelBehavior:
    error = UnexpectedModelBehavior("Exceeded maximum retries")
    error.__cause__ = cause
    return error


async def test_an_answer_on_the_first_try_is_returned_as_it_is() -> None:
    ask = Scripted("seen")

    assert await ask_twice(ask) == "seen"
    assert ask.refusals == [None]


async def test_a_refusal_is_handed_to_the_second_try() -> None:
    ask = Scripted(ValueError("span 9.9.9 is not in the run"), "seen")

    assert await ask_twice(ask) == "seen"
    assert ask.refusals == [None, "span 9.9.9 is not in the run"]


async def test_two_refusals_give_up_as_invalid_with_the_last_refusal() -> None:
    ask = Scripted(ValueError("first"), ValueError("second"))

    assert await ask_twice(ask) == Unresolved("invalid", "refused twice: second")


@pytest.mark.parametrize("cause", [_validation_error(), ModelRetry("try again")])
async def test_exhausted_output_validation_gives_up_as_invalid(cause: BaseException) -> None:
    ask = Scripted(_exhausted(cause))

    assert await ask_twice(ask) == Unresolved("invalid", f"output validation exhausted: {cause}")
    assert ask.refusals == [None]


async def test_any_other_unexpected_behavior_is_raised() -> None:
    ask = Scripted(_exhausted(RuntimeError("provider fell over")))

    with pytest.raises(UnexpectedModelBehavior):
        await ask_twice(ask)
