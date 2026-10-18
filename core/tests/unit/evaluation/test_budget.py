"""The budget record's rules, read straight off a cycle state with no adapter in the way."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from core.ai.evaluation.contracts import (
    BudgetCoverageManifest,
    BudgetCycleState,
    BudgetNotOpenError,
    BudgetPreflightError,
    BudgetRefusal,
    BudgetRole,
    BudgetScope,
    EvaluationLimits,
    OtherCostObservation,
    cycle_snapshot,
    require_full_coverage,
)
from core.ai.evaluation.domain.budget import (
    BudgetAdmission,
    BudgetRequestRecord,
    BudgetTrialState,
    ChargeStatus,
    admission_refusal,
    attempt_refusal,
    other_cost_total,
    requests_of,
)

START = datetime(2026, 9, 16, 9, 0, tzinfo=UTC)
SCOPE = BudgetScope(cycle_id="cycle-1", trial_id="trial-1")
LIMITS = EvaluationLimits(
    cycle_cost_stop_usd=Decimal(10), trial_cost_stop_usd=Decimal(2), trial_deadline_seconds=180, trial_request_limit=16, trial_attempt_limit=3
)


def _request(request_id: str, *, charge: Decimal | None, settled: bool = True, trial_id: str = "trial-1", role: BudgetRole = "worker", number: int = 1) -> BudgetRequestRecord:
    admission = BudgetAdmission(
        scope=BudgetScope(cycle_id="cycle-1", trial_id=trial_id),
        request_id=request_id,
        role=role,
        admitted_at=START,
        trial_request_number=number,
    )
    if not settled:
        return BudgetRequestRecord(admission=admission, charge_status="pending")
    if charge is None:
        return BudgetRequestRecord(admission=admission, charge_status="unresolved", settled_at=START)
    return BudgetRequestRecord(admission=admission, charge_status="known", settled_at=START, charge_usd=charge)


def _state(*, requests: tuple[BudgetRequestRecord, ...] = (), request_ids: tuple[str, ...] = (), attempt_ids: tuple[str, ...] = ()) -> BudgetCycleState:
    trial = BudgetTrialState(
        trial_id="trial-1",
        started_at=START,
        deadline_at=START + timedelta(seconds=LIMITS.trial_deadline_seconds),
        attempt_ids=attempt_ids,
        request_ids=request_ids or tuple(record.admission.request_id for record in requests),
    )
    return BudgetCycleState(
        cycle_id="cycle-1", limits=LIMITS, shared_root="/srv/pilot-state", device_id=16, started_at=START, trials=(trial,), requests=requests
    )


def test_a_fresh_trial_admits() -> None:
    assert admission_refusal(_state(), SCOPE, at=START) is None


def test_the_sixteenth_request_is_admitted_and_the_seventeenth_is_refused() -> None:
    sixteen = tuple(_request(f"req-{index}", charge=Decimal("0.01"), number=index + 1) for index in range(16))
    assert admission_refusal(_state(requests=sixteen[:15]), SCOPE, at=START) is None
    assert admission_refusal(_state(requests=sixteen), SCOPE, at=START) == "request_limit"


def test_the_deadline_refuses_below_every_other_threshold() -> None:
    state = _state()
    at = START + timedelta(seconds=LIMITS.trial_deadline_seconds)
    assert admission_refusal(state, SCOPE, at=at - timedelta(seconds=1)) is None
    assert admission_refusal(state, SCOPE, at=at) == "deadline"


def test_a_request_in_flight_blocks_the_next_one() -> None:
    state = _state(requests=(_request("req-1", charge=None, settled=False),))
    assert admission_refusal(state, SCOPE, at=START) == "request_in_flight"


def test_an_unresolved_charge_blocks_every_later_admission() -> None:
    state = _state(requests=(_request("req-1", charge=None),))
    snapshot = cycle_snapshot(state, SCOPE)
    assert snapshot.unresolved_charge is True
    assert snapshot.trial_measured_model_spent_usd == Decimal(0)
    assert admission_refusal(state, SCOPE, at=START) == "unresolved_charge"


def test_a_crossing_trial_call_records_actual_spend_and_overshoot_then_refuses() -> None:
    crossed = _state(requests=(_request("req-1", charge=Decimal("1.90")), _request("req-2", charge=Decimal("0.25"), number=2)))
    snapshot = cycle_snapshot(crossed, SCOPE)
    assert snapshot.trial_measured_model_spent_usd == Decimal("2.15")
    assert snapshot.trial_measured_model_overshoot_usd == Decimal("0.15")
    assert admission_refusal(crossed, SCOPE, at=START) == "trial_cost_stop"


def test_a_crossing_cycle_call_records_actual_spend_and_overshoot_then_refuses() -> None:
    other_trial = _request("req-earlier", charge=Decimal("9.90"), trial_id="trial-0", role="designer")
    crossed = BudgetCycleState(
        cycle_id="cycle-1",
        limits=LIMITS,
        shared_root="/srv/pilot-state",
        device_id=16,
        started_at=START,
        trials=(
            BudgetTrialState(trial_id="trial-0", started_at=START, deadline_at=START + timedelta(seconds=180), request_ids=("req-earlier",)),
            BudgetTrialState(trial_id="trial-1", started_at=START, deadline_at=START + timedelta(seconds=180), request_ids=("req-2",)),
        ),
        requests=(other_trial, _request("req-2", charge=Decimal("0.30"))),
    )
    snapshot = cycle_snapshot(crossed, SCOPE)
    assert snapshot.cycle_measured_model_spent_usd == Decimal("10.20")
    assert snapshot.cycle_measured_model_overshoot_usd == Decimal("0.20")
    assert snapshot.trial_measured_model_spent_usd == Decimal("0.30")
    assert admission_refusal(crossed, SCOPE, at=START) == "cycle_cost_stop"


def test_every_role_spends_in_the_same_cycle_and_trial() -> None:
    roles: tuple[BudgetRole, ...] = ("designer", "worker", "reviewer", "grader")
    state = _state(requests=tuple(_request(f"req-{role}", charge=Decimal("0.10"), role=role, number=index + 1) for index, role in enumerate(roles)))
    snapshot = cycle_snapshot(state, SCOPE)
    assert snapshot.trial_requests == 4
    assert {record.admission.role for record in requests_of(state, "trial-1")} == set(roles)
    assert snapshot.trial_measured_model_spent_usd == snapshot.cycle_measured_model_spent_usd == Decimal("0.40")


@pytest.mark.parametrize(
    ("attempts", "attempt_id", "expected"),
    [
        pytest.param((), "attempt-1", None, id="first attempt"),
        pytest.param(("attempt-1", "attempt-2"), "attempt-3", None, id="third attempt allowed"),
        pytest.param(("attempt-1", "attempt-2", "attempt-3"), "attempt-4", "attempt_limit", id="fourth attempt refused"),
        pytest.param(("attempt-1", "attempt-2", "attempt-3"), "attempt-2", None, id="a repeat is never one too many"),
    ],
)
def test_attempts_stop_at_the_declared_limit(attempts: tuple[str, ...], attempt_id: str, expected: BudgetRefusal | None) -> None:
    assert attempt_refusal(_state(attempt_ids=attempts), SCOPE, attempt_id) == expected


def test_a_trial_that_was_never_begun_admits_nothing() -> None:
    with pytest.raises(BudgetNotOpenError):
        admission_refusal(_state(), BudgetScope(cycle_id="cycle-1", trial_id="trial-9"), at=START)


def test_a_manifest_that_covers_every_generative_path_passes() -> None:
    require_full_coverage(BudgetCoverageManifest(generative_model_paths=("designer", "worker"), budgeted_model_paths=("worker", "designer")))


def test_an_external_cli_agent_path_refuses_the_trial() -> None:
    manifest = BudgetCoverageManifest(
        generative_model_paths=("designer", "external-cli-agent"), budgeted_model_paths=("designer",), unmetered_model_paths=("external-cli-agent",)
    )
    with pytest.raises(BudgetPreflightError) as refused:
        require_full_coverage(manifest)
    assert refused.value.refusal == "unmetered_model_path"


def test_an_unwrapped_generative_path_refuses_the_trial() -> None:
    manifest = BudgetCoverageManifest(generative_model_paths=("designer", "grader"), budgeted_model_paths=("designer",))
    with pytest.raises(BudgetPreflightError) as refused:
        require_full_coverage(manifest)
    assert refused.value.refusal == "unbudgeted_model_path"
    assert "grader" in str(refused.value)


def test_a_manifest_naming_one_path_twice_is_refused() -> None:
    with pytest.raises(ValueError, match="duplicate generative model paths"):
        BudgetCoverageManifest(generative_model_paths=("designer", "designer"), budgeted_model_paths=("designer",))


def test_an_unknown_other_cost_stays_unknown_and_never_reaches_the_model_total() -> None:
    observations = (
        OtherCostObservation(scope=SCOPE, kind="embedding", cost_usd=Decimal("0.02"), reason="embedded 12 pages"),
        OtherCostObservation(scope=SCOPE, kind="third_party_api", reason="the host reported no figure"),
    )
    assert other_cost_total(observations) is None
    assert other_cost_total(observations[:1]) == Decimal("0.02")
    state = _state(requests=(_request("req-1", charge=Decimal("0.10")),))
    assert cycle_snapshot(state, SCOPE).cycle_measured_model_spent_usd == Decimal("0.10")


@pytest.mark.parametrize(
    ("charge_status", "settled_at", "charge_usd", "reason"),
    [
        pytest.param("pending", START, None, "pending request carries neither", id="pending with a settlement time"),
        pytest.param("known", START, None, "known charge carries the figure", id="known with no figure"),
        pytest.param("unresolved", START, Decimal("0.1"), "unresolved charge carries no figure", id="unresolved with a figure"),
        pytest.param("known", None, Decimal("0.1"), "settled request records when", id="settled with no time"),
    ],
)
def test_a_request_record_refuses_a_charge_state_that_contradicts_itself(
    charge_status: ChargeStatus, settled_at: datetime | None, charge_usd: Decimal | None, reason: str
) -> None:
    admission = BudgetAdmission(scope=SCOPE, request_id="req-1", role="worker", admitted_at=START, trial_request_number=1)
    with pytest.raises(ValueError, match=reason):
        BudgetRequestRecord(admission=admission, charge_status=charge_status, settled_at=settled_at, charge_usd=charge_usd)


def test_an_admission_time_with_no_timezone_is_refused_rather_than_compared() -> None:
    naive = datetime(2026, 9, 16, 9, 0)  # noqa: DTZ001 -- the subject under test is a timestamp with no timezone
    with pytest.raises(ValueError, match="an admission time must carry a timezone"):
        admission_refusal(_state(), SCOPE, at=naive)


def test_a_persisted_timestamp_with_no_timezone_is_refused() -> None:
    naive = datetime(2026, 9, 16, 9, 0)  # noqa: DTZ001 -- the subject under test is a timestamp with no timezone
    with pytest.raises(ValueError, match="admitted_at"):
        BudgetAdmission(scope=SCOPE, request_id="req-1", role="worker", admitted_at=naive, trial_request_number=1)


def test_a_cycle_naming_one_request_twice_is_refused() -> None:
    duplicated = (_request("req-1", charge=Decimal("0.1")), _request("req-1", charge=Decimal("0.2")))
    with pytest.raises(ValueError, match="duplicate request ids"):
        _state(requests=duplicated)
