"""The ports the tuning loop depends on: the evaluator that measures a policy and the proposer that revises it."""

from __future__ import annotations

from typing import TYPE_CHECKING, Protocol

if TYPE_CHECKING:
    from engram.evals.generation.contracts import EvaluationDataset
    from engram.evals.rca.contracts import Diagnosis, ReflectiveFinding, ReportLike
    from engram.evals.tuning.domain.proposal import ProposedPolicy
    from engram.evals.tuning.policy import RetrievalPolicy


class PolicyEvaluator(Protocol):
    """Scores one retrieval policy against a dataset into a report that `diagnose` can read."""

    async def evaluate(self, policy: RetrievalPolicy, *, dataset: EvaluationDataset) -> ReportLike:
        """Score `policy` over every case of `dataset`.

        The report must hold one case per dataset case, under the same name, because the loop pairs
        scores by case name.

        Args:
            policy: The retrieval policy the agent under test runs with.
            dataset: The cases to score.

        Returns:
            The report of the run, which `diagnose` folds into a diagnosis.
        """
        ...


class PolicyProposer(Protocol):
    """Proposes one targeted revision of a retrieval policy from a diagnosis."""

    async def propose(
        self, policy: RetrievalPolicy, diagnosis: Diagnosis, *, findings: tuple[ReflectiveFinding, ...] = ()
    ) -> ProposedPolicy:
        """Propose one targeted revision to `policy` from `diagnosis`, with a rationale.

        The findings only inform the proposal and the proposer may ignore them. The loop's measurements
        alone decide whether the revision is accepted.

        Args:
            policy: The current policy to revise.
            diagnosis: The diagnosis of where `policy` is weak.
            findings: The round's gated reflective findings. Empty when no reflector is bound.

        Returns:
            The full revised policy and the rationale for its one change.
        """
        ...
