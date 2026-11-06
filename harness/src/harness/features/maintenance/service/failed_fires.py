"""The failed-fire notice: the owner hears about each scheduled run that ended failed, and what became of its changed steps."""

from __future__ import annotations

from typing import TYPE_CHECKING

from core.observability import get_logger

from harness.features.maintenance.domain import CHANGED_STEPS

if TYPE_CHECKING:
    from core.ai.workflows.contracts import RunEvidence

    from harness.adapters.contracts import Notifier
    from harness.domain.contracts import FailedFireSteps, NotPutBack
    from harness.features.maintenance.internal_contracts import StepSchedules

_logger = get_logger(__name__)

_NOT_PUT_BACK: dict[NotPutBack, str] = {
    "other_failure": "it did not stop with a provider error",
    "put_back_once": "a failed fire put them back once already",
    "schedule_paused": "the schedule is paused",
}
"""Why a failed fire's changed steps were not put back, in the words the notice says it."""


class FailedFireNotice:
    """Tells the owner once about each run a schedule fired that ended failed, by its outcome or its checked status.

    A completed run whose checked status is failed tells the owner too. A run whose runner does not start with the
    schedule runner prefix, such as a run started by hand, tells nobody and changes no schedule. Before it tells the
    owner, it has the schedules put back the changed steps of an on-step-change fire that stopped with a provider
    error, and the notice names the steps put back and those that need a run by hand.
    """

    def __init__(self, schedule_runner_prefix: str, notifier: Notifier, schedules: StepSchedules) -> None:
        """Hold the prefix a scheduled run's runner starts with, the way to the owner and the schedules.

        Args:
            schedule_runner_prefix: What the runner of every run a schedule fires starts with, followed by the schedule's id.
            notifier: How the owner is told.
            schedules: The on-step-change schedules, which take back a failed fire's changed steps.

        Raises:
            ValueError: If `schedule_runner_prefix` is empty, which every runner starts with.
        """
        if not schedule_runner_prefix:
            msg = "the schedule runner prefix is empty, and every run's runner starts with it. Pass the prefix a schedule's runs carry"
            raise ValueError(msg)
        self._prefix = schedule_runner_prefix
        self._notifier = notifier
        self._schedules = schedules

    async def run_ended(self, evidence: RunEvidence) -> None:
        """Tell the owner when the run was a schedule's fire and ended failed, with what became of its changed steps.

        A put back that raises is said in the notice, and the owner is still told.

        Args:
            evidence: What the run's ending recorded.
        """
        if not evidence.runner.startswith(self._prefix):
            return
        if evidence.outcome != "failed" and evidence.result.status != "failed":
            return
        schedule = evidence.runner.removeprefix(self._prefix)
        message = f"{evidence.workflow_id}, fired by schedule {schedule}, ended failed. {evidence.result.summary}"
        try:
            steps = await self._schedules.put_back_steps(evidence)
        except Exception as exc:  # a failed put back is said in the notice and never keeps the owner from hearing of the failure
            _logger.exception("putting back the changed steps of {workflow} failed", workflow=evidence.workflow_id)
            message += (
                f"\nPutting its changed steps back failed: {exc}. Run {evidence.workflow_id} by hand with the "
                f"{CHANGED_STEPS} it was fired with: {evidence.params.get(CHANGED_STEPS)}."
            )
        else:
            if steps is not None:
                message += _steps_text(evidence.workflow_id, steps)
        await self._notifier.notify("A scheduled run failed", message)


def _steps_text(workflow_id: str, steps: FailedFireSteps) -> str:
    """The lines naming the changed steps put back and those to run by hand, each on its own line."""
    text = ""
    if steps.put_back:
        text += f"\nIt stopped with a provider error, so {', '.join(steps.put_back)} went back on the schedule, which fires again after its quiet period."
    if steps.by_hand:
        because = "" if steps.why is None else f", because {_NOT_PUT_BACK[steps.why]}"
        text += (
            f"\nNot put back{because}: {', '.join(steps.by_hand)}. "
            f"Run {workflow_id} by hand with {CHANGED_STEPS} {','.join(steps.by_hand)} to plan them."
        )
    return text
