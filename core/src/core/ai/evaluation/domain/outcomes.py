"""Outcome comparisons: task datasets, paired trials, grading, the comparison report and one task's graded outcome."""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass
from decimal import Decimal
from typing import Literal, Protocol, Self

from pydantic import Field, model_validator

from core.ai.artifacts import ArtifactRef
from core.ai.evaluation.domain.models import (
    EvaluationInput,
    EvaluationModel,
    Fingerprint,
    Nonempty,
    Provenance,
    ResourceUsage,
    RunIdentity,
    Split,
    require_unique,
    total_usage,
)
from core.ai.evaluation.domain.reviews import OwnerReview
from core.ai.evaluation.domain.trace import ExecutionTrace
from core.ai.evaluation.exceptions import EvaluationError
from core.fingerprints import content_fingerprint, model_fingerprint

type Variant = Literal["baseline", "candidate"]


class TaskCase(EvaluationModel):
    """An input with its independent task-success criterion, retained by the grader."""

    case_id: Nonempty
    input: Nonempty
    success_criterion: Nonempty
    split: Split
    provenance: Provenance


class TaskDataset(EvaluationModel):
    """Versioned task outcomes, including optimization and held-out cases."""

    name: Nonempty
    version: Nonempty
    cases: tuple[TaskCase, ...] = Field(min_length=1)

    @model_validator(mode="after")
    def unique_cases(self) -> Self:
        """Refuse ambiguous case identity.

        Returns:
            The dataset, unchanged.

        Raises:
            ValueError: A case id appears twice.
        """
        require_unique((case.case_id for case in self.cases), "case ids")
        return self

    @property
    def fingerprint(self) -> str:
        """Canonical hash of case content, labels, provenance and version."""
        return model_fingerprint(self)


class ComparisonPlan(EvaluationModel):
    """Declare the cases, repetitions and acceptance objective before evaluating a change."""

    dataset: TaskDataset
    optimization_case_ids: tuple[Nonempty, ...] = ()
    evaluation_case_ids: tuple[Nonempty, ...] = Field(min_length=1)
    repetitions: int = Field(default=3, ge=2)
    objective: Literal["task_success", "cost"] = "task_success"

    @model_validator(mode="after")
    def disjoint_held_out_cases(self) -> Self:
        """Require disjoint identities and inputs. A training case cannot be held out.

        Returns:
            The plan, unchanged.

        Raises:
            ValueError: The case lists overlap, name an unknown case, hold out a training case or
                share an input.
        """
        require_unique(self.optimization_case_ids, "optimization case ids")
        require_unique(self.evaluation_case_ids, "evaluation case ids")
        optimization = set(self.optimization_case_ids)
        evaluation = set(self.evaluation_case_ids)
        by_id = {case.case_id: case for case in self.dataset.cases}
        if optimization & evaluation:
            msg = "optimization and held-out evaluation cases must be disjoint"
            raise ValueError(msg)
        if (optimization | evaluation) - by_id.keys():
            msg = "comparison references unknown case ids"
            raise ValueError(msg)
        if any(by_id[case_id].split == "train" for case_id in evaluation):
            msg = "held-out evaluation cases require validation or test split"
            raise ValueError(msg)
        optimized_inputs = {content_fingerprint(by_id[case_id].input) for case_id in optimization}
        if any(content_fingerprint(by_id[case_id].input) in optimized_inputs for case_id in evaluation):
            msg = "contaminated held-out input duplicates an optimization input"
            raise ValueError(msg)
        return self

    @property
    def fingerprint(self) -> str:
        """Hash of the complete predeclared comparison plan."""
        return model_fingerprint(self)


class DiagnosticObservation(EvaluationModel):
    """One labeled execution observation, independent of any particular symptom taxonomy."""

    code: Nonempty
    detail: Nonempty


class TaskExecution(EvaluationModel):
    """Observed output and usage. Execution cannot grade its own task success.

    `trace` is the record of what the run actually did: the payloads its tools delivered and what
    its model turns used. It is `None` when the run was not traced, which is not the same as a run
    that made no tool calls.
    """

    output: str
    usage: ResourceUsage = ResourceUsage()
    diagnostics: tuple[DiagnosticObservation, ...]
    trace: ExecutionTrace | None = None


class TaskGrade(EvaluationModel):
    """Independent outcome assessment against the task's success criterion."""

    success: bool
    reason: Nonempty
    usage: ResourceUsage = ResourceUsage(cost_usd=0)


class TaskRunner[ExecutionT: TaskExecution](Protocol):
    """Execute a case using the declared variant without receiving outcome labels."""

    async def __call__(self, case: EvaluationInput, identity: RunIdentity, repetition: int) -> ExecutionT:
        """Run one independent trial. A recoverable failure raises `EvaluationError`.

        Args:
            case: The task content, without its labels.
            identity: The variant's run identity.
            repetition: Which repetition this trial is.

        Returns:
            The observed execution.

        Raises:
            EvaluationError: The trial failed in a way the comparison records.
        """
        ...


class TaskGrader(Protocol):
    """Judge actual output, blind to variant identity and any execution diagnostics."""

    async def __call__(self, case: TaskCase, output: str) -> TaskGrade:
        """Apply the same independent success criterion to either variant.

        Args:
            case: The case with its success criterion.
            output: The output to grade.

        Returns:
            The grade.

        Raises:
            EvaluationError: Grading failed in a way the comparison records.
        """
        ...


class OutcomeTrial[ExecutionT: TaskExecution](EvaluationModel):
    """A complete or failed paired-trial slot, including costs spent before a failure."""

    case_id: Nonempty
    repetition: int = Field(ge=0)
    variant: Variant
    identity: RunIdentity
    execution: ExecutionT | None = None
    grade: TaskGrade | None = None
    failure: Nonempty | None = None
    failure_usage: ResourceUsage | None = None

    @model_validator(mode="after")
    def consistent_completion(self) -> Self:
        """A completed trial carries output and grade. A failed trial keeps whatever output it has.

        Returns:
            The trial, unchanged.

        Raises:
            ValueError: The failure, the grade and the usage disagree.
        """
        if self.failure is None and (self.execution is None or self.grade is None or self.failure_usage is not None):
            msg = "completed trial requires execution and grade without failure usage"
            raise ValueError(msg)
        if self.failure is not None and (self.grade is not None or self.failure_usage is None):
            msg = "failed trial requires failure usage and cannot carry a success grade"
            raise ValueError(msg)
        return self

    @property
    def usage(self) -> ResourceUsage:
        """Execution and successful or failed grading usage, without losing either cost."""
        return total_usage(
            tuple(
                row
                for row in (self.execution.usage if self.execution else None, self.grade.usage if self.grade else None, self.failure_usage)
                if row is not None
            )
        )


class ComparisonReport[ExecutionT: TaskExecution](EvaluationModel):
    """Descriptive paired results. Acceptance is evidence for review, never permission to deploy."""

    plan: ComparisonPlan
    dataset_fingerprint: Fingerprint
    plan_fingerprint: Fingerprint
    baseline: RunIdentity
    candidate: RunIdentity
    trials: tuple[OutcomeTrial[ExecutionT], ...]
    verdict: Literal["accepted", "rejected"]
    reasons: tuple[str, ...]
    baseline_success_rate: float
    candidate_success_rate: float
    regressions: tuple[str, ...]
    baseline_usage: ResourceUsage
    candidate_usage: ResourceUsage
    statistical_claim: Literal["descriptive_only"] = "descriptive_only"

    @model_validator(mode="after")
    def exact_dataset_and_plan(self) -> Self:
        """Require the exported hashes to name the recorded cases and plan.

        Returns:
            The report, unchanged.

        Raises:
            ValueError: A fingerprint differs from the recorded content.
        """
        if self.dataset_fingerprint != self.plan.dataset.fingerprint or self.plan_fingerprint != self.plan.fingerprint:
            msg = "evaluation dataset or plan fingerprint differs from recorded content"
            raise ValueError(msg)
        return self


def _require_comparable(baseline: RunIdentity, candidate: RunIdentity) -> None:
    if (
        baseline.model != candidate.model
        or baseline.model_version != candidate.model_version
        or baseline.conditions_fingerprint != candidate.conditions_fingerprint
        or baseline.grader_fingerprint != candidate.grader_fingerprint
    ):
        msg = "baseline and candidate require comparable model, version, execution conditions and independent grader"
        raise ValueError(msg)


def _validate_trials[ExecutionT: TaskExecution](
    plan: ComparisonPlan, identities: dict[Variant, RunIdentity], trials: tuple[OutcomeTrial[ExecutionT], ...]
) -> bool:
    expected = {
        (case_id, repetition, variant)
        for case_id in plan.evaluation_case_ids
        for repetition in range(plan.repetitions)
        for variant in identities
    }
    actual: set[tuple[str, int, Variant]] = set()
    for trial in trials:
        slot = (trial.case_id, trial.repetition, trial.variant)
        if slot in actual:
            msg = "duplicate paired trial"
            raise ValueError(msg)
        if slot not in expected or trial.identity != identities[trial.variant]:
            msg = "trial case, repetition or identity does not match the comparison plan"
            raise ValueError(msg)
        actual.add(slot)
    return actual == expected


def _acceptance_reasons[ExecutionT: TaskExecution](
    plan: ComparisonPlan,
    trials: tuple[OutcomeTrial[ExecutionT], ...],
    regressions: tuple[str, ...],
    totals: dict[Variant, int],
    *,
    complete: bool,
) -> tuple[str, ...]:
    reasons: list[str] = []
    if not complete:
        reasons.append("incomplete repetitions")
    if any(trial.failure is not None for trial in trials):
        reasons.append("failed trials")
    if regressions:
        reasons.append("case-level task success regressions")
    if totals["candidate"] > totals["baseline"]:
        return tuple(reasons)
    if plan.objective == "cost" and totals["candidate"] == totals["baseline"]:
        baseline_cost = total_usage(trial.usage for trial in trials if trial.variant == "baseline").cost_usd
        candidate_cost = total_usage(trial.usage for trial in trials if trial.variant == "candidate").cost_usd
        if baseline_cost is not None and candidate_cost is not None and candidate_cost < baseline_cost:
            return tuple(reasons)
        reasons.append("cost improvement requires complete known cost and strictly lower candidate cost")
    else:
        reasons.append("no task-success gain")
    return tuple(reasons)


def assess_comparison[ExecutionT: TaskExecution](
    plan: ComparisonPlan, *, baseline: RunIdentity, candidate: RunIdentity, trials: tuple[OutcomeTrial[ExecutionT], ...]
) -> ComparisonReport[ExecutionT]:
    """Reject missing trials, incomparable conditions, any case regression and diagnostic-only gains.

    Case success is the observed fraction across the declared repetitions. These small-sample
    comparisons make no statistical-significance or population-generalization claim.

    Args:
        plan: The predeclared comparison plan.
        baseline: The baseline's run identity.
        candidate: The candidate's run identity.
        trials: Every paired trial, completed or failed.

    Returns:
        The comparison report with its verdict and reasons.

    Raises:
        ValueError: The identities are not comparable, or a trial is duplicated or does not match
            the plan.
    """
    _require_comparable(baseline, candidate)
    complete = _validate_trials(plan, {"baseline": baseline, "candidate": candidate}, trials)
    successes: Counter[tuple[str, Variant]] = Counter()
    for trial in trials:
        if trial.grade and trial.grade.success:
            successes[trial.case_id, trial.variant] += 1
    regressions = tuple(case_id for case_id in plan.evaluation_case_ids if successes[case_id, "candidate"] < successes[case_id, "baseline"])
    totals: dict[Variant, int] = {
        variant: sum(count for (_, v), count in successes.items() if v == variant) for variant in ("baseline", "candidate")
    }
    reasons = _acceptance_reasons(plan, trials, regressions, totals, complete=complete)
    if baseline == candidate:
        reasons = (*reasons, "unchanged candidate")
    denominator = len(plan.evaluation_case_ids) * plan.repetitions
    return ComparisonReport[ExecutionT](
        plan=plan,
        dataset_fingerprint=plan.dataset.fingerprint,
        plan_fingerprint=plan.fingerprint,
        baseline=baseline,
        candidate=candidate,
        trials=trials,
        verdict="rejected" if reasons else "accepted",
        reasons=reasons,
        baseline_success_rate=totals["baseline"] / denominator,
        candidate_success_rate=totals["candidate"] / denominator,
        regressions=regressions,
        baseline_usage=total_usage(trial.usage for trial in trials if trial.variant == "baseline"),
        candidate_usage=total_usage(trial.usage for trial in trials if trial.variant == "candidate"),
    )


@dataclass(frozen=True)
class _TrialSlot:
    repetition: int
    variant: Variant
    identity: RunIdentity


async def _run_trial[ExecutionT: TaskExecution](
    case: TaskCase, runner: TaskRunner[ExecutionT], grader: TaskGrader, slot: _TrialSlot
) -> OutcomeTrial[ExecutionT]:
    execution: ExecutionT | None = None
    try:
        execution = await runner(EvaluationInput(case_id=case.case_id, input=case.input), slot.identity, slot.repetition)
        grade = await grader(case, execution.output)
    except EvaluationError as error:
        return OutcomeTrial[ExecutionT](
            case_id=case.case_id,
            repetition=slot.repetition,
            variant=slot.variant,
            identity=slot.identity,
            execution=execution,
            failure=str(error),
            failure_usage=error.usage,
        )
    return OutcomeTrial[ExecutionT](
        case_id=case.case_id, repetition=slot.repetition, variant=slot.variant, identity=slot.identity, execution=execution, grade=grade
    )


async def compare_outcomes[ExecutionT: TaskExecution](
    plan: ComparisonPlan, runner: TaskRunner[ExecutionT], grader: TaskGrader, *, baseline: RunIdentity, candidate: RunIdentity
) -> ComparisonReport[ExecutionT]:
    """Run paired independent outcomes with alternating variant order across repetitions.

    The runner sees neither diagnostic labels nor success criteria. The grader sees neither
    variant identity nor execution diagnostics. Provider timeouts belong to each adapter.

    Args:
        plan: The predeclared comparison plan.
        runner: The callback that executes one trial.
        grader: The callback that grades one output.
        baseline: The baseline's run identity.
        candidate: The candidate's run identity.

    Returns:
        The comparison report over every trial.

    Raises:
        ValueError: The identities are not comparable.
    """
    _require_comparable(baseline, candidate)
    cases = {case.case_id: case for case in plan.dataset.cases}
    identities: tuple[tuple[Variant, RunIdentity], ...] = (("baseline", baseline), ("candidate", candidate))
    trials: list[OutcomeTrial[ExecutionT]] = []
    for repetition in range(plan.repetitions):
        order = identities if repetition % 2 == 0 else tuple(reversed(identities))
        for case_id in plan.evaluation_case_ids:
            for variant, identity in order:
                slot = _TrialSlot(repetition=repetition, variant=variant, identity=identity)
                trials.append(await _run_trial(cases[case_id], runner, grader, slot))
    return assess_comparison(plan, baseline=baseline, candidate=candidate, trials=tuple(trials))


class Assessment(EvaluationModel):
    """One graded dimension of a task outcome: what was found, and who or what found it."""

    status: Literal["pass", "fail", "unknown", "not_applicable"]
    reason: Nonempty
    grader: Provenance | None = None


class EvaluationLimits(EvaluationModel):
    """Accrual stop thresholds for one live trial against a generative model.

    Dollar figures are where a budgeted model halts further spending, not a ceiling on the
    resulting bill or a promise about quality. Every field must be strictly positive: a limit of
    zero or less stops a trial before it starts rather than bounding one.
    """

    cycle_cost_stop_usd: Decimal = Field(gt=0)
    trial_cost_stop_usd: Decimal = Field(gt=0)
    trial_deadline_seconds: int = Field(gt=0)
    trial_request_limit: int = Field(gt=0)
    trial_attempt_limit: int = Field(gt=0)


class TaskOutcome(EvaluationModel):
    """One task's graded outcome: structure, design, evidence and success, kept apart.

    Each `Assessment` is independent: a structurally invalid attempt can still be graded on
    whatever evidence it produced, and a successful one is not thereby graded as well designed.
    `abandoned` and `correction_seconds` record whether and how long a person had to intervene,
    apart from whether the task ultimately succeeded.
    """

    run_id: Nonempty
    task_id: Nonempty
    structural_validity: Assessment
    workflow_design: Assessment
    evidence_support: Assessment
    task_success: Assessment
    abandoned: bool = False
    correction_seconds: float | None = Field(default=None, ge=0)
    observed_result: str | None = None
    evidence_refs: tuple[Nonempty, ...] = ()
    artifact_refs: tuple[ArtifactRef, ...] = ()
    usage: ResourceUsage = ResourceUsage()


class ReviewedTaskOutcome(EvaluationModel):
    """A task outcome beside the owner reviews decided over its own fixed artifacts.

    `owner_acceptance` is a separate fact from `outcome.task_success`: a run can succeed by its
    own checked facts and still be rejected by an owner, or the reverse. With no review at all,
    owner acceptance cannot say anything but `unknown`. Skipped human judgment never silently
    reads as either accepted or rejected.
    """

    outcome: TaskOutcome
    owner_acceptance: Assessment
    reviews: tuple[OwnerReview, ...] = ()

    @model_validator(mode="after")
    def unreviewed_stays_unknown(self) -> Self:
        """Refuse a claimed acceptance status that no owner review backs.

        Returns:
            The outcome, unchanged.

        Raises:
            ValueError: Owner acceptance is not `unknown` and no review exists.
        """
        if not self.reviews and self.owner_acceptance.status != "unknown":
            msg = "owner acceptance without any review must stay unknown"
            raise ValueError(msg)
        return self
