"""A step's dataset, the evaluator its cases name, and how a case reads as passed or failed."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from pydantic import JsonValue
from pydantic_evals import Dataset
from pydantic_evals.evaluators import EvaluationReason, Evaluator
from pydantic_evals.reporting import ReportCaseFailure

from harness.domain.contracts import CaseResult

if TYPE_CHECKING:
    from pydantic_evals.evaluators import EvaluatorContext
    from pydantic_evals.reporting import EvaluationReport, ReportCase

StepDataset = Dataset[dict[str, JsonValue], dict[str, JsonValue], dict[str, JsonValue]]
"""A step's dataset: each case's inputs are the step's arguments, its expected output a mapping holding the expected `outcome`."""

CaseDataset = Dataset[dict[str, JsonValue], object, dict[str, JsonValue]]
"""A reusable step's own cases: each case's inputs are the step's arguments, its output whatever the step answers.

The step answers its own output type, so an evaluator reads a field of it as `OutcomeMatches` does, and a case's
expected output is read from the dataset as plain JSON."""


@dataclass
class OutcomeMatches(Evaluator):
    """Scores 1.0 when a step's own `outcome` field matches the case's expected outcome, else 0.0.

    The step's output is read by attribute (`ctx.output.outcome`). The case's expected output is
    the dataset file's own plain mapping, read by key (`ctx.expected_output["outcome"]`).
    """

    def evaluate(self, ctx: EvaluatorContext) -> EvaluationReason:
        """Score this case: 1.0 on a match, 0.0 otherwise, with a reason naming both values.

        Args:
            ctx: The case's step output and expected output.

        Returns:
            The score and the reason.
        """
        got = ctx.output.outcome
        want = ctx.expected_output["outcome"] if ctx.expected_output is not None else None
        reason = f"the step answered {got!r}, the case expected {want!r}"
        return EvaluationReason(value=1.0 if got == want else 0.0, reason=reason)


def bind_collection(dataset: Dataset, collection: str) -> None:
    """Fill in `collection` on every case that does not already name one.

    It is a fallback, not an override: a case that names its own `collection` keeps it.

    Args:
        dataset: The step's cases, changed in place.
        collection: The knowledge base a case without one reads, as the CLI's `--knowledge-base` gives it.
    """
    for case in dataset.cases:
        if "collection" not in case.inputs:
            case.inputs = {**case.inputs, "collection": collection}


def case_result(case: ReportCase | ReportCaseFailure, *, outside: frozenset[str] = frozenset()) -> CaseResult:
    """How one case came out: passed when at least one evaluator judged it and every one passed it.

    An assertion passes when it is true and a score when it is 1.0. A case whose step or one of whose evaluators raised
    an error whose class `outside` names errored, and its reason says `errored` and names the error. Any other raise is
    the step's own failure, and so is a case no evaluator judged.

    Args:
        case: A case of an evaluation report, run or failed.
        outside: The class names of the errors whose cause lies outside the step. Empty reads every raise as the
            step's own failure.

    Returns:
        The case's result, its reason naming every evaluator's verdict or the error.
    """
    if isinstance(case, ReportCaseFailure):
        if _raised(case.error_message) in outside:
            return CaseResult(name=case.name, passed=False, errored=True, reason=f"errored before any evaluator judged it: {case.error_message}")
        return CaseResult(name=case.name, passed=False, errored=False, reason=f"the step raised: {case.error_message}")
    if case.evaluator_failures:
        errored = any(_raised(failure.error_message) in outside for failure in case.evaluator_failures)
        named = ". ".join(
            f"errored while {failure.name} judged it: {failure.error_message}"
            if _raised(failure.error_message) in outside
            else f"{failure.name} raised: {failure.error_message}"
            for failure in case.evaluator_failures
        )
        return CaseResult(name=case.name, passed=False, errored=errored, reason=named)
    verdicts = [*case.assertions.values(), *case.scores.values()]
    if not verdicts:
        return CaseResult(name=case.name, passed=False, errored=False, reason="no evaluator judged this case")
    passed = all(result.value is True for result in case.assertions.values()) and all(result.value == 1.0 for result in case.scores.values())
    reason = ". ".join(f"{result.name}: {result.reason or result.value}" for result in verdicts)
    return CaseResult(name=case.name, passed=passed, errored=False, reason=reason)


def _raised(error_message: str) -> str:
    """The class name of the error a report recorded as `<class name>: <message>`."""
    return error_message.split(":", maxsplit=1)[0]


def report_failed(report: EvaluationReport) -> bool:
    """Whether any case failed: its task raised, or an evaluator that judged it did not pass it.

    Every evaluator counts, its assertions and its scores alike, as `case_result` reads them.

    Args:
        report: The evaluation report to read.

    Returns:
        True when at least one case failed.
    """
    if report.failures:
        return True
    return any(not case_result(case).passed for case in report.cases)
