"""What an archived workflow-automation run is graded as, from its own recorded facts alone.

Each dimension of a `TaskOutcome` is read from its own fact and none is inferred from another. A
run's `Outcome` never says the work was useful, and nothing here upgrades it into that. `unknown` is
the answer wherever the record cannot say, and a skip is never a success. A `noop` passes only for a
case whose labels say having nothing to do is the right answer. Only `execution_input`, the case id
and the request, crosses from the author-visible development set into a run.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Literal, Protocol, Self

from core.ai.evaluation.contracts import (
    Assessment,
    BudgetRefusal,
    EvaluationInput,
    EvaluationModel,
    Nonempty,
    Provenance,
    Split,
    TaskCase,
    require_unique,
)
from core.ai.workflows.contracts import OutcomeStatus, WorkflowIntent, WorkflowResultSpec, intent_issues
from pydantic import Field, model_validator

if TYPE_CHECKING:
    from decimal import Decimal

    from core.ai.workflows.contracts import Outcome


class ArchivedRun(Protocol):
    """The slice of an ended run's record a grader reads: how it ended, what it checked, what ran."""

    @property
    def run_id(self) -> str:
        """The run this record is of."""
        ...

    @property
    def outcome(self) -> Literal["completed", "failed", "stopped"]:
        """How the execution ended, which is a separate fact from what the run did."""
        ...

    @property
    def result(self) -> Outcome | None:
        """The run's own checked outcome, or `None` for a record that captured none."""
        ...

    @property
    def spec(self) -> dict[str, object]:
        """The workflow snapshot that ran, as the record holds it."""
        ...


class SpendTotalsView(Protocol):
    """The slice of a run's folded spend a graded outcome reports."""

    @property
    def input_tokens(self) -> int:
        """Tokens the run's requests carried in."""
        ...

    @property
    def output_tokens(self) -> int:
        """Tokens its responses carried back."""
        ...

    @property
    def cost_usd(self) -> Decimal:
        """What the priced rows came to, a floor while `unpriced` is above zero."""
        ...

    @property
    def unpriced(self) -> int:
        """How many rows nothing could price. Above zero, the run's cost is unknown."""
        ...


class RunSpendView(Protocol):
    """One run's spend record, folded."""

    @property
    def totals(self) -> SpendTotalsView:
        """The run's own totals, across every step of it."""
        ...


type AssessmentStatus = Literal["pass", "fail", "unknown", "not_applicable"]
"""One graded dimension's answer.

`not_applicable` is for a question nobody asked: a case that does not ask it, or a result that
requires nothing the dimension grades. It is never a stand-in for an answer that could not be
established, which is `unknown`.
"""

type CaseFamily = Literal["workflow_design", "task_outcome", "answer_support", "budget_admission", "execution_failure"]
"""Which question a development case is about. One case asks one of them."""

type AnswerShape = Literal["supported", "contradicted", "ambiguous", "unsupported", "not_applicable"]
"""What the evidence behind an answer looks like, for the cases that grade an answer at all."""

_GRADER = Provenance(source="harness.features.evals.outcomes", kind="reproduced", review="deterministic", reviewer="archived run facts")


class CaseLabels(EvaluationModel):
    """What the grader holds for one case. None of it ever reaches a run.

    `permits_no_work` is the only thing that lets a `noop` pass: a case where having nothing to do
    is the right answer says so here, and every other case grades a `noop` as work not done.
    """

    success_criterion: Nonempty
    expected_task_success: AssessmentStatus
    permits_no_work: bool = False
    expected_result_status: OutcomeStatus | None = None
    expected_answer: AnswerShape = "not_applicable"
    expected_budget_refusal: BudgetRefusal | None = None
    notes: str = ""


class WorkflowAutomationCase(EvaluationModel):
    """One development case: the request a run sees, and the labels only a grader sees.

    `family` is which question the case asks, which is the same five questions whatever the work is
    about. `task_family` is the kind of work itself, and it must be one the set declares. It is an
    opaque label that nothing here interprets, and a case about the journey rather than about one
    kind of work names none.
    """

    case_id: Nonempty
    family: CaseFamily
    task_family: Nonempty | None = None
    visibility: Literal["author_visible"]
    split: Split
    request: Nonempty
    provenance: Provenance
    labels: CaseLabels

    @property
    def execution_input(self) -> EvaluationInput:
        """The whole of what execution is given: this case's id and the request itself."""
        return EvaluationInput(case_id=self.case_id, input=self.request)

    @property
    def task_case(self) -> TaskCase:
        """This case as the kernel's own graded case, criterion included, for the grading side."""
        return TaskCase(
            case_id=self.case_id,
            input=self.request,
            success_criterion=self.labels.success_criterion,
            split=self.split,
            provenance=self.provenance,
        )


class WorkflowAutomationDataset(EvaluationModel):
    """A named, versioned set of author-visible development cases.

    Author-visible is the only visibility this carries. Its cases are development evidence, and no
    version of this dataset is independently withheld. `task_families` declares the kinds of work the
    set covers, and a case naming a kind of work this version did not declare is refused.
    """

    name: Nonempty
    version: Nonempty
    task_families: tuple[Nonempty, ...] = ()
    cases: tuple[WorkflowAutomationCase, ...] = Field(min_length=1)

    @model_validator(mode="after")
    def unique_cases(self) -> Self:
        """Refuse two cases under one id, two cases with the same request, or an undeclared kind of work.

        Returns:
            The dataset, unchanged.

        Raises:
            ValueError: two cases share an id or a request, a kind of work is declared twice, or a
                case names one the set does not declare.
        """
        require_unique((case.case_id for case in self.cases), "case ids")
        require_unique((case.request for case in self.cases), "case requests")
        require_unique(self.task_families, "task families")
        declared = set(self.task_families)
        undeclared = sorted({case.task_family for case in self.cases if case.task_family is not None} - declared)
        if undeclared:
            msg = f"these cases name a kind of work this version does not declare: {', '.join(undeclared)}"
            raise ValueError(msg)
        return self

    @property
    def execution_inputs(self) -> tuple[EvaluationInput, ...]:
        """Every case as execution receives it, which is how a caller checks no label crossed over."""
        return tuple(case.execution_input for case in self.cases)

    def case(self, case_id: str) -> WorkflowAutomationCase | None:
        """The case `case_id` names, or `None` when this version carries no such case.

        Args:
            case_id: The case to find.

        Returns:
            The case, or `None`.
        """
        return next((case for case in self.cases if case.case_id == case_id), None)


def declared_result(record: ArchivedRun) -> WorkflowResultSpec | None:
    """The result the run's own workflow snapshot declared, or `None` when it declared none.

    Args:
        record: The archived run.

    Returns:
        The declared result, or `None`.

    Raises:
        ValidationError: the snapshot's result is not a valid result declaration.
    """
    declared = record.spec.get("result")
    if not isinstance(declared, dict):
        return None
    return WorkflowResultSpec.model_validate(declared)


def structural_validity(record: ArchivedRun) -> Assessment:
    """Whether the run ended holding a result that holds together.

    Counts that do not add up and an execution that never completed are both failures of structure.
    A record carrying no checked outcome at all is unknown: nothing was captured to check.

    Args:
        record: The archived run.

    Returns:
        The structural validity assessment.
    """
    result = record.result
    if result is None:
        return Assessment(status="unknown", reason="the archived run records no checked outcome", grader=_GRADER)
    if result.attempted != result.succeeded + result.skipped + result.failed:
        reason = f"the counts do not add up: {result.attempted} attempted against {result.succeeded}/{result.skipped}/{result.failed}"
        return Assessment(status="fail", reason=reason, grader=_GRADER)
    if record.outcome != "completed":
        return Assessment(status="fail", reason=f"the execution ended {record.outcome}", grader=_GRADER)
    return Assessment(status="pass", reason="the run completed and its counts add up", grader=_GRADER)


def workflow_design(record: ArchivedRun, intent: WorkflowIntent | None) -> Assessment:
    """Whether the automation was designed against a stated request and declares what it returns.

    Read from the intent beside the workflow and the result the workflow declares, never from how
    the run happened to go. A workflow nobody wrote an intent for cannot be graded on design.

    Args:
        record: The archived run.
        intent: The intent recorded beside the workflow, or `None` when nobody wrote one.

    Returns:
        The workflow design assessment.
    """
    if intent is None:
        return Assessment(status="unknown", reason="no intent was recorded beside this workflow", grader=_GRADER)
    result = declared_result(record)
    if result is None:
        return Assessment(status="fail", reason="the workflow declares no result a caller may select", grader=_GRADER)
    owed = intent_issues(intent)
    if owed:
        return Assessment(status="fail", reason=f"the intent still owes {', '.join(owed)}", grader=_GRADER)
    return Assessment(status="pass", reason=f"the request is stated and the result declares {result.summary}", grader=_GRADER)


def evidence_support(record: ArchivedRun) -> Assessment:
    """What the required gates recorded over the exact value the run ended holding.

    A result that requires no acceptance asks no question here, which is `not_applicable` rather
    than a pass. A check nobody could put is unknown, and a refusal is a failure.

    Args:
        record: The archived run.

    Returns:
        The evidence support assessment.
    """
    result = record.result
    if result is None:
        return Assessment(status="unknown", reason="the archived run records no checked outcome", grader=_GRADER)
    required = tuple(check for check in result.checks if check.required)
    if not required:
        return Assessment(status="not_applicable", reason="the result requires no recorded acceptance", grader=_GRADER)
    if any(check.passed is None for check in required):
        unanswered = ", ".join(check.id for check in required if check.passed is None)
        return Assessment(status="unknown", reason=f"no decision reached {unanswered}", grader=_GRADER)
    refused = tuple(check for check in required if not check.passed)
    if refused:
        return Assessment(status="fail", reason=f"{', '.join(check.id for check in refused)} was not accepted", grader=_GRADER)
    return Assessment(status="pass", reason=f"{len(required)} required gate(s) accepted the value the run ended holding", grader=_GRADER)


def task_success(result: Outcome | None, labels: CaseLabels) -> Assessment:
    """Whether the run did the work the case asked for, by the case's own criterion.

    Success is the one status that passes on its own. `noop` passes only where the case permits no
    work. `partial` and `failed` cannot pass however much of the work landed, and `unknown` is not
    a verdict at all.

    Args:
        result: The run's checked outcome, or `None` for a record that captured none.
        labels: The case's labels, whose `permits_no_work` decides a `noop`.

    Returns:
        The task success assessment.
    """
    if result is None:
        return Assessment(status="unknown", reason="the archived run records no checked outcome", grader=_GRADER)
    if result.status == "unknown":
        return Assessment(status="unknown", reason=f"the run's own facts establish nothing: {result.summary}", grader=_GRADER)
    if result.status == "succeeded":
        return Assessment(status="pass", reason=f"every piece of the primary work settled: {_counts(result)}", grader=_GRADER)
    if result.status == "noop":
        if labels.permits_no_work:
            return Assessment(status="pass", reason="the case permits no work, and the run correctly did none", grader=_GRADER)
        return Assessment(status="fail", reason="the run did no work, and the case asks for work to be done", grader=_GRADER)
    return Assessment(status="fail", reason=f"the run ended {result.status}: {_counts(result)}", grader=_GRADER)


def _counts(result: Outcome) -> str:
    """The settled work, and the rework it took, named apart so a repair never inflates either."""
    settled = f"{result.succeeded} succeeded, {result.skipped} skipped, {result.failed} failed of {result.attempted}"
    return f"{settled}, after {result.retry_attempts} rework(s)" if result.retry_attempts else settled


class DevelopmentDatasetStamp(EvaluationModel):
    """The governance sidecar filed beside one development dataset version.

    An author-visible set makes no held-out claim at all: it names no sealed cases and has no
    independent custody to have verified. Both are refused here rather than left to a reader.
    """

    dataset: Nonempty
    version: Nonempty
    visibility: Literal["author_visible"]
    custody: Nonempty
    case_ids: tuple[Nonempty, ...] = Field(min_length=1)
    task_families: tuple[Nonempty, ...] = ()
    """The kinds of work this version covers, named once each. Empty for a set that names none."""
    held_out_case_ids: tuple[Nonempty, ...] = ()
    independent_holdout_verified: bool | None = None
    notes: str = ""

    @model_validator(mode="after")
    def no_held_out_claim(self) -> Self:
        """Refuse an author-visible set that claims sealed cases or a verified holdout.

        Returns:
            The stamp, unchanged.

        Raises:
            ValueError: the stamp names held-out cases, states a holdout verification, or names a case
                or a kind of work twice.
        """
        if self.held_out_case_ids:
            msg = "an author-visible development set names no held-out cases"
            raise ValueError(msg)
        if self.independent_holdout_verified is not None:
            msg = "an author-visible development set has no independent custody to verify"
            raise ValueError(msg)
        require_unique(self.case_ids, "stamped case ids")
        require_unique(self.task_families, "stamped task families")
        return self


def require_stamped(dataset: WorkflowAutomationDataset, stamp: DevelopmentDatasetStamp) -> None:
    """Refuse a stamp that does not cover exactly this dataset version, its cases and its task families.

    Args:
        dataset: The dataset version the stamp must cover.
        stamp: The stamp filed beside it.

    Raises:
        ValueError: the stamp names another dataset version, a different set of cases, or different
            kinds of work than the cases themselves declare.
    """
    if stamp.dataset != dataset.name or stamp.version != dataset.version:
        msg = f"the stamp names {stamp.dataset} {stamp.version}, and the dataset is {dataset.name} {dataset.version}"
        raise ValueError(msg)
    if set(stamp.case_ids) != {case.case_id for case in dataset.cases}:
        msg = "the stamp names a different set of cases than the dataset carries"
        raise ValueError(msg)
    if set(stamp.task_families) != set(dataset.task_families):
        msg = "the stamp names different kinds of work than the cases themselves declare"
        raise ValueError(msg)
