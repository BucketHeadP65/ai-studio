"""Typed questions to a decision model: how likely each passage helps answer a query, and which option fits a text.

Every answer is a probability. Every failure answers `None`, so a caller carries on as if no model were set.
"""

from __future__ import annotations

import asyncio

from typing import TYPE_CHECKING, Literal

from pydantic import BaseModel, Field
from pydantic_ai import Agent, StructuredDict
from pydantic_ai.exceptions import AgentRunError, UnexpectedModelBehavior
from pydantic_ai.models.typesafe import TypeSafeModel
from pydantic_ai.providers.typesafe import TypeSafeProvider

from core.observability import get_logger

if TYPE_CHECKING:
    from collections.abc import Awaitable, Mapping, Sequence

    from pydantic_ai.capabilities import AbstractCapability
    from pydantic_ai.messages import ModelResponse
    from pydantic_ai.models.decision import DecisionModel

    from core.settings import GatewaySettings


def decision_model(model_name: str, gateway: GatewaySettings) -> TypeSafeModel:
    """TypeSafe's decision model `model_name`, reached through the gateway's TypeSafe route with the gateway key.

    Args:
        model_name: The name TypeSafe serves the model under, such as `jev-latest`.
        gateway: The gateway section of the infrastructure settings.

    Returns:
        The model.

    Raises:
        ValueError: The gateway section carries no key.
    """
    if not gateway.api_key:
        msg = f"decision model {model_name!r} is reached through the gateway, and infrastructure.gateway.api_key is empty"
        raise ValueError(msg)
    base_url = f"{gateway.base_url.rstrip('/')}/{gateway.typesafe_route.strip('/')}"
    return TypeSafeModel(model_name, provider=TypeSafeProvider(base_url=base_url, api_key=gateway.api_key))


type RequestShape = Literal["per_hit", "all_hits"]
"""One request per passage, or one request for every passage."""

RELEVANCE_QUESTION = "Does this passage help answer the query?"
"""The question a relevance request asks about each passage."""

_RELEVANT_FIELD = "relevant"
_PICK_FIELD = "pick"
_NO_TEXT = "(no text)"

_logger = get_logger(__name__)


class _Relevance(BaseModel):
    """Judge whether the passage helps answer the query."""

    relevance: float = Field(ge=0, le=1, description=RELEVANCE_QUESTION)


class DecisionModelQuestions:
    """Asks a decision model yes/no and pick-one questions and answers with probabilities.

    Every failure answers `None` and logs one warning: a model error, a spend limit refusal, an answer with no
    probabilities, or a missed deadline. One failed request among several fails the whole question.
    """

    def __init__(
        self,
        model: DecisionModel,
        *,
        capabilities: Sequence[AbstractCapability[None]] = (),
        max_concurrent_requests: int | None = None,
    ) -> None:
        """Hold one agent over `model`, with no tools.

        Args:
            model: The decision model that answers.
            capabilities: What every request runs through, such as the process's spend limits. Each must add no tools.
            max_concurrent_requests: How many requests may run at once. Requests past it wait their turn within the
                question's deadline. None sets no limit.
        """
        self._agent: Agent[None, str] = Agent(
            model, deps_type=type(None), capabilities=list(capabilities), max_concurrency=max_concurrent_requests
        )

    async def relevance(
        self, query: str, passages: Mapping[str, str], *, requests: RequestShape = "per_hit", deadline_seconds: float
    ) -> dict[str, float] | None:
        """The probability that each passage helps answer `query`, keyed like `passages`.

        `per_hit` sends one request per passage. `all_hits` sends one request whose questions carry one passage
        each. A single passage is always asked on its own, and an empty one is judged on a placeholder.

        Args:
            query: The query the passages were found for.
            passages: Each passage's text, under the key the answer uses.
            requests: One request per passage, or one for every passage.
            deadline_seconds: How long every request together may take.

        Returns:
            Each key's probability from 0 to 1, an empty mapping for no passages, or `None` when the model failed or
            missed the deadline.
        """
        if not passages:
            return {}
        texts = {key: text or _NO_TEXT for key, text in passages.items()}
        try:
            async with asyncio.timeout(deadline_seconds):
                if requests == "all_hits" and len(texts) > 1:
                    return await self._all_at_once(query, texts)
                return await self._one_by_one(query, texts)
        except (AgentRunError, TimeoutError) as error:
            _logger.warning("relevance answered nothing: {reason}", reason=_reason(error, deadline_seconds))
            return None

    async def _one_by_one(self, query: str, passages: Mapping[str, str]) -> dict[str, float]:
        keys = list(passages)
        answers = await _all_or_first_error(
            [self._agent.run(f"Query: {query}\n\nPassage: {passages[key]}", output_type=_Relevance) for key in keys]
        )
        return {key: answer.output.relevance for key, answer in zip(keys, answers, strict=True)}

    async def _all_at_once(self, query: str, passages: Mapping[str, str]) -> dict[str, float]:
        schema = {
            "type": "object",
            "properties": {
                _RELEVANT_FIELD: {
                    "type": "array",
                    "description": RELEVANCE_QUESTION,
                    "items": {"anyOf": [{"const": key, "description": text} for key, text in passages.items()]},
                }
            },
            "required": [_RELEVANT_FIELD],
        }
        result = await self._agent.run(f"Query: {query}", output_type=StructuredDict(schema, name="RelevantPassages"))
        probabilities = _probabilities(result.response, _RELEVANT_FIELD)
        missing = [key for key in passages if key not in probabilities]
        if missing:
            msg = f"the decision model reported no probability for {missing}"
            raise UnexpectedModelBehavior(msg)
        return {key: probabilities[key] for key in passages}

    async def pick(
        self, text: str, options: Mapping[str, str], *, question: str, permutations: int = 3, deadline_seconds: float
    ) -> tuple[str, float] | None:
        """The option that fits `text` and its probability, averaged over `permutations` orders of the options.

        Each order is the given order rotated to start at a different option, and each is asked in its own request
        with every option's description. Orders repeat when `permutations` exceeds the number of options. A tie goes
        to the option listed first in `options`.

        Args:
            text: The text the pick is judged on.
            options: Each option's description, under its label.
            question: What the pick asks, such as which skill a message needs.
            permutations: How many orders are asked.
            deadline_seconds: How long every request together may take.

        Returns:
            The label with the highest averaged probability and that probability, or `None` when the model failed
            or missed the deadline.

        Raises:
            ValueError: `options` holds fewer than two labels, or `permutations` is below one.
        """
        labels = list(options)
        if len(labels) < 2 or permutations < 1:  # noqa: PLR2004 (a pick-one needs two options to choose between)
            msg = f"a pick needs two or more options and one or more permutations, got {len(labels)} and {permutations}"
            raise ValueError(msg)
        orders = [_rotated(labels, round_index * len(labels) // permutations) for round_index in range(permutations)]
        try:
            async with asyncio.timeout(deadline_seconds):
                answers = await _all_or_first_error([self._pick_once(text, question, options, order) for order in orders])
        except (AgentRunError, TimeoutError) as error:
            _logger.warning("the pick answered nothing: {reason}", reason=_reason(error, deadline_seconds))
            return None
        averaged = {label: sum(answer.get(label, 0.0) for answer in answers) / len(answers) for label in labels}
        best = max(labels, key=averaged.__getitem__)
        return best, averaged[best]

    async def _pick_once(self, text: str, question: str, options: Mapping[str, str], order: Sequence[str]) -> dict[str, float]:
        schema = {
            "type": "object",
            "properties": {
                _PICK_FIELD: {"description": question, "anyOf": [{"const": label, "description": options[label]} for label in order]}
            },
            "required": [_PICK_FIELD],
        }
        result = await self._agent.run(text, output_type=StructuredDict(schema, name="Pick"))
        return _probabilities(result.response, _PICK_FIELD)


async def _all_or_first_error[T](calls: Sequence[Awaitable[T]]) -> list[T]:
    """Every call's result, run together, or the first call's error once all have settled."""
    results = await asyncio.gather(*calls, return_exceptions=True)
    for result in results:
        if isinstance(result, BaseException):
            raise result
    return [result for result in results if not isinstance(result, BaseException)]


def _rotated(labels: Sequence[str], offset: int) -> list[str]:
    """`labels` starting at `offset`, the ones before it moved to the end."""
    return [*labels[offset:], *labels[:offset]]


def _probabilities(response: ModelResponse, field: str) -> dict[str, float]:
    """The per-option probabilities the decision model reported for `field`."""
    details = response.provider_details or {}
    reported = details.get("probabilities")
    by_option = reported.get(field) if isinstance(reported, dict) else None
    if not isinstance(by_option, dict):
        msg = f"the decision model reported no probabilities for {field!r}"
        raise UnexpectedModelBehavior(msg)
    return {label: float(p) for label, p in by_option.items() if isinstance(label, str) and isinstance(p, int | float)}


def _reason(error: Exception, deadline_seconds: float) -> str:
    """One line saying why a question answered nothing."""
    if isinstance(error, TimeoutError):
        return f"no answer within {deadline_seconds}s"
    return f"{type(error).__name__}: {error}"
