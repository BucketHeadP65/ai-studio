"""The failed-fire notice: the owner hears about each scheduled run that ended failed."""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from core.ai.workflows.contracts import RunEvidence

    from harness.adapters.contracts import Notifier


class FailedFireNotice:
    """Tells the owner once about each run a schedule fired that ended failed, by its outcome or its checked status.

    A completed run whose checked status is failed tells the owner too, the way a live trial reads a failed run. A run
    whose runner does not start with the schedule runner prefix, such as a run started by hand, tells nobody.
    """

    def __init__(self, schedule_runner_prefix: str, notifier: Notifier) -> None:
        """Hold the prefix a scheduled run's runner starts with, and the way to the owner.

        Args:
            schedule_runner_prefix: What the runner of every run a schedule fires starts with, followed by the schedule's id.
            notifier: How the owner is told.

        Raises:
            ValueError: If `schedule_runner_prefix` is empty, which every runner starts with.
        """
        if not schedule_runner_prefix:
            msg = "the schedule runner prefix is empty, and every run's runner starts with it. Pass the prefix a schedule's runs carry"
            raise ValueError(msg)
        self._prefix = schedule_runner_prefix
        self._notifier = notifier

    async def run_ended(self, evidence: RunEvidence) -> None:
        """Tell the owner when the run was a schedule's fire and ended failed.

        Args:
            evidence: What the run's ending recorded.
        """
        if not evidence.runner.startswith(self._prefix):
            return
        if evidence.outcome != "failed" and evidence.result.status != "failed":
            return
        schedule = evidence.runner.removeprefix(self._prefix)
        message = f"{evidence.workflow_id}, fired by schedule {schedule}, ended failed. {evidence.result.summary}"
        await self._notifier.notify("A scheduled run failed", message)
