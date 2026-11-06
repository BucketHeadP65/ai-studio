"""The ports the maintenance slice reads and writes through: the evidence log, the library, a step's case runs and the schedules."""

from __future__ import annotations

from typing import TYPE_CHECKING, Protocol

from core.ai.workflows.contracts import (
    ConsumerRecords,
    LiveTrialLibrary,
    MaintenanceFlags,
    ReusableStepQueries,
    StepVersionQueries,
    WorkflowVersionQueries,
)

if TYPE_CHECKING:
    from collections.abc import Collection, Mapping, Sequence

    from core.ai.workflows.contracts import Approval, Grade, InlineStep, Lineage, RunEvidence, SavedWorkflow, Workflow
    from pydantic import JsonValue

    from harness.adapters.contracts import StandInCalls
    from harness.domain.contracts import CaseReport, FailedFireSteps


class StepSchedules(Protocol):
    """The on-step-change schedules, which take back the changed steps of a fire that ended failed."""

    async def put_back_steps(self, evidence: RunEvidence) -> FailedFireSteps | None:
        """Put the changed steps of a failed on-step-change fire back on its schedule when the run stopped with a provider error.

        A step goes back on one schedule once, and a paused schedule takes nothing back.

        Args:
            evidence: What the failed run's ending recorded.

        Returns:
            Which steps were put back and which need a run by hand, or None when no on-step-change schedule fired the run
            with changed steps.
        """
        ...


class RunHistoryQueries(Protocol):
    """The evidence log's reads by content address: every run, approval and grade one or several workflow versions hold."""

    async def history_for(self, *, spec_hash: str) -> Sequence[RunEvidence | Approval | Grade]:
        """Everything the evidence log holds about one workflow version.

        Args:
            spec_hash: The version's content address. It must not be empty.

        Returns:
            Its events, oldest first.

        Raises:
            ValueError: If `spec_hash` is empty.
        """
        ...

    async def history_for_any(self, *, spec_hashes: Collection[str]) -> Sequence[RunEvidence | Approval | Grade]:
        """Everything the evidence log holds about any of several workflow versions, read at once.

        Args:
            spec_hashes: The versions' content addresses. None may be empty. No address answers nothing.

        Returns:
            Their events, oldest first.

        Raises:
            ValueError: If one of `spec_hashes` is empty.
        """
        ...


class LiveTrialReads(Protocol):
    """The library reads a live trial is judged by: a workflow version, where it came from, and the id of another version."""

    async def fetch_workflow(self, workflow_id: str) -> Workflow:
        """The workflow version `workflow_id` names.

        Args:
            workflow_id: The workflow version's id, `wf.<name>.v<version>`.

        Returns:
            The workflow.

        Raises:
            WorkflowUnknownError: If no workflow has that id.
        """
        ...

    async def fetch_lineage(self, workflow_id: str) -> Lineage | None:
        """The adaptation that made the workflow version `workflow_id` names, or None when nobody adapted anything to make it.

        Args:
            workflow_id: The workflow version's id.

        Returns:
            The lineage, or None.

        Raises:
            WorkflowUnknownError: If no workflow has that id.
        """
        ...

    async def locate(self, name: str, version: int) -> str | None:
        """The id one version of a workflow is kept under, or None when the library does not have it.

        Args:
            name: The workflow's name.
            version: The version number.

        Returns:
            The id, or None.
        """
        ...


class MaintenanceLibrary(
    ConsumerRecords,
    StepVersionQueries,
    ReusableStepQueries,
    MaintenanceFlags,
    LiveTrialLibrary,
    WorkflowVersionQueries,
    LiveTrialReads,
    Protocol,
):
    """Every library read and write the step maintenance steps make: consumers, step versions, flags and adoptions."""

    async def fetch_card(self, workflow_id: str) -> SavedWorkflow | None:
        """One workflow version's card: its title, summary, applicability, presentation and intent.

        Args:
            workflow_id: The workflow version's id.

        Returns:
            The card, or None when the library has no such workflow.
        """
        ...

    async def current(self, workflow_id: str) -> str:
        """The id of the newest live version of the workflow `workflow_id` names, or `workflow_id` itself.

        Args:
            workflow_id: The id of any version of the workflow.

        Returns:
            The id of the newest live version, or `workflow_id` when the library names no newer one.
        """
        ...


class CaseRunner(Protocol):
    """How step maintenance runs a reusable step's own cases: the case file checked first, then the cases performed."""

    def refusal(self, step_id: str, cases: Mapping[str, JsonValue]) -> str | None:
        """Why a step's case file cannot run, before any case runs, or None when it can.

        Args:
            step_id: The step version's id, which the refusal names.
            cases: The step's dataset in `pydantic_evals` form.

        Returns:
            The refusal, saying what to change in the case file, or None.
        """
        ...

    async def perform(self, step: InlineStep, step_id: str, cases: Mapping[str, JsonValue] | None, calls: StandInCalls) -> CaseReport:
        """The case report of one step version: the kept one, or a new one from running every case over stand-ins.

        Pass a new `calls` list for each step, because the report counts the stand-in calls by how much it grew.

        Args:
            step: The step version whose cases run.
            step_id: The step version's id, which the report names.
            cases: The step's dataset in `pydantic_evals` form, or None when it has none.
            calls: The list the stand-ins of this step's case run record into.

        Returns:
            The case report.

        Raises:
            RuntimeError: The registry the cases run through cannot perform the step. Nothing has run.
        """
        ...
