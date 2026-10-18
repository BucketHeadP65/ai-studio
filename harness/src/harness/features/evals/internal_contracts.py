"""The ports the evals slice reads and spends through.

The library, running, the budget record and the CLI fill them.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Protocol

from core.ai.evaluation.contracts import EvaluationBudgetRecord, VerifiedStateRoot

if TYPE_CHECKING:
    from datetime import datetime

    from core.ai.evaluation.contracts import (
        BudgetCycleState,
        BudgetRole,
        BudgetScope,
        EvaluationInput,
        EvaluationLimits,
        OwnerReview,
        RunIdentity,
        TaskCase,
        TaskGrade,
    )
    from core.ai.workflows.contracts import InlineStep
    from pydantic_ai.models import Model

    from harness.features.evals.domain.benchmark import UsageSource
    from harness.features.evals.domain.outcomes import ArchivedRun, RunSpendView
    from harness.features.evals.domain.pilot import PilotTrial, WorkflowAutomationExecution
    from harness.features.evals.infrastructure.budgeted_model import PilotMeter


class ReusableStepQueries(Protocol):
    """The one library read a step eval needs: a step document behind its own id."""

    async def fetch_step(self, step_id: str) -> InlineStep:
        """The reusable step `step_id` names, validated as an inline step.

        Args:
            step_id: The reusable step's id.

        Returns:
            The step as a `Briefing`, `Program` or `AgentStep`.

        Raises:
            LibraryError: no reusable step has that id.
        """
        ...


class ArchivedRuns(Protocol):
    """The one archive read a grader needs: the whole record behind one run id."""

    def get(self, run_id: str) -> ArchivedRun | None:
        """The archived record of one run.

        Args:
            run_id: The run's id.

        Returns:
            The record filed under `run_id`, or `None` when nothing is.
        """
        ...


class RunSpendRecords(Protocol):
    """The one spend read a graded outcome needs."""

    def find(self, run_id: str) -> RunSpendView | None:
        """The run's folded spend, under whichever workflow filed it.

        Args:
            run_id: The run's id.

        Returns:
            The folded spend, or `None` when the run has none.
        """
        ...


class PilotRuns(Protocol):
    """Which run one case's pilot trial produced."""

    async def trial_for(self, case_id: str, repetition: int) -> PilotTrial | None:
        """The trial one repetition of a case produced.

        Args:
            case_id: The evaluation case's id.
            repetition: The repetition of that case.

        Returns:
            The trial, or `None` when that journey never ran.
        """
        ...


class PilotBudgetRecord(VerifiedStateRoot, Protocol):
    """What opening a pilot cycle needs of its record: the root it verifies, and the cycle itself."""

    async def begin_cycle(self, cycle_id: str, limits: EvaluationLimits, *, pricing_known: bool, at: datetime) -> None:
        """Open `cycle_id` under `limits`, or accept the identical call again without resetting it.

        Args:
            cycle_id: The cycle to open.
            limits: The stop thresholds the cycle runs under.
            pricing_known: Whether every configured role's model has a resolvable price.
            at: When the cycle starts.

        Raises:
            BudgetPreflightError: `pricing_known` is false, or this process cannot verify the
                cycle's own state root.
            BudgetConflictError: the cycle exists under different limits or a different start.
        """


class PilotCycleRecord(PilotBudgetRecord, Protocol):
    """What a live pilot needs of its record beyond opening a cycle: the trial, and the state itself."""

    async def begin_trial(self, scope: BudgetScope, *, at: datetime) -> None:
        """Open `scope.trial_id` at `at`, deadline included, or accept the identical call again.

        Args:
            scope: The cycle and trial to open.
            at: When the trial starts.

        Raises:
            BudgetNotOpenError: the cycle was never begun.
            BudgetConflictError: the trial exists with a different start.
        """
        ...

    async def read(self, cycle_id: str) -> BudgetCycleState | None:
        """The whole persisted state of one cycle.

        Args:
            cycle_id: The cycle to read.

        Returns:
            The cycle state, or `None` when nothing has opened it.
        """
        ...


class BenchmarkRecord(EvaluationBudgetRecord, PilotCycleRecord, Protocol):
    """What a benchmark needs of its record: the pilot's own cycle reads, plus admission and settlement."""


class BenchmarkReviews(Protocol):
    """The slice of the owner-review store a benchmark reads: what was decided about one run."""

    async def for_run(self, run_id: str) -> tuple[OwnerReview, ...]:
        """Every review committed against `run_id`, in the order they were appended.

        Args:
            run_id: The run whose reviews are wanted.

        Returns:
            The reviews, oldest first.
        """
        ...


class BenchmarkArm(Protocol):
    """One arm of a comparison: a runner that names the model paths it requests through, and their source."""

    @property
    def request_paths(self) -> tuple[BenchmarkModel, ...]:
        """Every declared path this arm makes its own requests through, empty when it makes none."""
        ...

    @property
    def usage_source(self) -> UsageSource:
        """Whether the usage this arm reports was admitted by this record or measured by another."""
        ...

    async def __call__(self, case: EvaluationInput, identity: RunIdentity, repetition: int) -> WorkflowAutomationExecution:
        """Run one trial of this case without receiving any of its labels.

        Args:
            case: The case id and request.
            identity: The run identity of this arm.
            repetition: The repetition of the case this trial is.

        Returns:
            The execution, with the usage this arm reports.

        Raises:
            EvaluationError: the trial failed. Its `usage` carries what the arm spent before it failed.
        """
        ...


class BenchmarkGrader(Protocol):
    """The grader of a comparison: a grader that names the model paths it requests through, and their source."""

    @property
    def request_paths(self) -> tuple[BenchmarkModel, ...]:
        """Every declared path this grader makes its own requests through, empty when it makes none."""
        ...

    @property
    def usage_source(self) -> UsageSource:
        """Whether the usage this grader reports was admitted by this record or measured by another."""
        ...

    async def __call__(self, case: TaskCase, output: str) -> TaskGrade:
        """Judge one output against this case's own criterion, blind to which arm produced it.

        Args:
            case: The case, criterion included.
            output: The output text to judge.

        Returns:
            The grade, with the usage this grader reports.

        Raises:
            EvaluationError: the grading failed. Its `usage` carries what the grader spent before it failed.
        """
        ...


class BenchmarkComposer(Protocol):
    """Builds the metered models, arms and grader for one cycle.

    It is called only after the sealed cases pass their custody audit.
    """

    async def __call__(self, cycle_id: str) -> ComposedBenchmark:
        """Build everything the benchmark runs through, mounted on the record for `cycle_id`.

        Args:
            cycle_id: The benchmark's cycle, which every mounted model reports to.

        Returns:
            The composed benchmark.
        """
        ...


@dataclass(frozen=True)
class BenchmarkModel:
    """One generative model path a benchmark takes, named by where the request is made from."""

    path: str
    role: BudgetRole
    model: Model


@dataclass(frozen=True)
class BenchmarkArms:
    """The two runners a comparison puts side by side, and the one grader over both."""

    baseline: BenchmarkArm
    candidate: BenchmarkArm
    grader: BenchmarkGrader


@dataclass(frozen=True)
class ComposedBenchmark:
    """What one benchmark actually holds once it has been built, as it was built.

    `models` is every generative model path the two arms and the grader make requests through, in
    whatever state the composer left them. The preflight reads these objects to decide what is
    metered, and `unbound_paths` holds them against the paths the arms themselves name.
    """

    models: tuple[BenchmarkModel, ...]
    arms: BenchmarkArms
    baseline: RunIdentity
    candidate: RunIdentity
    meter: PilotMeter | None = None
    """A meter moved onto each slot's trial along with `models`, or `None` when there is none."""
    reviews: BenchmarkReviews | None = None
    """Where owner reviews are read. `None` reports every run's acceptance as `unknown`."""
