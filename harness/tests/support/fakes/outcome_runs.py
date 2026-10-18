"""The pilot trials a test filed, and the archived runs they name.

`FakePilotRuns` is the index a workflow-automation runner reads: which run one case's journey
produced. `an_archived_run` and `an_outcome` build the record that journey would have left, so a
test states the facts it is grading and nothing else.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Literal

from core.ai.workflows.contracts import Outcome
from core.ai.workflows.domain.outcome import OutcomeCheck

from harness.features.running.domain.ended_run import RunArchiveRecord

if TYPE_CHECKING:
    from core.ai.workflows.contracts import OutcomeStatus

    from harness.features.evals.domain.pilot import PilotTrial

_STARTED = "2026-09-16T09:00:00+00:00"
_ENDED = "2026-09-16T09:04:00+00:00"


class FakePilotRuns:
    """Holds the trials a test filed, keyed by case id and repetition."""

    def __init__(self, trials: list[PilotTrial] | None = None) -> None:
        self.trials: dict[tuple[str, int], PilotTrial] = {(trial.case_id, trial.repetition): trial for trial in (trials or [])}

    async def trial_for(self, case_id: str, repetition: int) -> PilotTrial | None:
        """The trial filed under this case and repetition, or `None` when none is."""
        return self.trials.get((case_id, repetition))


def an_outcome(
    status: OutcomeStatus,
    *,
    summary: str = "what the run did",
    attempted: int = 1,
    succeeded: int = 1,
    skipped: int = 0,
    failed: int = 0,
    checks: tuple[OutcomeCheck, ...] = (),
) -> Outcome:
    """One checked outcome, with whatever counts and gate checks the row under test needs."""
    return Outcome(
        status=status, summary=summary, attempted=attempted, succeeded=succeeded, skipped=skipped, failed=failed, checks=checks
    )


def a_gate_check(check_id: str = "review", *, passed: bool | None = True, required: bool = True) -> OutcomeCheck:
    """One required gate's check, decided, refused or never put at all."""
    return OutcomeCheck(
        id=check_id,
        required=required,
        passed=passed,
        expected_sources=("person",),
        observed_source="person" if passed is not None else "unknown",
        detail="a person decided" if passed is not None else "no decision reached this gate",
    )


def an_archived_run(
    run_id: str = "run-1",
    *,
    result: Outcome | None = None,
    outcome: Literal["completed", "failed", "stopped"] = "completed",
    declares_result: bool = True,
    workflow_id: str = "wf.workflow-automation-pilot",
) -> RunArchiveRecord:
    """One ended run as the archive holds it, carrying the facts a grader reads and nothing more."""
    spec: dict[str, object] = {"name": workflow_id, "version": 1}
    if declares_result:
        spec["result"] = {"summary": "the page the run wrote", "primary_work": "write"}
    return RunArchiveRecord(
        run_id=run_id,
        workflow_id=workflow_id,
        outcome=outcome,
        result=result,
        started_at=_STARTED,
        ended_at=_ENDED,
        runner="app:harness",
        session_id="sess-pilot",
        params={},
        spec=spec,
        values={},
        events=[],
    )
