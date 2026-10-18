"""Timing a pydantic-ai model's requests into one trial's model-turn record.

A response carries its own token counts and its own model and provider names, but not how long the
request took, so the wrapper measures that around the call itself. Price comes from the response's
own price resolution, and a model or provider the price tables do not know leaves `cost_usd` as
`None` rather than zero.

The wrapper records the plain request path. A streamed request passes through untraced, so a run
that wants model turns in its trace runs the model without streaming.
"""

from __future__ import annotations

from time import perf_counter
from typing import TYPE_CHECKING

from pydantic_ai.models.wrapper import WrapperModel

from core.ai.evaluation.domain.trace import ModelTurnTrace

if TYPE_CHECKING:
    from decimal import Decimal

    from pydantic_ai.messages import ModelMessage, ModelResponse
    from pydantic_ai.models import Model, ModelRequestParameters
    from pydantic_ai.settings import ModelSettings

    from core.ai.evaluation.domain.trace import TraceSink


class TracingModel(WrapperModel):
    """Records one `ModelTurnTrace` per completed request, then returns the response untouched.

    The wrapped model decides everything about the answer. This adds only the measurement: the
    position in the trial, the wall-clock duration, the response's own usage and names, and its
    price when the price is resolvable.
    """

    def __init__(self, wrapped: Model, sink: TraceSink) -> None:
        """Wrap a model and record its turns into one trial's sink.

        Args:
            wrapped: The model that answers every request.
            sink: The trial's trace sink.
        """
        super().__init__(wrapped)
        self._sink = sink

    async def request(
        self, messages: list[ModelMessage], model_settings: ModelSettings | None, model_request_parameters: ModelRequestParameters
    ) -> ModelResponse:
        """Run the wrapped request, recording what it used and how long it took.

        Args:
            messages: The conversation so far.
            model_settings: The settings for this request.
            model_request_parameters: The request parameters pydantic-ai passes through.

        Returns:
            The wrapped model's response, untouched.
        """
        ordinal = await self._sink.claim_model_turn_ordinal()
        started = perf_counter()
        response = await super().request(messages, model_settings, model_request_parameters)
        duration_ms = max(round((perf_counter() - started) * 1000), 0)
        await self._sink.record_model_turn(_turn(ordinal, response, duration_ms))
        return response


def _turn(ordinal: int, response: ModelResponse, duration_ms: int) -> ModelTurnTrace:
    """One response's measured usage, duration and resolvable price."""
    usage = response.usage
    return ModelTurnTrace(
        ordinal=ordinal,
        input_tokens=usage.input_tokens,
        output_tokens=usage.output_tokens,
        cache_read_tokens=usage.cache_read_tokens,
        cache_write_tokens=usage.cache_write_tokens,
        duration_ms=duration_ms,
        cost_usd=_price(response),
        model_name=response.model_name,
        provider_name=response.provider_name,
    )


def _price(response: ModelResponse) -> Decimal | None:
    """The response's total price, or `None` when the answering model or provider has no known price."""
    if not response.model_name:
        return None
    try:
        return response.cost().total_price
    except LookupError:
        return None
