"""The live trial watch: each run of a consumer version on live trial is read when it ends, and a failed or worse one reverts it."""

from __future__ import annotations

from dataclasses import dataclass
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

    from core.ai.workflows.contracts import Approval, LiveTrialMarker, LiveTrialStep, LiveTrialVerdict, Workflow, WorkflowLibrary

    from harness.adapters.contracts import Notifier
    from harness.features.maintenance.internal_contracts import LiveTrialReads, RunHistoryQueries
    from harness.session import PooledSessions

_logger = get_logger(__name__)

BASELINE_RUNS = 3
"""How many of the previous version's newest completed runs a live trial run is compared with."""


@dataclass(frozen=True)
class LiveTrialRuns:
    """One consumer version on live trial read against its baseline: how each of its runs reads, and whether the live trial is over."""

    workflow: Workflow
    """The version on live trial."""
    marker: LiveTrialMarker
    """Its live trial marker."""
    previous: Workflow
    """The version the marker names as previous, whose runs are the baseline."""
    touched: tuple[str, ...]
    """The nodes every adaptation from the previous version to this one touched."""
    baseline: tuple[RunEvidence, ...]
    """The previous version's newest completed runs, oldest first."""
    baseline_grades: tuple[Grade, ...]
    """The grades about the baseline runs."""
    own: tuple[RunEvidence | Approval | Grade, ...]
    """Everything the evidence log holds about the version on live trial, oldest first."""

    def verdict_on(self, run: RunEvidence) -> LiveTrialVerdict:
        """How one run of the version on live trial reads, with the grades about it on record now.

        Args:
            run: The run, which must be a run of the version on live trial.

        Returns:
            The verdict.
        """
        return live_trial_verdict(
            self.workflow,
            self.touched,
            run,
            run_grades=_grades_about(self.own, (run,)),
            baseline=self.baseline,
            baseline_grades=self.baseline_grades,
            previous=self.previous,
        )

    def is_over(self, *, before: datetime | None = None) -> bool:
        """Whether as many runs counted as the marker names, among the runs that ended before `before`.

        Args:
            before: Read only the runs that ended before this time, or every run on record when None.

        Returns:
            True once the live trial is over.
        """
        ended = [event for event in self.own if isinstance(event, RunEvidence) and (before is None or event.at < before)]
        return sum(self.verdict_on(run).kind == "counts" for run in ended) >= self.marker.runs


async def read_live_trial(
    library: LiveTrialReads, history: RunHistoryQueries, live_trial_id: str, workflow: Workflow, marker: LiveTrialMarker
) -> LiveTrialRuns:
    """The runs of one consumer version on live trial and of the version its marker names as previous, read once.

    The touched nodes are read along the lineages from the version on live trial back to the previous version, so a
    version that stacked an adoption onto another still on live trial is judged at the nodes of every step it covers.

    Args:
        library: The library the versions and their lineages are read from.
        history: The evidence log.
        live_trial_id: The id of the version on live trial.
        workflow: The version on live trial.
        marker: Its live trial marker.

    Returns:
        The reading.

    Raises:
        LibraryError: The previous version or a lineage cannot be read.
    """
    previous = await library.fetch_workflow(marker.previous)
    prior = await history.history_for(spec_hash=spec_identity(previous))
    completed = sorted((event for event in prior if isinstance(event, RunEvidence) and event.outcome == "completed"), key=lambda run: run.at)
    baseline = tuple(completed[-BASELINE_RUNS:])
    return LiveTrialRuns(
        workflow=workflow,
        marker=marker,
        previous=previous,
        touched=await _touched_since(library, live_trial_id, workflow, previous),
        baseline=baseline,
        baseline_grades=_grades_about(prior, baseline),
        own=tuple(await history.history_for(spec_hash=spec_identity(workflow))),
    )


class LiveTrialWatch:
    """Reads each ended run of a consumer version on live trial, and reverts the version after its first failed or worse run.

    Each run is read once, by `live_trial_verdict`, against the newest completed runs of the version the marker names as
    previous, with the grades on record when the watch reads it: a grade written later never changes a verdict already
    acted on. Once as many earlier runs counted as the marker names, the live trial is over and nothing is read again. A
    revert makes the previous version live again, records its decline of every step version on the marker as reverted,
    raises a `live_trial_reverted` maintenance flag for each of those steps and tells the owner once. A version that is
    no longer the newest live one of its name is never reverted. A library that keeps no live trials is never read past
    its type.
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
            marker = None
        if marker is None:
            return
        workflow = await library.fetch_workflow(evidence.workflow_id)
        live_trial_id = await library.locate(workflow.name, workflow.version)
        if live_trial_id is None or spec_identity(workflow) != evidence.spec_hash:
            return
        runs = await read_live_trial(library, self._history, live_trial_id, workflow, marker)
        verdict = runs.verdict_on(evidence)
        if runs.is_over(before=evidence.at) or verdict.kind not in ("failed", "worse"):
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
        for step in marker.steps:
            await self._decline(library, restored, step)
        await self._raise_flags(library, live_trial_id=live_trial_id, workflow=workflow, restored=restored, marker=marker, verdict=verdict, ended_at=evidence.at)
        message = f"{live_trial_id} was reverted after a {verdict.kind} run on live trial, and {restored} is live again. {verdict.reason}"
        await self._notifier.notify(f"Live trial of {', '.join(step.step for step in marker.steps)} reverted", message)

    async def _decline(self, library: WorkflowLibrary, restored: str, step: LiveTrialStep) -> None:
        """Record the restored version's decline of one step version as reverted, logging a failure."""
        if not isinstance(library, ConsumerRecords):
            return
        try:
            await library.record_decline(restored, step=step.step, step_hash=step.step_hash, because="reverted")
        except Exception:
            _logger.exception("{workflow} is live again, and its decline of {step} was not recorded", workflow=restored, step=step.step)

    async def _raise_flags(  # noqa: PLR0913 (each keyword is one fact the flag pages state)
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
        """Write one `live_trial_reverted` flag about the reverted version for each step on its marker, logging each failure."""
        if not isinstance(library, MaintenanceFlags) or not isinstance(library, StepVersionQueries):
            return
        ended = ended_at if ended_at.tzinfo is not None else ended_at.replace(tzinfo=UTC)
        names = ", ".join(step.step for step in marker.steps)
        for step in marker.steps:
            try:
                step_id = next((saved.step_id for saved in await library.step_versions(step.step) if saved.step_hash == step.step_hash), None)
                if step_id is None:
                    _logger.warning(
                        "{workflow} was reverted, and no saved version of {step} has the content address its marker names, so no flag was raised for it",
                        workflow=live_trial_id,
                        step=step.step,
                    )
                    continue
                await library.put_flag(
                    MaintenanceFlag(
                        kind="live_trial_reverted",
                        step=step.step,
                        step_id=step_id,
                        step_hash=step.step_hash,
                        workflow=live_trial_id,
                        title=f"Live trial of {step.step} reverted in {workflow.name}",
                        summary=f"Decide whether {workflow.name} adopts {step_id} again once it is fixed. {restored} is live and declines it.",
                        evidence=(
                            f"A run of {live_trial_id} that ended at {ended.isoformat()} was {verdict.kind}. {verdict.reason}\n\n"
                            f"{live_trial_id} was archived, and {restored}, the version before its live trial, is live again. "
                            f"The live trial covered {names}, and {restored} declines each version it tried."
                        ),
                        raised_by=STEP_MAINTENANCE_AGENT,
                        raised_at=ended,
                    )
                )
            except Exception:
                _logger.exception("{workflow} was reverted, and its live_trial_reverted flag for {step} was not written", workflow=live_trial_id, step=step.step)


async def _touched_since(library: LiveTrialReads, live_trial_id: str, workflow: Workflow, previous: Workflow) -> tuple[str, ...]:
    """The nodes the lineages from the version on live trial back to the previous version touched, each once."""
    touched: list[str] = []
    seen: set[str] = set()
    version_id: str | None = live_trial_id
    while version_id is not None and version_id not in seen:
        seen.add(version_id)
        lineage = await library.fetch_lineage(version_id)
        if lineage is None or lineage.parent_name != workflow.name:
            break
        touched.extend(lineage.touched)
        if lineage.parent_version <= previous.version:
            break
        version_id = await library.locate(workflow.name, lineage.parent_version)
    return tuple(dict.fromkeys(touched))


def _grades_about(events: Sequence[RunEvidence | Approval | Grade], runs: Sequence[RunEvidence]) -> tuple[Grade, ...]:
    """The grades among `events` about one of `runs`, matched by the run's end time."""
    ended = {run.at for run in runs}
    return tuple(event for event in events if isinstance(event, Grade) and event.graded_run_at in ended)
