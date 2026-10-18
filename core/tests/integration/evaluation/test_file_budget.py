"""The file-backed budget record: one document, real processes, and a restart that changes nothing.

Every row here uses a real temporary POSIX directory. The concurrent rows start real processes with
the spawn method, so two admissions genuinely race for one cooperative lock rather than taking turns
inside one interpreter. Nothing here needs a database.
"""

from __future__ import annotations

import asyncio
import multiprocessing

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import TYPE_CHECKING

import pytest

from core.ai.evaluation.contracts import (
    BudgetConflictError,
    BudgetCycleState,
    BudgetNotOpenError,
    BudgetPreflightError,
    BudgetRefusedError,
    BudgetScope,
    EvaluationLimits,
    FileEvaluationBudgetRecord,
    content_fingerprint,
)
from core.ai.evaluation.exceptions import BudgetUnreadableError

if TYPE_CHECKING:
    from pathlib import Path

START = datetime(2026, 9, 16, 9, 0, tzinfo=UTC)
CYCLE = "cycle-pilot"
SCOPE = BudgetScope(cycle_id=CYCLE, trial_id="trial-1")
LIMITS = EvaluationLimits(
    cycle_cost_stop_usd=Decimal(10), trial_cost_stop_usd=Decimal(2), trial_deadline_seconds=180, trial_request_limit=16, trial_attempt_limit=3
)
CHILD_SECONDS = 30


async def _opened(root: Path) -> FileEvaluationBudgetRecord:
    ledger = FileEvaluationBudgetRecord(root)
    await ledger.begin_cycle(CYCLE, LIMITS, pricing_known=True, at=START)
    await ledger.begin_trial(SCOPE, at=START)
    return ledger


def admit_in_child(root: str, request_id: str, results: multiprocessing.Queue) -> None:
    """Admit one request from a process of its own and report what the record answered."""
    from pathlib import Path

    from core.ai.evaluation.contracts import BudgetRefusedError, BudgetScope, FileEvaluationBudgetRecord

    ledger = FileEvaluationBudgetRecord(Path(root))
    scope = BudgetScope(cycle_id=CYCLE, trial_id="trial-1")
    try:
        admission = asyncio.run(ledger.admit(scope, request_id, "worker", at=START))
    except BudgetRefusedError as refused:
        results.put(("refused", refused.refusal))
    else:
        results.put(("admitted", admission.request_id))


def _run_children(root: Path, request_ids: tuple[str, ...]) -> list[tuple[str, str]]:
    context = multiprocessing.get_context("spawn")
    results: multiprocessing.Queue = context.Queue()
    workers = [context.Process(target=admit_in_child, args=(str(root), request_id, results)) for request_id in request_ids]
    for worker in workers:
        worker.start()
    for worker in workers:
        worker.join(CHILD_SECONDS)
        assert worker.exitcode == 0, f"a worker process ended with exit code {worker.exitcode}"
    return [results.get(timeout=CHILD_SECONDS) for _ in request_ids]


async def _opened_and_read(root: Path) -> BudgetCycleState:
    """The record as it stands the moment a cycle and its trial have been opened on `root`."""
    ledger = await _opened(root)
    state = await ledger.read(CYCLE)
    assert state is not None, "the cycle was just opened, so its record is on disk"
    return state


async def test_the_record_is_one_document_named_by_the_hash_of_its_cycle(tmp_path: Path) -> None:
    await _opened(tmp_path)
    directory = tmp_path / "evaluation-budgets"
    names = {path.name for path in directory.iterdir()}
    assert names == {f"{content_fingerprint(CYCLE)}.json", f"{content_fingerprint(CYCLE)}.lock"}
    assert not any(CYCLE in name for name in names), "a cycle id never becomes a path segment"


async def test_unknown_pricing_refuses_the_cycle_before_anything_is_written(tmp_path: Path) -> None:
    ledger = FileEvaluationBudgetRecord(tmp_path)
    with pytest.raises(BudgetPreflightError) as refused:
        await ledger.begin_cycle(CYCLE, LIMITS, pricing_known=False, at=START)
    assert refused.value.refusal == "pricing_unknown"
    assert await ledger.read(CYCLE) is None


async def test_reopening_a_cycle_and_a_trial_is_idempotent_and_resets_no_counter(tmp_path: Path) -> None:
    ledger = await _opened(tmp_path)
    admission = await ledger.admit(SCOPE, "req-1", "designer", at=START)
    await ledger.settle(admission, Decimal("0.40"), at=START)
    await ledger.begin_cycle(CYCLE, LIMITS, pricing_known=True, at=START)
    await ledger.begin_trial(SCOPE, at=START)
    snapshot = await ledger.begin_attempt(SCOPE, "attempt-1", at=START)
    assert snapshot.trial_requests == 1
    assert snapshot.trial_measured_model_spent_usd == Decimal("0.40")
    assert snapshot.deadline_at == START + timedelta(seconds=180)


@pytest.mark.parametrize(
    ("limits", "at"),
    [
        pytest.param(LIMITS.model_copy(update={"trial_request_limit": 99}), START, id="changed limits"),
        pytest.param(LIMITS, START + timedelta(minutes=5), id="changed start"),
    ],
)
async def test_a_cycle_reopened_under_different_terms_is_refused(tmp_path: Path, limits: EvaluationLimits, at: datetime) -> None:
    ledger = await _opened(tmp_path)
    with pytest.raises(BudgetConflictError):
        await ledger.begin_cycle(CYCLE, limits, pricing_known=True, at=at)


async def test_a_trial_reopened_from_a_different_start_is_refused(tmp_path: Path) -> None:
    ledger = await _opened(tmp_path)
    with pytest.raises(BudgetConflictError):
        await ledger.begin_trial(SCOPE, at=START + timedelta(seconds=30))


async def test_a_trial_that_was_never_begun_admits_nothing(tmp_path: Path) -> None:
    ledger = await _opened(tmp_path)
    with pytest.raises(BudgetNotOpenError):
        await ledger.admit(BudgetScope(cycle_id=CYCLE, trial_id="trial-unknown"), "req-1", "worker", at=START)


async def test_sixteen_requests_are_admitted_and_the_seventeenth_is_refused(tmp_path: Path) -> None:
    ledger = await _opened(tmp_path)
    for index in range(16):
        admission = await ledger.admit(SCOPE, f"req-{index}", "designer" if index % 2 else "worker", at=START)
        assert admission.trial_request_number == index + 1
        await ledger.settle(admission, Decimal("0.01"), at=START)
    with pytest.raises(BudgetRefusedError) as refused:
        await ledger.admit(SCOPE, "req-17", "worker", at=START)
    assert refused.value.refusal == "request_limit"


async def test_a_crossing_call_records_actual_spend_and_overshoot_then_stops_the_trial(tmp_path: Path) -> None:
    ledger = await _opened(tmp_path)
    first = await ledger.admit(SCOPE, "req-1", "worker", at=START)
    await ledger.settle(first, Decimal("1.90"), at=START)
    crossing = await ledger.admit(SCOPE, "req-2", "reviewer", at=START)
    snapshot = await ledger.settle(crossing, Decimal("0.25"), at=START)
    assert snapshot.trial_measured_model_spent_usd == Decimal("2.15")
    assert snapshot.trial_measured_model_overshoot_usd == Decimal("0.15")
    with pytest.raises(BudgetRefusedError) as refused:
        await ledger.admit(SCOPE, "req-3", "worker", at=START)
    assert refused.value.refusal == "trial_cost_stop"


async def test_the_deadline_refuses_the_next_request(tmp_path: Path) -> None:
    ledger = await _opened(tmp_path)
    with pytest.raises(BudgetRefusedError) as refused:
        await ledger.admit(SCOPE, "req-1", "worker", at=START + timedelta(seconds=180))
    assert refused.value.refusal == "deadline"


async def test_the_fourth_attempt_is_refused_and_a_repeat_is_not(tmp_path: Path) -> None:
    ledger = await _opened(tmp_path)
    for attempt in ("attempt-1", "attempt-2", "attempt-3", "attempt-2"):
        snapshot = await ledger.begin_attempt(SCOPE, attempt, at=START)
    assert snapshot.trial_attempts == 3
    with pytest.raises(BudgetRefusedError) as refused:
        await ledger.begin_attempt(SCOPE, "attempt-4", at=START)
    assert refused.value.refusal == "attempt_limit"


async def test_settlement_repeats_for_one_charge_and_refuses_a_second_one(tmp_path: Path) -> None:
    ledger = await _opened(tmp_path)
    admission = await ledger.admit(SCOPE, "req-1", "grader", at=START)
    first = await ledger.settle(admission, Decimal("0.30"), at=START)
    again = await ledger.settle(admission, Decimal("0.30"), at=START + timedelta(seconds=5))
    assert first == again
    with pytest.raises(BudgetConflictError):
        await ledger.settle(admission, Decimal("0.90"), at=START)


async def test_settling_a_request_nobody_admitted_is_refused(tmp_path: Path) -> None:
    ledger = await _opened(tmp_path)
    admission = await ledger.admit(SCOPE, "req-1", "worker", at=START)
    await ledger.settle(admission, Decimal("0.10"), at=START)
    unknown = admission.model_copy(update={"request_id": "req-never"})
    with pytest.raises(BudgetNotOpenError):
        await ledger.settle(unknown, Decimal("0.10"), at=START)


async def test_an_unresolved_charge_survives_a_new_record_object_and_blocks_admission(tmp_path: Path) -> None:
    ledger = await _opened(tmp_path)
    admission = await ledger.admit(SCOPE, "req-1", "worker", at=START)
    await ledger.settle(admission, None, at=START)
    restarted = FileEvaluationBudgetRecord(tmp_path)
    with pytest.raises(BudgetRefusedError) as refused:
        await restarted.admit(SCOPE, "req-2", "worker", at=START)
    assert refused.value.refusal == "unresolved_charge"
    state = await restarted.read(CYCLE)
    assert state is not None
    assert [record.charge_status for record in state.requests] == ["unresolved"]


async def test_a_record_opened_against_another_root_or_device_is_refused(tmp_path: Path) -> None:
    ledger = await _opened(tmp_path)
    state = await ledger.read(CYCLE)
    assert state is not None
    document = tmp_path / "evaluation-budgets" / f"{content_fingerprint(CYCLE)}.json"
    document.write_text(state.model_copy(update={"shared_root": "/mnt/another-worker", "device_id": 9_999}).model_dump_json(), encoding="utf-8")
    with pytest.raises(BudgetPreflightError) as refused:
        await ledger.admit(SCOPE, "req-1", "worker", at=START)
    assert refused.value.refusal == "unverified_state_root"


def test_the_record_names_the_root_and_device_it_holds_itself_to(tmp_path: Path) -> None:
    ledger = FileEvaluationBudgetRecord(tmp_path)
    identity = ledger.state_identity()
    assert identity.canonical_state_root == str(tmp_path.resolve())
    assert identity.device_id == (tmp_path / "evaluation-budgets").stat().st_dev
    state = asyncio.run(_opened_and_read(tmp_path))
    assert (state.shared_root, state.device_id) == (identity.canonical_state_root, identity.device_id)


async def test_a_damaged_record_reads_as_unreadable_rather_than_empty(tmp_path: Path) -> None:
    ledger = await _opened(tmp_path)
    document = tmp_path / "evaluation-budgets" / f"{content_fingerprint(CYCLE)}.json"
    document.write_text("{not a cycle state", encoding="utf-8")
    with pytest.raises(BudgetUnreadableError):
        await ledger.read(CYCLE)


async def test_an_in_flight_request_survives_a_worker_process_restart(tmp_path: Path) -> None:
    ledger = await _opened(tmp_path)
    await ledger.admit(SCOPE, "req-in-flight", "designer", at=START)
    assert _run_children(tmp_path, ("req-from-a-restarted-worker",)) == [("refused", "request_in_flight")]
    state = await FileEvaluationBudgetRecord(tmp_path).read(CYCLE)
    assert state is not None
    assert [record.admission.request_id for record in state.requests] == ["req-in-flight"]
    assert state.trials[0].deadline_at == START + timedelta(seconds=180)


async def test_only_one_of_four_processes_wins_the_in_flight_admission(tmp_path: Path) -> None:
    await _opened(tmp_path)
    answers = _run_children(tmp_path, ("req-a", "req-b", "req-c", "req-d"))
    admitted = [request_id for outcome, request_id in answers if outcome == "admitted"]
    refusals = [reason for outcome, reason in answers if outcome == "refused"]
    assert len(admitted) == 1
    assert refusals == ["request_in_flight"] * 3
    state = await FileEvaluationBudgetRecord(tmp_path).read(CYCLE)
    assert state is not None
    assert [record.admission.request_id for record in state.requests] == admitted


async def test_a_cycle_the_children_never_opened_is_absent_rather_than_empty(tmp_path: Path) -> None:
    assert await FileEvaluationBudgetRecord(tmp_path).read("cycle-nobody-opened") is None
    assert not (tmp_path / "evaluation-budgets").exists()


async def test_concurrent_admissions_serialise_inside_one_process(tmp_path: Path) -> None:
    ledger = await _opened(tmp_path)
    attempts = [ledger.admit(SCOPE, f"req-{index}", "worker", at=START) for index in range(4)]
    answers = await asyncio.gather(*attempts, return_exceptions=True)
    admitted = [answer for answer in answers if not isinstance(answer, BaseException)]
    refused = [answer for answer in answers if isinstance(answer, BudgetRefusedError)]
    assert len(admitted) == 1
    assert [error.refusal for error in refused] == ["request_in_flight"] * 3


def test_the_recorded_cycle_state_round_trips_through_its_own_document(tmp_path: Path) -> None:
    asyncio.run(_opened(tmp_path))
    document = (tmp_path / "evaluation-budgets" / f"{content_fingerprint(CYCLE)}.json").read_text(encoding="utf-8")
    state = BudgetCycleState.model_validate_json(document)
    assert state.shared_root == str(tmp_path.resolve())
    assert state.device_id == (tmp_path / "evaluation-budgets").stat().st_dev
    assert state.limits == LIMITS
