"""Four model roles, four processes, one record: what a measured trial actually persists.

A trial spreads a cycle over processes that share nothing but a directory. These rows start real
processes with the spawn method and let them spend through `BudgetedModel` over the file-backed
record, so the counters, the one in-flight request and the state a killed process leaves behind are
the record's own rather than one interpreter's memory. No provider is reached and no database is
needed.
"""

from __future__ import annotations

import asyncio
import multiprocessing
import os
import time

from datetime import UTC, datetime
from decimal import Decimal
from typing import TYPE_CHECKING

import pytest

from core.ai.evaluation.contracts import BudgetRefusedError, BudgetRole, BudgetScope, FileEvaluationBudgetRecord, cycle_snapshot

from harness.features.evals.domain.pilot import WORKFLOW_AUTOMATION_DEVELOPMENT_LIMITS

if TYPE_CHECKING:
    from multiprocessing.process import BaseProcess
    from pathlib import Path

    from core.ai.evaluation.contracts import BudgetCycleState

START = datetime(2026, 9, 16, 9, 0, tzinfo=UTC)
CYCLE = "cycle-wa-pilot"
TRIAL = "trial-1"
SCOPE = BudgetScope(cycle_id=CYCLE, trial_id=TRIAL)
LIMITS = WORKFLOW_AUTOMATION_DEVELOPMENT_LIMITS
CHARGE = Decimal("0.05")
CHILD_SECONDS = 60
KILLED = 3


def spend_in_child(
    root: str, role: BudgetRole, results: multiprocessing.Queue, *, hold_seconds: float = 0.0, die_before_settling: bool = False
) -> None:
    """Make one budgeted request from a process of its own and report how it ended.

    `hold_seconds` keeps the request in flight while another process tries to be admitted.
    `die_before_settling` leaves the process the way a kill does: the admission is on disk and no
    settlement ever follows it.
    """
    import asyncio

    from pathlib import Path

    from core.ai.evaluation.contracts import BudgetRefusedError, BudgetScope, FileEvaluationBudgetRecord
    from pydantic_ai.messages import ModelResponse, TextPart
    from pydantic_ai.models import ModelRequestParameters
    from pydantic_ai.models.function import FunctionModel
    from pydantic_ai.usage import RequestUsage

    from harness.features.evals.infrastructure.budgeted_model import BudgetedModel

    def answer(messages, info) -> ModelResponse:
        del messages, info
        if hold_seconds:
            time.sleep(hold_seconds)
        if die_before_settling:
            os._exit(KILLED)
        return ModelResponse(parts=[TextPart(content="done")], usage=RequestUsage(input_tokens=10, output_tokens=5))

    def charge(response: ModelResponse) -> Decimal | None:
        del response
        return CHARGE

    ledger = FileEvaluationBudgetRecord(Path(root))
    model = BudgetedModel(
        FunctionModel(function=answer),
        record=ledger,
        scope=BudgetScope(cycle_id=CYCLE, trial_id=TRIAL),
        role=role,
        charge=charge,
        clock=lambda: START,
    )
    try:
        asyncio.run(model.request([], None, ModelRequestParameters()))
    except BudgetRefusedError as refused:
        results.put(("refused", refused.refusal))
    else:
        results.put(("settled", role))


def _started(root: Path, role: BudgetRole, **script: object) -> tuple[BaseProcess, multiprocessing.Queue]:
    context = multiprocessing.get_context("spawn")
    results: multiprocessing.Queue = context.Queue()
    worker = context.Process(target=spend_in_child, args=(str(root), role, results), kwargs=script)
    worker.start()
    return worker, results


async def _opened(root: Path) -> FileEvaluationBudgetRecord:
    ledger = FileEvaluationBudgetRecord(root)
    await ledger.begin_cycle(CYCLE, LIMITS, pricing_known=True, at=START)
    await ledger.begin_trial(SCOPE, at=START)
    return ledger


async def _state(root: Path) -> BudgetCycleState:
    state = await FileEvaluationBudgetRecord(root).read(CYCLE)
    assert state is not None, "the cycle was opened, so its record is on disk"
    return state


async def test_four_roles_in_four_processes_settle_into_one_cycle_and_trial(tmp_path: Path) -> None:
    await _opened(tmp_path)
    roles: tuple[BudgetRole, ...] = ("designer", "worker", "reviewer", "grader")
    for role in roles:
        worker, results = _started(tmp_path, role)
        worker.join(CHILD_SECONDS)
        assert worker.exitcode == 0
        assert results.get(timeout=CHILD_SECONDS) == ("settled", role)
    state = await _state(tmp_path)
    assert [record.admission.role for record in state.requests] == ["designer", "worker", "reviewer", "grader"]
    snapshot = cycle_snapshot(state, SCOPE)
    assert snapshot.trial_requests == 4
    assert snapshot.trial_measured_model_spent_usd == CHARGE * 4
    assert snapshot.in_flight_request_id is None


async def test_only_one_process_holds_the_cycles_one_in_flight_request(tmp_path: Path) -> None:
    await _opened(tmp_path)
    holder, held = _started(tmp_path, "designer", hold_seconds=1.5)
    await asyncio.sleep(0.5)
    other, blocked = _started(tmp_path, "worker")
    for worker in (holder, other):
        worker.join(CHILD_SECONDS)
        assert worker.exitcode == 0
    assert blocked.get(timeout=CHILD_SECONDS) == ("refused", "request_in_flight")
    assert held.get(timeout=CHILD_SECONDS) == ("settled", "designer")
    state = await _state(tmp_path)
    assert [record.admission.role for record in state.requests] == ["designer"]


async def test_a_process_killed_before_settling_leaves_its_request_in_flight(tmp_path: Path) -> None:
    ledger = await _opened(tmp_path)
    killed, _ = _started(tmp_path, "worker", die_before_settling=True)
    killed.join(CHILD_SECONDS)
    assert killed.exitcode == KILLED, "the child died where the script said it would"
    state = await _state(tmp_path)
    assert [record.charge_status for record in state.requests] == ["pending"]
    assert cycle_snapshot(state, SCOPE).in_flight_request_id == state.requests[0].admission.request_id
    with pytest.raises(BudgetRefusedError) as refused:
        await ledger.admit(SCOPE, "req-after-the-kill", "designer", at=START)
    assert refused.value.refusal == "request_in_flight"
