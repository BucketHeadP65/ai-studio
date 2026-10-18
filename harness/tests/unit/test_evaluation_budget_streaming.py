"""A streamed request is admitted once before the provider and settled once however it ends.

A stream has more endings than a plain request: it can run out naturally, be dropped half way, be
cancelled, fail while it is being read, or fail before the provider answers at all. Only the first
of those knows what it cost. Every other one settles unresolved, once, and an unresolved charge
blocks the trial until a person looks at it.
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from typing import TYPE_CHECKING

import pytest

from core.ai.evaluation.contracts import BudgetRefusedError, BudgetScope, FileEvaluationBudgetRecord, cycle_snapshot
from pydantic_ai import Agent
from pydantic_ai.messages import ModelResponse, TextPart
from pydantic_ai.models import ModelRequestParameters
from pydantic_ai.usage import RequestUsage

from harness.features.evals.domain.pilot import WORKFLOW_AUTOMATION_DEVELOPMENT_LIMITS
from harness.features.evals.infrastructure.budgeted_model import BudgetedModel
from tests.support.fakes.evaluation_budget import CountingLedger, LostLedger, SettlementLostError
from tests.support.streaming import ScriptedStreamModel, usage_preserving_function_model

if TYPE_CHECKING:
    from collections.abc import Sequence
    from pathlib import Path

    from core.ai.evaluation.contracts import BudgetCycleState, EvaluationBudgetRecord, ResponseCharge
    from pydantic_ai.messages import ModelMessage
    from pydantic_ai.models import Model
    from pydantic_ai.models.function import AgentInfo

START = datetime(2026, 9, 16, 9, 0, tzinfo=UTC)
CYCLE = "cycle-wa-pilot"
SCOPE = BudgetScope(cycle_id=CYCLE, trial_id="trial-1")
LIMITS = WORKFLOW_AUTOMATION_DEVELOPMENT_LIMITS
CHARGE = Decimal("0.05")


def _answer(messages: Sequence[ModelMessage], info: AgentInfo) -> ModelResponse:
    del messages, info
    return ModelResponse(parts=[TextPart(content="a drafted workflow")], usage=RequestUsage(input_tokens=120, output_tokens=40))


def _charge(response: ModelResponse) -> Decimal | None:
    del response
    return CHARGE


async def _opened(root: Path) -> CountingLedger:
    ledger = FileEvaluationBudgetRecord(root)
    await ledger.begin_cycle(CYCLE, LIMITS, pricing_known=True, at=START)
    await ledger.begin_trial(SCOPE, at=START)
    return CountingLedger(ledger)


def _budgeted(ledger: EvaluationBudgetRecord, *, wrapped: Model | None = None, charge: ResponseCharge = _charge) -> BudgetedModel:
    return BudgetedModel(
        wrapped or usage_preserving_function_model(_answer), record=ledger, scope=SCOPE, role="designer", charge=charge, clock=lambda: START
    )


async def _drained(model: BudgetedModel) -> None:
    """One streamed request, read to the end, however it chooses to end."""
    async with model.request_stream([], None, ModelRequestParameters()) as stream:
        async for _ in stream:
            pass


async def _entered(model: BudgetedModel) -> None:
    """One streamed request whose events are never read at all."""
    async with model.request_stream([], None, ModelRequestParameters()):
        pass


async def _state(root: Path) -> BudgetCycleState:
    state = await FileEvaluationBudgetRecord(root).read(CYCLE)
    assert state is not None, "the cycle was opened, so its record is on disk"
    return state


async def test_a_stream_that_runs_out_naturally_is_priced_and_settled_once(tmp_path: Path) -> None:
    ledger = await _opened(tmp_path)
    async with _budgeted(ledger).request_stream([], None, ModelRequestParameters()) as stream:
        async for _ in stream:
            pass
        assert stream.get().state == "complete"
    assert ledger.settlements == [(ledger.admitted[0], CHARGE)]
    state = await _state(tmp_path)
    assert [(record.charge_status, record.charge_usd) for record in state.requests] == [("known", CHARGE)]
    assert cycle_snapshot(state, SCOPE).in_flight_request_id is None


async def test_a_consumer_that_drops_the_stream_early_settles_unresolved_once(tmp_path: Path) -> None:
    ledger = await _opened(tmp_path)
    async with _budgeted(ledger).request_stream([], None, ModelRequestParameters()) as stream:
        async for _ in stream:
            break
    assert ledger.settlements == [(ledger.admitted[0], None)]
    state = await _state(tmp_path)
    assert [record.charge_status for record in state.requests] == ["unresolved"]


async def test_a_cancelled_stream_settles_unresolved_once(tmp_path: Path) -> None:
    ledger = await _opened(tmp_path)
    async with _budgeted(ledger).request_stream([], None, ModelRequestParameters()) as stream:
        await stream.cancel()
        assert stream.get().state == "interrupted"
    assert ledger.settlements == [(ledger.admitted[0], None)]
    assert [record.charge_status for record in (await _state(tmp_path)).requests] == ["unresolved"]


async def test_a_stream_that_fails_while_it_is_read_settles_unresolved_once(tmp_path: Path) -> None:
    ledger = await _opened(tmp_path)
    failing = ScriptedStreamModel(_answer, iteration_error=ConnectionError("the stream died half way"))
    with pytest.raises(ConnectionError):
        await _drained(_budgeted(ledger, wrapped=failing))
    assert ledger.settlements == [(ledger.admitted[0], None)]
    assert [record.charge_status for record in (await _state(tmp_path)).requests] == ["unresolved"]


async def test_a_provider_that_fails_before_answering_settles_unresolved_once(tmp_path: Path) -> None:
    ledger = await _opened(tmp_path)
    refusing = ScriptedStreamModel(_answer, entry_error=TimeoutError("no headers ever arrived"))
    with pytest.raises(TimeoutError):
        await _entered(_budgeted(ledger, wrapped=refusing))
    assert ledger.settlements == [(ledger.admitted[0], None)]
    state = await _state(tmp_path)
    assert [record.charge_status for record in state.requests] == ["unresolved"]
    assert cycle_snapshot(state, SCOPE).unresolved_charge is True


async def test_leaving_the_context_after_an_earlier_settlement_settles_nothing_twice(tmp_path: Path) -> None:
    ledger = await _opened(tmp_path)
    async with _budgeted(ledger).request_stream([], None, ModelRequestParameters()) as stream:
        async for _ in stream:
            pass
    assert len(ledger.settlements) == 1, "the exit path never settles a request the success path already settled"
    state = await _state(tmp_path)
    assert len(state.requests) == 1
    assert state.requests[0].settled_at == START


async def test_a_stream_the_record_refuses_never_reaches_the_provider(tmp_path: Path) -> None:
    ledger = await _opened(tmp_path)
    entered: list[str] = []

    def entering(messages: Sequence[ModelMessage], info: AgentInfo) -> ModelResponse:
        entered.append("request")
        return _answer(messages, info)

    model = _budgeted(ledger, wrapped=usage_preserving_function_model(entering), charge=lambda response: Decimal("0.01"))
    for _ in range(16):
        await _drained(model)
    assert len(entered) == 16
    with pytest.raises(BudgetRefusedError) as refused:
        await _entered(model)
    assert refused.value.refusal == "request_limit"
    assert len(entered) == 16, "the seventeenth request was refused before the provider context was entered"


async def test_streamed_and_plain_requests_count_against_one_trial(tmp_path: Path) -> None:
    ledger = await _opened(tmp_path)
    streamed = _budgeted(ledger, charge=lambda response: Decimal("0.01"))
    plain = BudgetedModel(
        usage_preserving_function_model(_answer),
        record=ledger,
        scope=SCOPE,
        role="worker",
        charge=lambda response: Decimal("0.01"),
        clock=lambda: START,
    )
    for index in range(16):
        if index % 2:
            await plain.request([], None, ModelRequestParameters())
        else:
            await _drained(streamed)
    state = await _state(tmp_path)
    assert cycle_snapshot(state, SCOPE).trial_requests == 16
    assert {record.admission.role for record in state.requests} == {"designer", "worker"}
    with pytest.raises(BudgetRefusedError):
        await plain.request([], None, ModelRequestParameters())


async def test_a_designer_streaming_events_through_an_agent_settles_in_the_shared_trial(tmp_path: Path) -> None:
    ledger = await _opened(tmp_path)
    agent: Agent[None, str] = Agent(_budgeted(ledger), output_type=str)
    async with agent.run_stream_events("draft one") as stream:
        events = [event async for event in stream]
    assert events, "the agent streamed its run"
    state = await _state(tmp_path)
    assert [(record.admission.role, record.charge_status, record.charge_usd) for record in state.requests] == [("designer", "known", CHARGE)]


async def test_a_settlement_that_never_lands_leaves_the_request_in_flight_across_a_restart(tmp_path: Path) -> None:
    opened = await _opened(tmp_path)
    lost = LostLedger(opened.ledger)
    with pytest.raises(SettlementLostError):
        await _drained(_budgeted(lost))
    restarted = FileEvaluationBudgetRecord(tmp_path)
    state = await _state(tmp_path)
    assert [record.charge_status for record in state.requests] == ["pending"]
    assert cycle_snapshot(state, SCOPE).in_flight_request_id == lost.admitted[0]
    with pytest.raises(BudgetRefusedError) as refused:
        await restarted.admit(SCOPE, "req-after-restart", "worker", at=START)
    assert refused.value.refusal == "request_in_flight"
