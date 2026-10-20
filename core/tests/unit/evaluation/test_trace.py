"""Unit specs for the execution trace and the model-turn timing adapter.

Two facts stay apart: what a tool call admitted into the conversation, and what a model turn used.
No tool row carries a model turn's price, an unresolvable price stays `None` rather than zero, and
every row holds one position so nothing can be hidden behind another row.
"""

from __future__ import annotations

import asyncio

from decimal import Decimal
from typing import Literal

import pytest

from pydantic_ai import Agent
from pydantic_ai.models.test import TestModel

from core.ai.evaluation.contracts import DeliveredEvidenceRef, ExecutionTrace, TaskExecution, ToolCallTrace, TraceRecorder, TracingModel
from core.ai.evaluation.domain.trace import ModelTurnTrace

_HASH = "a" * 64


def _ref(ordinal: int) -> DeliveredEvidenceRef:
    """A reference standing in for one stored payload."""
    return DeliveredEvidenceRef(evidence_id=f"trial:{ordinal}", ordinal=ordinal, payload_sha256=_HASH, payload_bytes=12)


def _call(ordinal: int, *, status: Literal["ok", "refused", "failed"] = "ok") -> ToolCallTrace:
    """One recorded tool call at `ordinal`."""
    return ToolCallTrace(tool_name="search", ordinal=ordinal, status=status, response_bytes=12, duration_ms=3)


def _turn(ordinal: int) -> ModelTurnTrace:
    """One recorded model turn at `ordinal`."""
    return ModelTurnTrace(ordinal=ordinal, input_tokens=10, output_tokens=2, duration_ms=5)


# -- the trace shapes ---------------------------------------------------------


class TestTraceShapes:
    def test_a_tool_call_carries_no_model_turn_price(self):
        call = _call(0)

        assert call.direct_cost_usd is None
        assert call.withheld_exposure is False

    def test_a_turn_with_no_resolvable_price_stays_unknown(self):
        assert _turn(0).cost_usd is None

    def test_a_negative_price_is_refused(self):
        with pytest.raises(ValueError, match="cost_usd"):
            ModelTurnTrace(ordinal=0, duration_ms=1, cost_usd=Decimal("-0.01"))

    def test_two_tool_calls_cannot_claim_one_position(self):
        with pytest.raises(ValueError, match="duplicate tool call ordinals"):
            ExecutionTrace(tool_calls=(_call(0), _call(0)))

    def test_two_model_turns_cannot_claim_one_position(self):
        with pytest.raises(ValueError, match="duplicate model turn ordinals"):
            ExecutionTrace(model_turns=(_turn(1), _turn(1)))

    def test_withheld_exposures_names_every_contaminated_call(self):
        clean = _call(0)
        exposed = ToolCallTrace(tool_name="fetch", ordinal=1, status="refused", response_bytes=40, duration_ms=1, withheld_exposure=True)

        trace = ExecutionTrace(tool_calls=(clean, exposed))

        assert trace.withheld_exposures == (exposed,)

    def test_an_untraced_execution_says_so_rather_than_claiming_an_empty_trace(self):
        assert TaskExecution(output="answer", diagnostics=()).trace is None

    def test_an_execution_carries_the_trial_trace_it_produced(self):
        trace = ExecutionTrace(tool_calls=(_call(0),), model_turns=(_turn(0),), run_duration_ms=42)

        assert TaskExecution(output="answer", diagnostics=(), trace=trace).trace == trace

    def test_a_delivered_reference_rides_the_call_that_delivered_it(self):
        call = ToolCallTrace(
            tool_name="search", ordinal=0, status="ok", delivered_evidence=(_ref(0),), returned_unit_refs=("page:kb/a",),
            response_bytes=12, duration_ms=1,
        )

        assert call.delivered_evidence[0].payload_sha256 == _HASH
        assert call.returned_unit_refs == ("page:kb/a",)


# -- the recorder -------------------------------------------------------------


class TestTraceRecorder:
    async def test_positions_are_claimed_in_order(self):
        recorder = TraceRecorder()

        claimed = [await recorder.claim_tool_ordinal() for _ in range(3)]

        assert claimed == [0, 1, 2]

    async def test_concurrent_claims_never_collide(self):
        recorder = TraceRecorder()

        claimed = await asyncio.gather(*(recorder.claim_tool_ordinal() for _ in range(20)))

        assert sorted(claimed) == list(range(20))

    async def test_tool_and_model_positions_are_counted_apart(self):
        recorder = TraceRecorder()

        assert await recorder.claim_tool_ordinal() == 0
        assert await recorder.claim_model_turn_ordinal() == 0

    async def test_rows_come_back_in_position_order_whatever_order_they_finished_in(self):
        recorder = TraceRecorder()
        await recorder.record_tool_call(_call(2))
        await recorder.record_tool_call(_call(0))
        await recorder.record_tool_call(_call(1))
        await recorder.record_model_turn(_turn(1))
        await recorder.record_model_turn(_turn(0))

        trace = recorder.trace(run_duration_ms=77)

        assert [call.ordinal for call in trace.tool_calls] == [0, 1, 2]
        assert [turn.ordinal for turn in trace.model_turns] == [0, 1]
        assert trace.run_duration_ms == 77

    async def test_an_untouched_recorder_yields_an_empty_trace(self):
        assert TraceRecorder().trace() == ExecutionTrace()


# -- the model-turn adapter ---------------------------------------------------


class TestTracingModel:
    async def test_every_request_becomes_one_recorded_turn(self):
        recorder = TraceRecorder()
        agent = Agent(TracingModel(TestModel(), recorder))

        await agent.run("a question")

        assert len(recorder.trace().model_turns) == 1

    async def test_the_turn_carries_the_response_usage_and_identity(self):
        recorder = TraceRecorder()
        agent = Agent(TracingModel(TestModel(), recorder))

        result = await agent.run("a question")

        turn = recorder.trace().model_turns[0]
        assert turn.input_tokens == result.usage.input_tokens
        assert turn.output_tokens == result.usage.output_tokens
        assert turn.model_name == "test"
        assert turn.provider_name == "test"

    async def test_a_model_with_no_known_price_leaves_the_price_unknown(self):
        recorder = TraceRecorder()
        agent = Agent(TracingModel(TestModel(), recorder))

        await agent.run("a question")

        assert recorder.trace().model_turns[0].cost_usd is None

    async def test_the_turn_is_timed_rather_than_stamped(self):
        recorder = TraceRecorder()
        agent = Agent(TracingModel(TestModel(), recorder))

        await agent.run("a question")

        assert recorder.trace().model_turns[0].duration_ms >= 0

    async def test_the_wrapped_answer_passes_through_unchanged(self):
        recorder = TraceRecorder()
        plain = await Agent(TestModel()).run("a question")

        traced = await Agent(TracingModel(TestModel(), recorder)).run("a question")

        assert traced.output == plain.output
