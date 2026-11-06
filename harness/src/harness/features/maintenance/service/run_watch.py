"""The maintenance run watch: a step maintenance run that ended at its plan judge's last rejection raises a flag per changed step."""

from __future__ import annotations

from datetime import UTC
from typing import TYPE_CHECKING

from core.ai.workflows.contracts import (
    STEP_MAINTENANCE_AGENT,
    JudgeGate,
    LibraryError,
    MaintenanceFlag,
    MaintenanceFlags,
    ReusableStepQueries,
    StepVersionQueries,
    WorkflowUnknownError,
)
from core.observability import get_logger
from pydantic import ValidationError

from harness.features.maintenance.domain import CHANGED_STEPS, MAINTENANCE_WORKFLOW, PLAN_GATE, changed_ids
from harness.features.maintenance.service.changed_steps import newest_version

if TYPE_CHECKING:
    from core.ai.workflows.contracts import RunEvidence

    from harness.session import PooledSessions

_logger = get_logger(__name__)


class MaintenanceRunWatch:
    """Raises one `plan_rejected` maintenance flag per changed step when a step maintenance run ends failed at its plan judge.

    The run must have been rejected at `PLAN_GATE` as many times as the gate's `max_attempts`, which is how a run ends with
    nothing landed. Each flag is about the newest saved version of the step a changed id names, the version the run
    planned, and a step named twice is flagged once. Every other run, of this workflow or another, writes nothing. A changed step the library cannot read
    or whose stored document no longer validates, or a flag it refuses, is logged and the other steps' flags are still
    written.
    """

    def __init__(self, sessions: PooledSessions) -> None:
        """Hold where the library is found.

        Args:
            sessions: The sessions this process holds, whose own session's library is read when a run ends.
        """
        self._sessions = sessions

    async def run_ended(self, evidence: RunEvidence) -> None:
        """Raise the flags when the run is a step maintenance run its plan judge rejected to the end.

        Args:
            evidence: What the run's ending recorded.
        """
        rejections = evidence.gates_rejected.get(PLAN_GATE, 0)
        if evidence.outcome != "failed" or not rejections:
            return
        library = (await self._sessions.pool()).default.library
        if not isinstance(library, MaintenanceFlags) or not isinstance(library, ReusableStepQueries) or not isinstance(library, StepVersionQueries):
            return
        try:
            workflow = await library.fetch_workflow(evidence.workflow_id)
        except WorkflowUnknownError:
            return
        gate = next((node for node in workflow.nodes if isinstance(node, JudgeGate) and node.id == PLAN_GATE), None)
        if workflow.name != MAINTENANCE_WORKFLOW or gate is None or rejections < gate.max_attempts:
            return
        changed = evidence.params.get(CHANGED_STEPS)
        step_ids = changed_ids(changed) if isinstance(changed, str) else ()
        ended = evidence.at if evidence.at.tzinfo is not None else evidence.at.replace(tzinfo=UTC)
        flagged: set[str] = set()
        for step_id in step_ids:
            try:
                planned, _ = await newest_version(step_id, fetch_step=library.fetch_step, step_versions=library.step_versions)
                if planned.name in flagged:
                    continue
                flagged.add(planned.name)
                await library.put_flag(
                    MaintenanceFlag(
                        kind="plan_rejected",
                        step=planned.name,
                        step_id=planned.step_id,
                        step_hash=planned.step_hash,
                        title=f"Plan for {planned.name} rejected",
                        summary=(
                            f"Decide how {planned.step_id} reaches the workflows that hold {planned.name}: the step maintenance judge "
                            f"rejected the plan {rejections} times and nothing landed."
                        ),
                        evidence=(
                            f"The step maintenance run of {evidence.workflow_id} that ended at {ended.isoformat()} stopped at "
                            f"{PLAN_GATE}'s rejection {rejections} of {gate.max_attempts}, with nothing landed. It was checking "
                            f"{', '.join(step_ids)}."
                        ),
                        raised_by=STEP_MAINTENANCE_AGENT,
                        raised_at=ended,
                    )
                )
            except (LibraryError, ValidationError) as exc:
                _logger.warning("the plan_rejected flag for {step} was not written: {reason}", step=step_id, reason=str(exc))
