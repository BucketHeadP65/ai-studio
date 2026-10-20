"""The evidence vocabulary widens: a person can end a run, and automode can wave a gate through.

`stopped` and `failed` both confirm nothing -- a person ending a run deliberately is no more
evidence of success than a crash is. `gates_auto` records what automode released without a
human click; `trust_level` never reads it, because it is not an approval.
"""

from __future__ import annotations

import pytest

from pydantic import ValidationError

from core.ai.workflows.contracts import Outcome, RunEvidence, WorkflowState, trust_level


def test_a_stopped_run_validates() -> None:
    run = RunEvidence(workflow_id="wf.report", spec_hash="abc", outcome="stopped", result=Outcome(status="unknown", summary="nothing was checked"), gates_auto=(), gates_orchestrated=())
    assert run.outcome == "stopped"


def test_a_stopped_run_counts_like_a_failure_and_confirms_nothing() -> None:
    record = trust_level([RunEvidence(workflow_id="wf.report", spec_hash="abc", outcome="stopped", result=Outcome(status="unknown", summary="nothing was checked"), gates_auto=(), gates_orchestrated=())])
    assert record.level == "unverified"
    assert record.failed_runs == 1
    assert record.runs_with_evidence == 0


def test_gates_auto_never_reads_as_an_approval() -> None:
    run = RunEvidence(workflow_id="wf.report", spec_hash="abc", outcome="completed", gates_auto=("review",), result=Outcome(status="unknown", summary="nothing was checked"), gates_orchestrated=())
    assert run.gates_auto == ("review",)
    assert run.gates_approved == ()


@pytest.mark.parametrize("missing", ["result", "gates_auto", "gates_orchestrated"])
def test_a_run_record_missing_a_field_every_writer_writes_is_refused(missing: str) -> None:
    record = {
        "kind": "run",
        "workflow_id": "wf.report",
        "spec_hash": "abc",
        "outcome": "completed",
        "result": {"status": "unknown", "summary": "nothing was checked"},
        "gates_auto": [],
        "gates_orchestrated": [],
    }
    del record[missing]

    with pytest.raises(ValidationError):
        RunEvidence.model_validate(record)


def test_a_run_record_whose_result_is_null_is_refused() -> None:
    with pytest.raises(ValidationError):
        RunEvidence.model_validate(
            {"kind": "run", "workflow_id": "wf.report", "spec_hash": "abc", "outcome": "completed", "result": None, "gates_auto": [], "gates_orchestrated": []}
        )


def test_an_execution_outcome_is_exactly_the_three_words_an_observer_can_see() -> None:
    """What mechanically happened, never whether the work was any good."""
    for outcome in ("completed", "failed", "stopped"):
        assert RunEvidence(workflow_id="wf.report", spec_hash="abc", outcome=outcome, result=Outcome(status="unknown", summary="nothing was checked"), gates_auto=(), gates_orchestrated=()).outcome == outcome  # type: ignore[arg-type]
    with pytest.raises(ValidationError):
        RunEvidence(workflow_id="wf.report", spec_hash="abc", outcome="succeeded", result=Outcome(status="unknown", summary="nothing was checked"), gates_auto=(), gates_orchestrated=())  # type: ignore[arg-type]


def test_a_maps_skip_list_is_a_reason_per_failure_and_not_a_disposition_per_item() -> None:
    """Run state's own skip record cannot say which item was left out, only how many and why."""
    state = WorkflowState(params={})
    state.skipped.setdefault("each", []).append("the fetch timed out")

    assert state.skipped == {"each": ["the fetch timed out"]}
    assert not state.work_facts, "the disposition of one item is its own fact, filed elsewhere"


def test_only_a_completed_run_with_a_checked_result_confirms_anything() -> None:
    confirmed = RunEvidence(
        workflow_id="wf.report", spec_hash="abc", outcome="completed", result=Outcome(status="succeeded", summary="every item landed"), gates_auto=(), gates_orchestrated=()
    )
    record = trust_level([confirmed])

    assert record.level == "machine-confirmed"
    assert record.runs_with_evidence == 1


def test_an_explicit_no_op_is_a_checked_result_and_confirms_the_workflow_runs() -> None:
    nothing_to_do = RunEvidence(
        workflow_id="wf.report", spec_hash="abc", outcome="completed", result=Outcome(status="noop", summary="nothing was waiting"), gates_auto=(), gates_orchestrated=()
    )

    assert trust_level([nothing_to_do]).level == "machine-confirmed"


@pytest.mark.parametrize("status", ["unknown", "partial", "failed"])
def test_a_completed_run_whose_result_is_not_confirmed_is_counted_apart(status: str) -> None:
    ran = RunEvidence(
        workflow_id="wf.report", spec_hash="abc", outcome="completed", result=Outcome.model_validate({"status": status, "summary": "not confirmed"}), gates_auto=(), gates_orchestrated=()
    )
    record = trust_level([ran])

    assert record.level == "unverified"
    assert record.runs_with_evidence == 0
    assert record.unconfirmed_runs == 1
    assert record.failed_runs == 0, "a completed run is not a failure"


def test_an_approval_still_carries_the_workflow_whatever_the_runs_say() -> None:
    from core.ai.workflows.contracts import Approval

    events = [RunEvidence(workflow_id="wf.report", spec_hash="abc", outcome="completed", result=Outcome(status="unknown", summary="nothing was checked"), gates_auto=(), gates_orchestrated=()), Approval(workflow_id="wf.report", spec_hash="abc", by="human:owner")]

    assert trust_level(events).level == "approved"
