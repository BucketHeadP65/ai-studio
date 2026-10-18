"""Learning records: the evidence about one recurring problem and every accepted or rejected improvement attempt."""

from __future__ import annotations

from typing import Literal, Self

from pydantic import model_validator

from core.ai.evaluation.domain.models import EvaluationModel, Nonempty, Provenance, require_unique
from core.ai.evaluation.domain.outcomes import ComparisonReport, TaskExecution, assess_comparison
from core.fingerprints import content_fingerprint, model_fingerprint


class LearningKey(EvaluationModel):
    """The concrete recurring problem and the repair surface evidence concerns."""

    problem: Nonempty
    surface: Nonempty

    @property
    def fingerprint(self) -> str:
        """Stable identity independent of evidence or evaluation history."""
        return model_fingerprint(self)


class LearningEvidence(EvaluationModel):
    """One traceable observation, including counterexamples and successful alternatives."""

    evidence_id: Nonempty
    source: Nonempty
    observation: Nonempty
    stance: Literal["supporting", "contradicting", "successful_alternative"]
    provenance: Provenance


class ImprovementProposal(EvaluationModel):
    """Exact proposed artifact content. A changed proposal receives a different fingerprint."""

    description: Nonempty
    content: Nonempty

    @property
    def fingerprint(self) -> str:
        """SHA-256 of the exact proposed content, matching the evaluated implementation."""
        return content_fingerprint(self.content)


class LearningAttempt[ExecutionT: TaskExecution](EvaluationModel):
    """A proposal and all paired evidence supporting its accepted or rejected verdict."""

    attempt_id: Nonempty
    proposal: ImprovementProposal
    evaluation: ComparisonReport[ExecutionT]
    rationale: Nonempty

    @model_validator(mode="after")
    def evaluated_proposal(self) -> Self:
        """Refuse mismatched candidate content and verdicts inconsistent with trial evidence.

        Returns:
            The attempt, unchanged.

        Raises:
            ValueError: The proposal fingerprint or the recorded verdict disagrees with the trials.
        """
        if self.proposal.fingerprint != self.evaluation.candidate.implementation_fingerprint:
            msg = "proposal fingerprint must match the evaluated candidate implementation fingerprint"
            raise ValueError(msg)
        expected = assess_comparison(
            self.evaluation.plan, baseline=self.evaluation.baseline, candidate=self.evaluation.candidate, trials=self.evaluation.trials
        )
        if expected != self.evaluation:
            msg = "evaluation verdict or metrics differ from the recorded trial evidence"
            raise ValueError(msg)
        return self


class LearningRecord[ExecutionT: TaskExecution](EvaluationModel):
    """An accumulating evidence record. An accepted result never erases a rejected attempt."""

    key: LearningKey
    evidence: tuple[LearningEvidence, ...] = ()
    attempts: tuple[LearningAttempt[ExecutionT], ...] = ()

    @model_validator(mode="after")
    def unique_history(self) -> Self:
        """Every historical identifier names one immutable observation or attempt.

        Returns:
            The record, unchanged.

        Raises:
            ValueError: An evidence id or an attempt id appears twice.
        """
        require_unique((row.evidence_id for row in self.evidence), "evidence ids")
        require_unique((row.attempt_id for row in self.attempts), "attempt ids")
        return self

    def add_evidence(self, evidence: LearningEvidence) -> LearningRecord[ExecutionT]:
        """Append a new observation. Repeating an identical observation changes nothing.

        Args:
            evidence: The observation to add.

        Returns:
            The record with the observation.

        Raises:
            ValueError: The evidence id already names a different observation.
        """
        for existing in self.evidence:
            if existing.evidence_id == evidence.evidence_id:
                if existing != evidence:
                    msg = f"evidence {evidence.evidence_id!r} is immutable"
                    raise ValueError(msg)
                return self
        return type(self)(key=self.key, evidence=(*self.evidence, evidence), attempts=self.attempts)

    def add_attempt(self, attempt: LearningAttempt[ExecutionT]) -> LearningRecord[ExecutionT]:
        """Append an evaluation without rewriting prior proposals or rejected results.

        Args:
            attempt: The attempt to add.

        Returns:
            The record with the attempt.

        Raises:
            ValueError: The attempt id already names a different attempt.
        """
        for existing in self.attempts:
            if existing.attempt_id == attempt.attempt_id:
                if existing != attempt:
                    msg = f"attempt {attempt.attempt_id!r} is immutable"
                    raise ValueError(msg)
                return self
        return type(self)(key=self.key, evidence=self.evidence, attempts=(*self.attempts, attempt))
