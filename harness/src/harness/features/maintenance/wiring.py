"""What the maintenance slice registers: the watches that read each run's ending after its evidence is written."""

from __future__ import annotations

from core.ai.workflows.contracts import FileEvidenceLog
from wireup import injectable

from harness.adapters.contracts import Notifier
from harness.features.maintenance.service.failed_fires import FailedFireNotice
from harness.features.maintenance.service.live_trial import LiveTrialWatch
from harness.features.maintenance.service.run_watch import MaintenanceRunWatch
from harness.features.running.contracts import SCHEDULE_RUNNER_PREFIX
from harness.session import PooledSessions
from harness.settings import StateSettings


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
