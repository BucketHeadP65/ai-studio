"""The one dataset-level evaluator a step's cases name: does its verdict match the expected one."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from pydantic_evals.evaluators import EvaluationReason, Evaluator

if TYPE_CHECKING:
    from pydantic_evals import Dataset
    from pydantic_evals.evaluators import EvaluatorContext
    from pydantic_evals.reporting import EvaluationReport


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


_OUTCOME_KEY = OutcomeMatches.get_serialization_name()


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


def report_failed(report: EvaluationReport) -> bool:
    """Whether any case failed: its task raised, or its `OutcomeMatches` score was not 1.0.

    Args:
        report: The evaluation report to read.

    Returns:
        True when at least one case failed.
    """
    if report.failures:
        return True
    return any(case.scores.get(_OUTCOME_KEY) is None or case.scores[_OUTCOME_KEY].value != 1.0 for case in report.cases)
