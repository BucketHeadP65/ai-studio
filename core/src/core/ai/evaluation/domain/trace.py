"""The execution trace: what one trial's tools returned and what its model turns cost.

A trial's honest record has two halves that are never mixed. `ToolCallTrace` says what one tool
call admitted into the conversation: the payload it delivered, the references that payload carried,
its serialized size, how long it took, and whether it was refused. `ModelTurnTrace` says what one
model request cost: its tokens, its duration and its price when the price is known. A tool call has
no model price, so `ToolCallTrace.direct_cost_usd` stays `None` unless the tool itself was billed
directly.

Unknown stays unknown. A turn whose price cannot be resolved records `cost_usd=None` rather than
zero, so a report can say the monetary total is unknown instead of understating it.

`TraceSink` is the port a running trial writes through. It hands out the ordinals as well as
collecting the rows, so concurrent tool calls in one trial get distinct, gap-free positions from one
place. `TraceRecorder` is the in-memory sink a single trial uses, and its `trace()` returns the rows
in ordinal order.
"""

from __future__ import annotations

import asyncio

from decimal import Decimal
from typing import Literal, Protocol, Self

from pydantic import Field, model_validator

from core.ai.evaluation.domain.evidence import DeliveredEvidenceRef
from core.ai.evaluation.domain.models import EvaluationModel, Nonempty, require_unique


class ToolCallTrace(EvaluationModel):
    """One tool call as the trial saw it: what it delivered, what it cost in context, and its verdict.

    `status` separates a delivered result (`ok`) from one the guard refused before it could reach
    the model (`refused`) and one the tool itself failed (`failed`). `withheld_exposure` is the
    contamination flag: it is true when the call's own arguments named a reference the case
    withholds, which invalidates the trial however well the rest of it scored.
    """

    tool_name: Nonempty
    ordinal: int = Field(ge=0)
    status: Literal["ok", "refused", "failed"]
    delivered_evidence: tuple[DeliveredEvidenceRef, ...] = ()
    returned_unit_refs: tuple[Nonempty, ...] = ()
    response_bytes: int = Field(ge=0)
    duration_ms: int = Field(ge=0)
    direct_cost_usd: Decimal | None = Field(default=None, ge=0)
    withheld_exposure: bool = False


class ModelTurnTrace(EvaluationModel):
    """One model request's measured usage and price, kept apart from any tool's own cost.

    `cost_usd` is `None` whenever the price could not be resolved for the answering model and
    provider. A report combining turns reads one unknown price as an unknown total.
    """

    ordinal: int = Field(ge=0)
    input_tokens: int = Field(default=0, ge=0)
    output_tokens: int = Field(default=0, ge=0)
    cache_read_tokens: int = Field(default=0, ge=0)
    cache_write_tokens: int = Field(default=0, ge=0)
    duration_ms: int = Field(ge=0)
    cost_usd: Decimal | None = Field(default=None, ge=0)
    model_name: str | None = None
    provider_name: str | None = None


class ExecutionTrace(EvaluationModel):
    """One trial's complete tool calls and model turns, each row at its own position."""

    tool_calls: tuple[ToolCallTrace, ...] = ()
    model_turns: tuple[ModelTurnTrace, ...] = ()
    run_duration_ms: int = Field(default=0, ge=0)

    @model_validator(mode="after")
    def unique_positions(self) -> Self:
        """Refuse two rows claiming one position, which would hide one of them.

        Returns:
            The trace, unchanged.

        Raises:
            ValueError: Two tool calls or two model turns claim one ordinal.
        """
        require_unique((str(call.ordinal) for call in self.tool_calls), "tool call ordinals")
        require_unique((str(turn.ordinal) for turn in self.model_turns), "model turn ordinals")
        return self

    @property
    def withheld_exposures(self) -> tuple[ToolCallTrace, ...]:
        """Every call whose arguments named a withheld reference, empty when the trial stayed clean."""
        return tuple(call for call in self.tool_calls if call.withheld_exposure)


class TraceSink(Protocol):
    """Where one trial's tool calls and model turns are recorded, and where their order comes from."""

    async def claim_tool_ordinal(self) -> int:
        """The position the next tool call will be filed under, claimed before the call runs.

        Returns:
            The claimed ordinal.
        """
        ...

    async def claim_model_turn_ordinal(self) -> int:
        """The position the next model turn will be filed under, claimed before the request runs.

        Returns:
            The claimed ordinal.
        """
        ...

    async def record_tool_call(self, call: ToolCallTrace) -> None:
        """File one finished tool call.

        Args:
            call: The finished tool call.
        """
        ...

    async def record_model_turn(self, turn: ModelTurnTrace) -> None:
        """File one finished model turn.

        Args:
            turn: The finished model turn.
        """
        ...


class TraceRecorder:
    """The in-memory `TraceSink` one trial writes through, safe for concurrent tool calls.

    One recorder belongs to one trial. Ordinals are handed out under a lock, so two tool calls
    running at once never claim the same position, and `trace` returns the rows in that order
    whatever order they finished in.
    """

    def __init__(self) -> None:
        self._lock = asyncio.Lock()
        self._next_tool_ordinal = 0
        self._next_model_turn_ordinal = 0
        self._tool_calls: list[ToolCallTrace] = []
        self._model_turns: list[ModelTurnTrace] = []

    async def claim_tool_ordinal(self) -> int:
        """The position the next tool call will be filed under.

        Returns:
            The claimed ordinal.
        """
        async with self._lock:
            ordinal = self._next_tool_ordinal
            self._next_tool_ordinal += 1
            return ordinal

    async def claim_model_turn_ordinal(self) -> int:
        """The position the next model turn will be filed under.

        Returns:
            The claimed ordinal.
        """
        async with self._lock:
            ordinal = self._next_model_turn_ordinal
            self._next_model_turn_ordinal += 1
            return ordinal

    async def record_tool_call(self, call: ToolCallTrace) -> None:
        """File one finished tool call.

        Args:
            call: The finished tool call.
        """
        async with self._lock:
            self._tool_calls.append(call)

    async def record_model_turn(self, turn: ModelTurnTrace) -> None:
        """File one finished model turn.

        Args:
            turn: The finished model turn.
        """
        async with self._lock:
            self._model_turns.append(turn)

    def trace(self, *, run_duration_ms: int = 0) -> ExecutionTrace:
        """Everything recorded so far, each half ordered by its own ordinal.

        Args:
            run_duration_ms: The whole run's duration, recorded on the trace.

        Returns:
            The trial's trace.
        """
        return ExecutionTrace(
            tool_calls=tuple(sorted(self._tool_calls, key=lambda call: call.ordinal)),
            model_turns=tuple(sorted(self._model_turns, key=lambda turn: turn.ordinal)),
            run_duration_ms=run_duration_ms,
        )
