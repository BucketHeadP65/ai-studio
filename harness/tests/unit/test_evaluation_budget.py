"""Every plain model request of a measured trial asks the record first, and settles once after.

The record is the real file-backed one over a temporary directory, so what these rows prove is what
a pilot would actually persist. No request reaches a provider: every model here is a `FunctionModel`
double and every charge comes from a resolver the test hands in.
"""

from __future__ import annotations

import asyncio

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import TYPE_CHECKING

import pytest

from core.ai.evaluation.contracts import (
    BudgetCoverageManifest,
    BudgetPreflightError,
    BudgetRefusedError,
    BudgetRole,
    BudgetScope,
    FileEvaluationBudgetRecord,
    OtherCostObservation,
    ResponseCharge,
    cycle_snapshot,
)
from core.ai.evaluation.domain.budget import other_cost_total
from pydantic_ai import Agent
from pydantic_ai.messages import ModelResponse, TextPart
from pydantic_ai.models import ModelRequestContext, ModelRequestParameters
from pydantic_ai.models.function import FunctionModel
from pydantic_ai.usage import RequestUsage
from pydantic_ai_harness.spend import Budget, SpendLimitExceeded, SpendLimits

from harness.features.evals.domain.pilot import (
    WORKFLOW_AUTOMATION_DEVELOPMENT_LIMITS,
    PilotParticipant,
    PilotPreflight,
    PilotRoleModel,
    check_pilot,
)
from harness.features.evals.exceptions import UnmeteredModelCallError
from harness.features.evals.infrastructure.budgeted_model import BudgetedModel
from harness.features.evals.service.pilot import pilot_preflight, roles_without_price, start_pilot_cycle
from tests.support.fakes.evaluation_budget import GatedLedger

if TYPE_CHECKING:
    from collections.abc import Sequence
    from pathlib import Path

    from pydantic_ai.messages import ModelMessage
    from pydantic_ai.models.function import AgentInfo, FunctionDef
    from pydantic_ai.settings import ModelSettings

START = datetime(2026, 9, 16, 9, 0, tzinfo=UTC)
CYCLE = "cycle-wa-pilot"
SCOPE = BudgetScope(cycle_id=CYCLE, trial_id="trial-1")
LIMITS = WORKFLOW_AUTOMATION_DEVELOPMENT_LIMITS
COVERAGE = BudgetCoverageManifest(
    generative_model_paths=("designer", "worker", "reviewer", "grader"), budgeted_model_paths=("designer", "worker", "reviewer", "grader")
)


def _answer(messages: Sequence[ModelMessage], info: AgentInfo) -> ModelResponse:
    del messages, info
    return ModelResponse(parts=[TextPart(content="done")], usage=RequestUsage(input_tokens=10, output_tokens=5))


def _failing(messages: Sequence[ModelMessage], info: AgentInfo) -> ModelResponse:
    del messages, info
    msg = "the provider hung up"
    raise TimeoutError(msg)


def _charging(amount: Decimal) -> ResponseCharge:
    def charge(response: ModelResponse) -> Decimal | None:
        del response
        return amount

    return charge


def _unpriceable(response: ModelResponse) -> Decimal | None:
    del response
    return None


class RecordingModel(FunctionModel):
    """A `FunctionModel` that writes down every path a caller reaches it through."""

    def __init__(self, function: FunctionDef, entered: list[str]) -> None:
        super().__init__(function=function)
        self.entered = entered

    async def count_tokens(
        self, messages: list[ModelMessage], model_settings: ModelSettings | None, model_request_parameters: ModelRequestParameters
    ) -> RequestUsage:
        """Record the call a real provider would have been asked to answer."""
        del messages, model_settings, model_request_parameters
        self.entered.append("count_tokens")
        return RequestUsage(input_tokens=10)

    async def compact_messages(self, request_context: ModelRequestContext, *, instructions: str | None = None) -> ModelResponse:
        """Record the call a real provider would have billed for."""
        del request_context, instructions
        self.entered.append("compact_messages")
        return ModelResponse(parts=[TextPart(content="compacted")])


async def _opened(root: Path) -> FileEvaluationBudgetRecord:
    ledger = FileEvaluationBudgetRecord(root)
    await ledger.begin_cycle(CYCLE, LIMITS, pricing_known=True, at=START)
    await ledger.begin_trial(SCOPE, at=START)
    return ledger


def _budgeted(
    ledger: FileEvaluationBudgetRecord, role: BudgetRole, *, charge: ResponseCharge | None = None, function: FunctionDef = _answer
) -> BudgetedModel:
    return BudgetedModel(
        FunctionModel(function=function), record=ledger, scope=SCOPE, role=role, charge=charge or _charging(Decimal("0.01")), clock=lambda: START
    )


async def _request(model: BudgetedModel) -> ModelResponse:
    return await model.request([], None, ModelRequestParameters())


def test_the_development_pilot_limits_are_the_declared_ones() -> None:
    assert LIMITS.cycle_cost_stop_usd == Decimal(10)
    assert LIMITS.trial_cost_stop_usd == Decimal(2)
    assert LIMITS.trial_deadline_seconds == 180
    assert LIMITS.trial_request_limit == 16
    assert LIMITS.trial_attempt_limit == 3


async def test_two_nested_roles_spend_one_trial_budget_between_them(tmp_path: Path) -> None:
    ledger = await _opened(tmp_path)
    designer = _budgeted(ledger, "designer")
    worker = _budgeted(ledger, "worker")
    for index in range(16):
        await _request(designer if index % 2 else worker)
    with pytest.raises(BudgetRefusedError) as refused:
        await _request(designer)
    assert refused.value.refusal == "request_limit"
    state = await ledger.read(CYCLE)
    assert state is not None
    assert len(state.requests) == 16, "the refused request is not recorded as one that was made"


async def test_every_role_settles_into_the_same_cycle_and_trial(tmp_path: Path) -> None:
    ledger = await _opened(tmp_path)
    roles: tuple[BudgetRole, ...] = ("designer", "worker", "reviewer", "grader")
    for role in roles:
        await _request(_budgeted(ledger, role))
    state = await ledger.read(CYCLE)
    assert state is not None
    assert {record.admission.role for record in state.requests} == set(roles)
    assert all(record.admission.scope == SCOPE for record in state.requests), "every role's request belongs to the one trial"
    snapshot = cycle_snapshot(state, SCOPE)
    assert snapshot.trial_requests == 4
    assert snapshot.trial_measured_model_spent_usd == Decimal("0.04")


async def test_a_provider_failure_settles_unresolved_and_blocks_the_next_request(tmp_path: Path) -> None:
    ledger = await _opened(tmp_path)
    with pytest.raises(TimeoutError):
        await _request(_budgeted(ledger, "worker", function=_failing))
    state = await ledger.read(CYCLE)
    assert state is not None
    assert [record.charge_status for record in state.requests] == ["unresolved"]
    with pytest.raises(BudgetRefusedError) as refused:
        await _request(_budgeted(ledger, "worker"))
    assert refused.value.refusal == "unresolved_charge"


async def test_a_response_the_resolver_cannot_price_settles_unresolved(tmp_path: Path) -> None:
    ledger = await _opened(tmp_path)
    await _request(_budgeted(ledger, "grader", charge=_unpriceable))
    state = await ledger.read(CYCLE)
    assert state is not None
    assert [record.charge_status for record in state.requests] == ["unresolved"]
    assert cycle_snapshot(state, SCOPE).cycle_measured_model_spent_usd == Decimal(0)


async def test_a_crossing_call_records_its_overshoot_and_stops_the_trial(tmp_path: Path) -> None:
    ledger = await _opened(tmp_path)
    await _request(_budgeted(ledger, "worker", charge=_charging(Decimal("1.90"))))
    await _request(_budgeted(ledger, "reviewer", charge=_charging(Decimal("0.25"))))
    state = await ledger.read(CYCLE)
    assert state is not None
    snapshot = cycle_snapshot(state, SCOPE)
    assert snapshot.trial_measured_model_spent_usd == Decimal("2.15")
    assert snapshot.trial_measured_model_overshoot_usd == Decimal("0.15")
    with pytest.raises(BudgetRefusedError) as refused:
        await _request(_budgeted(ledger, "worker"))
    assert refused.value.refusal == "trial_cost_stop"


async def test_a_budgeted_model_mounted_on_an_agent_records_the_request_the_run_made(tmp_path: Path) -> None:
    ledger = await _opened(tmp_path)
    agent = Agent(_budgeted(ledger, "designer"), output_type=str)
    result = await agent.run("draft one")
    assert result.output == "done"
    state = await ledger.read(CYCLE)
    assert state is not None
    assert [(record.admission.role, record.charge_status, record.charge_usd) for record in state.requests] == [("designer", "known", Decimal("0.01"))]


async def test_the_deployments_own_spend_brake_refuses_before_the_record_admits_anything(tmp_path: Path) -> None:
    ledger = await _opened(tmp_path)
    spend = SpendLimits[object](budgets=[Budget(usd=Decimal("0.001"), window="day")], price=lambda response: Decimal("0.05"))
    agent: Agent[object, str] = Agent(_budgeted(ledger, "worker"), deps_type=object, output_type=str, capabilities=[spend])
    assert (await agent.run("once")).output == "done"
    with pytest.raises(SpendLimitExceeded):
        await agent.run("twice")
    state = await ledger.read(CYCLE)
    assert state is not None
    assert len(state.requests) == 1, "a request the stricter brake refused never reached the record"


async def test_a_trial_the_resolver_cannot_price_never_opens(tmp_path: Path) -> None:
    ledger = FileEvaluationBudgetRecord(tmp_path)
    preflight = _preflight(tmp_path, unpriced=("grader",))
    with pytest.raises(BudgetPreflightError) as refused:
        await start_pilot_cycle(ledger, preflight, at=START)
    assert refused.value.refusal == "pricing_unknown"
    assert await ledger.read(CYCLE) is None


async def test_a_measurable_trial_opens_its_cycle(tmp_path: Path) -> None:
    ledger = FileEvaluationBudgetRecord(tmp_path)
    await start_pilot_cycle(ledger, _preflight(tmp_path), at=START)
    state = await ledger.read(CYCLE)
    assert state is not None
    assert state.limits == LIMITS


def test_an_unwrapped_model_path_refuses_the_pilot(tmp_path: Path) -> None:
    coverage = BudgetCoverageManifest(
        generative_model_paths=("designer", "external-cli-agent"),
        budgeted_model_paths=("designer",),
        unmetered_model_paths=("external-cli-agent",),
    )
    with pytest.raises(BudgetPreflightError) as refused:
        check_pilot(_preflight(tmp_path, coverage=coverage))
    assert refused.value.refusal == "unmetered_model_path"


@pytest.mark.parametrize(
    ("participant", "expected"),
    [
        pytest.param(
            PilotParticipant(name="temporal worker", canonical_state_root="/srv/pilot-state", device_id=None, cooperative_locking=True),
            "unverified_state_root",
            id="a remote worker cannot read the device at all",
        ),
        pytest.param(
            PilotParticipant(name="other host", canonical_state_root="/srv/pilot-state", device_id=4_242, cooperative_locking=True),
            "unverified_state_root",
            id="another device behind the same path",
        ),
        pytest.param(
            PilotParticipant(name="grader", canonical_state_root="/srv/pilot-state", device_id=16, cooperative_locking=False),
            "no_cooperative_lock",
            id="a participant that cannot lock",
        ),
    ],
)
def test_a_participant_that_cannot_verify_the_shared_root_refuses_the_pilot(tmp_path: Path, participant: PilotParticipant, expected: str) -> None:
    preflight = _preflight(tmp_path).model_copy(
        update={"canonical_state_root": "/srv/pilot-state", "device_id": 16, "participants": (participant,)}
    )
    with pytest.raises(BudgetPreflightError) as refused:
        check_pilot(preflight)
    assert refused.value.refusal == expected


def test_the_price_resolver_names_a_configured_role_or_the_role_is_reported_unpriced() -> None:
    configured = (
        PilotRoleModel(role="designer", model_name="gpt-4o", provider_name="openai"),
        PilotRoleModel(role="grader", model_name="a-model-nothing-sells", provider_name="openai"),
    )
    assert roles_without_price(configured, at=START) == ("grader",)


async def test_an_embedding_charge_nothing_priced_stays_outside_the_model_total(tmp_path: Path) -> None:
    ledger = await _opened(tmp_path)
    await _request(_budgeted(ledger, "worker"))
    observations = (
        OtherCostObservation(scope=SCOPE, kind="embedding", reason="the embedding host reported no figure"),
        OtherCostObservation(scope=SCOPE, kind="third_party_api", cost_usd=Decimal("0.05"), reason="one lookup, billed per call"),
    )
    assert other_cost_total(observations) is None
    state = await ledger.read(CYCLE)
    assert state is not None
    assert cycle_snapshot(state, SCOPE).cycle_measured_model_spent_usd == Decimal("0.01"), "an observation of its own never enters the model total"


async def test_a_cancelled_request_still_settles_the_charge_it_already_knew(tmp_path: Path) -> None:
    ledger = GatedLedger(await _opened(tmp_path))
    model = BudgetedModel(
        FunctionModel(function=_answer), record=ledger, scope=SCOPE, role="worker", charge=_charging(Decimal("0.07")), clock=lambda: START
    )
    answering = asyncio.create_task(_request(model))
    await ledger.settling.wait()
    answering.cancel()
    ledger.release.set()
    with pytest.raises(asyncio.CancelledError):
        await answering
    await asyncio.wait_for(ledger.settled.wait(), timeout=5)
    state = await FileEvaluationBudgetRecord(tmp_path).read(CYCLE)
    assert state is not None
    assert [(record.charge_status, record.charge_usd) for record in state.requests] == [("known", Decimal("0.07"))]


async def test_counting_tokens_under_a_budgeted_scope_is_refused_before_the_provider(tmp_path: Path) -> None:
    ledger = await _opened(tmp_path)
    entered: list[str] = []
    model = BudgetedModel(
        RecordingModel(_answer, entered), record=ledger, scope=SCOPE, role="worker", charge=_charging(Decimal("0.01")), clock=lambda: START
    )
    with pytest.raises(UnmeteredModelCallError):
        await model.count_tokens([], None, ModelRequestParameters())
    assert entered == [], "a path the record cannot meter never reaches the provider"
    state = await ledger.read(CYCLE)
    assert state is not None
    assert state.requests == (), "and nothing was admitted for it either"


async def test_compacting_messages_under_a_budgeted_scope_is_refused_before_the_provider(tmp_path: Path) -> None:
    ledger = await _opened(tmp_path)
    entered: list[str] = []
    wrapped = RecordingModel(_answer, entered)
    model = BudgetedModel(wrapped, record=ledger, scope=SCOPE, role="designer", charge=_charging(Decimal("0.01")), clock=lambda: START)
    context = ModelRequestContext(model=wrapped, messages=[], model_settings=None, model_request_parameters=ModelRequestParameters())
    with pytest.raises(UnmeteredModelCallError):
        await model.compact_messages(context)
    assert entered == [], "compaction is a billed generative request, so it is refused rather than forwarded"
    state = await ledger.read(CYCLE)
    assert state is not None
    assert state.requests == ()


async def test_a_preflight_built_from_the_record_opens_the_cycle(tmp_path: Path) -> None:
    ledger = FileEvaluationBudgetRecord(tmp_path)
    preflight = pilot_preflight(
        ledger, cycle_id=CYCLE, limits=LIMITS, coverage=COVERAGE, participants=(_participant(ledger),)
    )
    await start_pilot_cycle(ledger, preflight, at=START)
    state = await ledger.read(CYCLE)
    assert state is not None
    assert (state.shared_root, state.device_id) == (preflight.canonical_state_root, preflight.device_id)


@pytest.mark.parametrize(
    ("root", "device"),
    [
        pytest.param("/srv/another-pilot-state", None, id="everyone verified against another root"),
        pytest.param(None, 4_242, id="everyone verified against another device"),
    ],
)
async def test_a_preflight_verified_elsewhere_never_opens_a_cycle_here(tmp_path: Path, root: str | None, device: int | None) -> None:
    """The participants agree with each other and with the preflight, and all of them are elsewhere.

    This is the shape that gets past every participant check: one configured root, every process
    verified against it, and a record constructed on a different path.
    """
    ledger = FileEvaluationBudgetRecord(tmp_path)
    here = ledger.state_identity()
    elsewhere_root = root or here.canonical_state_root
    elsewhere_device = device if device is not None else here.device_id
    elsewhere = PilotPreflight(
        cycle_id=CYCLE,
        limits=LIMITS,
        canonical_state_root=elsewhere_root,
        device_id=elsewhere_device,
        coverage=COVERAGE,
        participants=(
            PilotParticipant(name="studio", canonical_state_root=elsewhere_root, device_id=elsewhere_device, cooperative_locking=True),
        ),
    )
    check_pilot(elsewhere)
    with pytest.raises(BudgetPreflightError) as refused:
        await start_pilot_cycle(ledger, elsewhere, at=START)
    assert refused.value.refusal == "unverified_state_root"
    assert await ledger.read(CYCLE) is None, "nothing is opened on a root the participants were never checked against"


def _participant(ledger: FileEvaluationBudgetRecord) -> PilotParticipant:
    identity = ledger.state_identity()
    return PilotParticipant(
        name="studio", canonical_state_root=identity.canonical_state_root, device_id=identity.device_id, cooperative_locking=True
    )


def _preflight(root: Path, *, coverage: BudgetCoverageManifest = COVERAGE, unpriced: tuple[BudgetRole, ...] = ()) -> PilotPreflight:
    directory = root / "evaluation-budgets"
    directory.mkdir(parents=True, exist_ok=True)
    return PilotPreflight(
        cycle_id=CYCLE,
        limits=LIMITS,
        canonical_state_root=str(root.resolve()),
        device_id=directory.stat().st_dev,
        coverage=coverage,
        participants=(
            PilotParticipant(
                name="studio", canonical_state_root=str(root.resolve()), device_id=directory.stat().st_dev, cooperative_locking=True
            ),
        ),
        roles_without_price=unpriced,
    )


async def test_a_trial_deadline_refuses_a_later_request(tmp_path: Path) -> None:
    ledger = await _opened(tmp_path)
    late = BudgetedModel(
        FunctionModel(function=_answer),
        record=ledger,
        scope=SCOPE,
        role="worker",
        charge=_charging(Decimal("0.01")),
        clock=lambda: START + timedelta(seconds=LIMITS.trial_deadline_seconds),
    )
    with pytest.raises(BudgetRefusedError) as refused:
        await _request(late)
    assert refused.value.refusal == "deadline"
