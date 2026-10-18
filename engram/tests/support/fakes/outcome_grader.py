"""A scriptable `AnswerGrader` for callers that need a verdict without running a model.

`FakeAnswerGrader` answers with whatever `assessment` holds and records every call on `calls`, so a
caller under test can be checked on what it handed the grader and on what it did with the verdict.
Set `error` to make the next call raise instead.

`assessment_at` builds a verdict holding one status on every dimension the criterion asks for, which
is what most callers need to drive their own branch.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from core.ai.evaluation.contracts import Assessment

from engram.evals.outcomes.domain.answers import AnswerAssessment, AnswerCriterion, AssessmentStatus, ClaimAssessment

if TYPE_CHECKING:
    from core.ai.evaluation.contracts import DeliveredEvidenceRef


def assessment_at(
    criterion: AnswerCriterion,
    status: AssessmentStatus,
    reason: str = "scripted by the fake grader",
    claims: tuple[ClaimAssessment, ...] = (),
) -> AnswerAssessment:
    """A verdict holding `status` on every dimension `criterion` asks for.

    Args:
        criterion: The criterion whose asked dimensions take `status`.
        status: The status every asked dimension carries.
        reason: The reason every assessment carries.
        claims: The claim assessments to attach.

    Returns:
        The scripted verdict, with the dimensions the criterion does not ask for `not_applicable`.
    """
    asked = criterion.required_dimensions
    graded = Assessment(status=status, reason=reason)
    skipped = Assessment(status="not_applicable", reason="the criterion does not ask for this dimension")

    def of(name: str) -> Assessment:
        return graded if name in asked else skipped

    return AnswerAssessment(
        overall=graded,
        correctness=of("correctness"),
        support=of("support"),
        coverage=of("coverage"),
        contradiction_handling=of("contradiction_handling"),
        abstention=of("abstention"),
        claims=claims,
    )


def claim_at(
    claim: str, status: AssessmentStatus, evidence: tuple[DeliveredEvidenceRef, ...] = (), *, critical: bool = True
) -> ClaimAssessment:
    """One claim assessment at `status`, citing `evidence`.

    Args:
        claim: The claim text, which must match the requirement it answers.
        status: The support status to record.
        evidence: The references the claim cites.
        critical: Whether the answer's usefulness rests on this claim.

    Returns:
        The claim assessment.
    """
    return ClaimAssessment(
        claim=claim, support=Assessment(status=status, reason=f"scripted {status}"), evidence=evidence, critical=critical
    )


@dataclass(frozen=True)
class GradeCall:
    """One call a caller made to the grader, exactly as it made it."""

    criterion: AnswerCriterion
    answer: str
    evidence: tuple[DeliveredEvidenceRef, ...]


@dataclass
class FakeAnswerGrader:
    """An `AnswerGrader` answering from public state and recording what it was asked.

    Attributes:
        assessment: The verdict to answer with, or `None` to pass every dimension the criterion asks for.
        error: Raised instead of answering, when set.
        calls: Every call made so far, in order.
    """

    assessment: AnswerAssessment | None = None
    error: Exception | None = None
    calls: list[GradeCall] = field(default_factory=list)

    async def __call__(self, criterion: AnswerCriterion, answer: str, evidence: tuple[DeliveredEvidenceRef, ...]) -> AnswerAssessment:
        self.calls.append(GradeCall(criterion=criterion, answer=answer, evidence=evidence))
        if self.error is not None:
            raise self.error
        return self.assessment if self.assessment is not None else assessment_at(criterion, "pass")
