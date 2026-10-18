"""The outcome-evaluation slice's own port: the blind answer grader."""

from __future__ import annotations

from typing import TYPE_CHECKING, Protocol

if TYPE_CHECKING:
    from core.ai.evaluation.contracts import DeliveredEvidenceRef

    from engram.evals.outcomes.domain.answers import AnswerAssessment, AnswerCriterion


class AnswerGrader(Protocol):
    """Grade one answer against its hidden criterion, reading only what the trial retained."""

    async def __call__(self, criterion: AnswerCriterion, answer: str, evidence: tuple[DeliveredEvidenceRef, ...]) -> AnswerAssessment:
        """The verdict on `answer`, from `criterion` and the payloads `evidence` names.

        Args:
            criterion: What the case expects of the answer.
            answer: The answer under grading.
            evidence: The delivered-evidence references this trial retained.

        Returns:
            The graded verdict. Evidence that cannot be read back exactly makes every dimension the
            criterion asks for `unknown` rather than raising.
        """
        ...
