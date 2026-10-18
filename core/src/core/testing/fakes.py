"""In-memory stand-ins for core's ports and a scripted decision model, for unit tests."""

from __future__ import annotations

import asyncio

from dataclasses import dataclass
from typing import TYPE_CHECKING, Self

from pydantic_ai.models.decision import (
    ChoiceAnswer,
    ChoiceQuestion,
    DecisionAnswer,
    DecisionModel,
    DecisionModelSettings,
    DecisionRequest,
    DecisionResponse,
    NoulAnswer,
    NoulQuestion,
)

from core.cqrs.contracts import CoordinationBuffer

if TYPE_CHECKING:
    from collections.abc import Callable, Mapping
    from uuid import UUID

    from core.cqrs.contracts import Event


class FakeUnitOfWork:
    """A `UnitOfWork` that records commit and rollback without touching a database.

    Leaving the block rolls back unless `commit` was called first, as the real one does.
    `committed` and `rolled_back` say which happened.
    Recorded events and deliveries stay on `coordination` until a rollback clears them.
    """

    def __init__(self) -> None:
        self.committed = False
        self.rolled_back = False
        self.coordination = CoordinationBuffer()

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(self, *exc: object) -> None:
        del exc
        if not self.committed:
            self.rolled_back = True
            self.coordination.clear()

    async def commit(self) -> None:
        """Mark the unit of work committed."""
        self.committed = True

    def record(self, *events: Event) -> None:
        """Queue events on the coordination buffer.

        Args:
            *events: The events to queue.
        """
        self.coordination.record(*events)

    def record_delivery(self, event_id: UUID, subscriber: str) -> None:
        """Queue a delivery record on the coordination buffer.

        Args:
            event_id: The delivered event.
            subscriber: The subscriber it was delivered to.
        """
        self.coordination.record_delivery(event_id, subscriber)


def _even_yes(_request: DecisionRequest, _name: str, _question: NoulQuestion) -> float:
    return 0.5


def _even_choice(_request: DecisionRequest, _name: str, question: ChoiceQuestion) -> Mapping[str, float]:
    return {label: 1 / len(question.criteria) for label in question.criteria}


@dataclass(init=False)
class FakeDecisionModel(DecisionModel[None]):
    """A decision model that answers scripted probabilities and records every request it receives.

    `yes` answers each yes/no question's probability of yes, and `choose` each pick-one question's probabilities.
    Both default to an even answer. `error`, when set, is raised by every request. `delay_seconds` holds each
    request back that long. `requests` holds every request in the order it arrived. `most_in_flight` is the most
    requests that ran at the same time.
    """

    def __init__(
        self,
        *,
        yes: Callable[[DecisionRequest, str, NoulQuestion], float] | None = None,
        choose: Callable[[DecisionRequest, str, ChoiceQuestion], Mapping[str, float]] | None = None,
        error: Exception | None = None,
        delay_seconds: float = 0.0,
    ) -> None:
        """Script the model.

        Args:
            yes: Answers a yes/no question with its probability of yes. Defaults to 0.5.
            choose: Answers a pick-one question with a probability per option label. Defaults to an even split.
            error: Raised by every request when set.
            delay_seconds: How long each request waits before it answers.
        """
        super().__init__()
        self.yes = yes or _even_yes
        self.choose = choose or _even_choice
        self.error = error
        self.delay_seconds = delay_seconds
        self.requests: list[DecisionRequest] = []
        self.most_in_flight = 0
        self._in_flight = 0

    @property
    def model_name(self) -> str:
        """The name `fake-decision`."""
        return "fake-decision"

    @property
    def system(self) -> str:
        """The system `fake`."""
        return "fake"

    async def decide(self, request: DecisionRequest, model_settings: DecisionModelSettings) -> DecisionResponse:
        """Record `request`, then answer every question from `yes` and `choose`, or raise `error`.

        Args:
            request: The questions to answer.
            model_settings: Ignored.

        Returns:
            One answer per question, under the question's name.

        Raises:
            Exception: The `error` the fake was given, when set.
            NotImplementedError: A question is a score question.
        """
        del model_settings
        self.requests.append(request)
        self._in_flight += 1
        self.most_in_flight = max(self.most_in_flight, self._in_flight)
        try:
            if self.delay_seconds:
                await asyncio.sleep(self.delay_seconds)
            if self.error is not None:
                raise self.error
            return self._answer(request)
        finally:
            self._in_flight -= 1

    def _answer(self, request: DecisionRequest) -> DecisionResponse:
        answers: dict[str, DecisionAnswer] = {}
        for name, question in request.questions.items():
            if isinstance(question, NoulQuestion):
                answers[name] = NoulAnswer(noul=self.yes(request, name, question))
            elif isinstance(question, ChoiceQuestion):
                probabilities = dict(self.choose(request, name, question))
                choice = max(probabilities, key=probabilities.__getitem__)
                answers[name] = ChoiceAnswer(choice=choice, confidence=probabilities[choice], probabilities=probabilities)
            else:
                msg = f"FakeDecisionModel answers no {question.type} question"
                raise NotImplementedError(msg)
        return DecisionResponse(answers=answers, model_name=self.model_name)
