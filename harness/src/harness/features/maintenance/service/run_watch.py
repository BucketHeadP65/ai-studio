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
    WorkflowUnknownError,
    step_content_address,
)
from core.observability import get_logger
from pydantic import ValidationError

from harness.features.maintenance.domain import CHANGED_STEPS, MAINTENANCE_WORKFLOW, PLAN_GATE

if TYPE_CHECKING:
    from core.ai.workflows.contracts import RunEvidence

    from harness.session import PooledSessions

_logger = get_logger(__name__)


class MaintenanceRunWatch:
    """Raises one `plan_rejected` maintenance flag per changed step when a step maintenance run ends failed at its plan judge.

    The run must have been rejected at `PLAN_GATE` as many times as the gate's `max_attempts`, which is how a run ends with
    nothing landed. Every other run, of this workflow or another, writes nothing. A changed step the library cannot read
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
        if not isinstance(library, MaintenanceFlags) or not isinstance(library, ReusableStepQueries):
            return
        try:
            workflow = await library.fetch_workflow(evidence.workflow_id)
        except WorkflowUnknownError:
            return
        gate = next((node for node in workflow.nodes if isinstance(node, JudgeGate) and node.id == PLAN_GATE), None)
        if workflow.name != MAINTENANCE_WORKFLOW or gate is None or rejections < gate.max_attempts:
            return
        changed = evidence.params.get(CHANGED_STEPS)
        step_ids = [part.strip() for part in changed.split(",") if part.strip()] if isinstance(changed, str) else []
        ended = evidence.at if evidence.at.tzinfo is not None else evidence.at.replace(tzinfo=UTC)
        for step_id in step_ids:
            try:
                step = await library.fetch_step(step_id)
                await library.put_flag(
                    MaintenanceFlag(
                        kind="plan_rejected",
                        step=step.name,
                        step_id=step_id,
                        step_hash=step_content_address(step),
                        title=f"Plan for {step.name} rejected",
                        summary=(
                            f"Decide how {step_id} reaches the workflows that hold {step.name}: the step maintenance judge "
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
