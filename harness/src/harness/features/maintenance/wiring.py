"""What the maintenance slice registers: the watches that read each run's ending, and the case runs its steps go through."""

from __future__ import annotations

from typing import TYPE_CHECKING

from core.ai.workflows.contracts import FileEvidenceLog
from wireup import injectable

from harness.adapters.contracts import Notifier
from harness.features.evals.contracts import CaseRunRefusedError, check_step_cases, perform_step_cases
from harness.features.maintenance.service.failed_fires import FailedFireNotice
from harness.features.maintenance.service.live_trial import LiveTrialWatch
from harness.features.maintenance.service.run_watch import MaintenanceRunWatch
from harness.features.running.contracts import SCHEDULE_RUNNER_PREFIX
from harness.session import PooledSessions
from harness.settings import StateSettings

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable, Mapping

    from core.ai.workflows.contracts import InlineStep, StepRegistry
    from pydantic import JsonValue

    from harness.adapters.contracts import StandInCalls
    from harness.domain.contracts import CaseReport
    from harness.features.evals.contracts import CaseReports


@injectable
def live_trial_watch(sessions: PooledSessions, state: StateSettings, notifier: Notifier) -> LiveTrialWatch:
    """The live trial watch, reading the evidence log under this deployment's state home through a reader of its own.

    Args:
        sessions: The sessions this process holds.
        state: Where this deployment keeps its state, the evidence log included.
        notifier: How the owner is told about a revert.

    Returns:
        The live trial watch.
    """
    return LiveTrialWatch(sessions, FileEvidenceLog(state.evidence_log), notifier)


@injectable
def maintenance_run_watch(sessions: PooledSessions) -> MaintenanceRunWatch:
    """The maintenance run watch.

    Args:
        sessions: The sessions this process holds.

    Returns:
        The maintenance run watch.
    """
    return MaintenanceRunWatch(sessions)


@injectable
def failed_fire_notice(notifier: Notifier) -> FailedFireNotice:
    """The failed-fire notice, over the runner prefix of every run a schedule fires.

    Args:
        notifier: How the owner is told.

    Returns:
        The failed-fire notice.
    """
    return FailedFireNotice(SCHEDULE_RUNNER_PREFIX, notifier)


REGISTRATIONS: list[object] = [live_trial_watch, maintenance_run_watch, failed_fire_notice]
"""Everything this slice puts in the container, each under its own type."""


class StepCaseRuns:
    """The case runs step maintenance goes through: a step's case file checked, then its cases performed over stand-ins.

    Each performance builds a registry of its own over the stand-in list it is handed, so two steps' case runs never
    count each other's calls.
    """

    def __init__(self, stand_in_registry: Callable[[StandInCalls], Awaitable[StepRegistry]], reports: CaseReports) -> None:
        """Hold how a stand-in registry is built and where case reports are kept.

        Args:
            stand_in_registry: Builds a registry whose write tools are stand-ins recording into the list it is handed.
            reports: Where case reports are kept, so a step version runs its cases once.
        """
        self._stand_in_registry = stand_in_registry
        self._reports = reports

    def refusal(self, step_id: str, cases: Mapping[str, JsonValue]) -> str | None:
        """Why the case file cannot run, from the case run's own check, or None when it can.

        Args:
            step_id: The step version's id, which the refusal names.
            cases: The step's dataset in `pydantic_evals` form.

        Returns:
            The refusal, or None.
        """
        try:
            check_step_cases(step_id, cases)
        except CaseRunRefusedError as exc:
            return str(exc)
        return None

    async def perform(self, step: InlineStep, step_id: str, cases: Mapping[str, JsonValue] | None, calls: StandInCalls) -> CaseReport:
        """The case report of one step version, its cases performed through a stand-in registry built over `calls`.

        Args:
            step: The step version whose cases run.
            step_id: The step version's id, which the report names.
            cases: The step's dataset in `pydantic_evals` form, or None when it has none.
            calls: The list this step's stand-ins record into.

        Returns:
            The case report.

        Raises:
            CaseRunRefusedError: The stand-in registry cannot perform the step. Nothing has run.
        """
        registry = await self._stand_in_registry(calls)
        return await perform_step_cases(step, step_id, cases, registry, self._reports, calls)
