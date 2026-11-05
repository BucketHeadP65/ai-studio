"""The live trial watch: each run of a consumer version on live trial is read when it ends, and a failed or worse one reverts it."""

from __future__ import annotations

from datetime import UTC
from typing import TYPE_CHECKING

from core.ai.workflows.contracts import (
    STEP_MAINTENANCE_AGENT,
    ConsumerRecords,
    Grade,
    LibraryError,
    LiveTrialLibrary,
    MaintenanceFlag,
    MaintenanceFlags,
    RunEvidence,
    StepVersionQueries,
    WorkflowUnknownError,
    live_trial_verdict,
    spec_identity,
)
from core.observability import get_logger

if TYPE_CHECKING:
    from collections.abc import Sequence
    from datetime import datetime

    from core.ai.workflows.contracts import Approval, LiveTrialMarker, LiveTrialVerdict, Workflow, WorkflowLibrary

    from harness.adapters.contracts import Notifier
    from harness.features.maintenance.internal_contracts import RunHistoryQueries
    from harness.session import PooledSessions

_logger = get_logger(__name__)

BASELINE_RUNS = 3
"""How many of the previous version's newest completed runs a live trial run is compared with."""


class LiveTrialWatch:
    """Reads each ended run of a consumer version on live trial, and reverts the version after its first failed or worse run.

    Each run is read once, by `live_trial_verdict`, against the previous version's newest completed runs, with the grades
    on record when the watch reads it: a grade written later never changes a verdict already acted on. Once as many
    earlier runs counted as the marker names, the live trial is over and nothing is read again. A revert makes the
    previous version live again, records its decline of the step version as reverted, raises a `live_trial_reverted`
    maintenance flag and tells the owner. A version that is no longer the newest live one of its name is never reverted.
    A library that keeps no live trials is never read past its type.
    """

    def __init__(self, sessions: PooledSessions, history: RunHistoryQueries, notifier: Notifier) -> None:
        """Hold where the library is found, the evidence log read and the way to the owner.

        Args:
            sessions: The sessions this process holds, whose own session's library is read when a run ends.
            history: The evidence log, read by content address.
            notifier: How the owner is told about a revert.
        """
        self._sessions = sessions
        self._history = history
        self._notifier = notifier

    async def run_ended(self, evidence: RunEvidence) -> None:
        """Read the run when its version is on live trial, and revert the version when the run failed or did worse.

        Args:
            evidence: What the run's ending recorded.
        """
        library = (await self._sessions.pool()).default.library
        if not isinstance(library, LiveTrialLibrary):
            return
        try:
            marker = await library.fetch_live_trial(evidence.workflow_id)
        except WorkflowUnknownError:
            return
        if marker is None:
            return
        workflow = await library.fetch_workflow(evidence.workflow_id)
        live_trial_id = await library.locate(workflow.name, workflow.version)
        if live_trial_id is None or spec_identity(workflow) != evidence.spec_hash:
            return
        verdict = await self._verdict(library, live_trial_id, workflow, marker, evidence)
        if verdict is None or verdict.kind not in ("failed", "worse"):
            return
        try:
            restored = await library.revert(live_trial_id)
        except LibraryError as exc:
            _logger.warning(
                "the live trial of {workflow} was not reverted: {reason}. When the previous version was already written back it may be "
                "live again beside {workflow}, and the next failed or worse run of {workflow} asks for the revert again",
                workflow=live_trial_id,
                reason=str(exc),
            )
            return
        _logger.info("reverted {workflow} to {restored} after a {kind} run", workflow=live_trial_id, restored=restored, kind=verdict.kind)
        await self._decline(library, restored, marker)
        await self._raise_flag(library, live_trial_id=live_trial_id, workflow=workflow, restored=restored, marker=marker, verdict=verdict, ended_at=evidence.at)
        message = f"{live_trial_id} was reverted after a {verdict.kind} run on live trial, and {restored} is live again. {verdict.reason}"
        await self._notifier.notify(f"Live trial of {marker.step} reverted", message)

    async def _verdict(
        self, library: WorkflowLibrary, live_trial_id: str, workflow: Workflow, marker: LiveTrialMarker, evidence: RunEvidence
    ) -> LiveTrialVerdict | None:
        """The verdict on `evidence`, or None when as many earlier runs counted as the marker names."""
        lineage = await library.fetch_lineage(live_trial_id)
        previous = await library.fetch_workflow(marker.previous)
        prior = await self._history.history_for(spec_hash=spec_identity(previous))
        baseline = sorted((event for event in prior if isinstance(event, RunEvidence) and event.outcome == "completed"), key=lambda run: run.at)
        baseline = baseline[-BASELINE_RUNS:]
        baseline_grades = _grades_about(prior, baseline)
        own = await self._history.history_for(spec_hash=evidence.spec_hash)
        touched = () if lineage is None else lineage.touched

        def verdict_on(run: RunEvidence) -> LiveTrialVerdict:
            return live_trial_verdict(
                workflow, touched, run, run_grades=_grades_about(own, (run,)), baseline=baseline, baseline_grades=baseline_grades, previous=previous
            )

        earlier = [event for event in own if isinstance(event, RunEvidence) and event.at < evidence.at]
        if sum(verdict_on(run).kind == "counts" for run in earlier) >= marker.runs:
            return None
        return verdict_on(evidence)

    async def _decline(self, library: WorkflowLibrary, restored: str, marker: LiveTrialMarker) -> None:
        """Record the restored version's decline of the step version as reverted, logging a failure."""
        if not isinstance(library, ConsumerRecords):
            return
        try:
            await library.record_decline(restored, step=marker.step, step_hash=marker.step_hash, because="reverted")
        except Exception:
            _logger.exception("{workflow} is live again, and its decline of {step} was not recorded", workflow=restored, step=marker.step)

    async def _raise_flag(  # noqa: PLR0913 (each keyword is one fact the flag's page states)
        self,
        library: WorkflowLibrary,
        *,
        live_trial_id: str,
        workflow: Workflow,
        restored: str,
        marker: LiveTrialMarker,
        verdict: LiveTrialVerdict,
        ended_at: datetime,
    ) -> None:
        """Write the `live_trial_reverted` flag about the reverted version, logging a failure."""
        if not isinstance(library, MaintenanceFlags) or not isinstance(library, StepVersionQueries):
            return
        try:
            step_id = next((saved.step_id for saved in await library.step_versions(marker.step) if saved.step_hash == marker.step_hash), None)
            if step_id is None:
                _logger.warning(
                    "{workflow} was reverted, and no saved version of {step} has the content address its marker names, so no flag was raised",
                    workflow=live_trial_id,
                    step=marker.step,
                )
                return
            ended = ended_at if ended_at.tzinfo is not None else ended_at.replace(tzinfo=UTC)
            await library.put_flag(
                MaintenanceFlag(
                    kind="live_trial_reverted",
                    step=marker.step,
                    step_id=step_id,
                    step_hash=marker.step_hash,
                    workflow=live_trial_id,
                    title=f"Live trial of {marker.step} reverted in {workflow.name}",
                    summary=f"Decide whether {workflow.name} adopts {step_id} again once it is fixed. {restored} is live and declines it.",
                    evidence=(
                        f"A run of {live_trial_id} that ended at {ended.isoformat()} was {verdict.kind}. {verdict.reason}\n\n"
                        f"{live_trial_id} was archived, and {restored}, the version it replaced, is live again."
                    ),
                    raised_by=STEP_MAINTENANCE_AGENT,
                    raised_at=ended,
                )
            )
        except Exception:
            _logger.exception("{workflow} was reverted, and its live_trial_reverted flag was not written", workflow=live_trial_id)


def _grades_about(events: Sequence[RunEvidence | Approval | Grade], runs: Sequence[RunEvidence]) -> tuple[Grade, ...]:
    """The grades among `events` about one of `runs`, matched by the run's end time."""
    ended = {run.at for run in runs}
    return tuple(event for event in events if isinstance(event, Grade) and event.graded_run_at in ended)
