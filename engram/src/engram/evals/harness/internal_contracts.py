"""The ports the evaluation harness depends on."""

from __future__ import annotations

from typing import TYPE_CHECKING, Protocol

if TYPE_CHECKING:
    from collections.abc import Mapping

    from engram.evals.harness.domain.experiments import ExperimentVariant


class VariantEvaluator(Protocol):
    """Evaluates one experiment variant and returns its aggregate metrics."""

    async def __call__(self, variant: ExperimentVariant) -> Mapping[str, float]:
        """Run the variant's evaluation on its dataset and return its aggregate metrics.

        Args:
            variant: The model, instructions, dataset and retrieval strategy to evaluate.

        Returns:
            The mean score of each metric, such as `recall@5` or `mrr`, keyed by metric name.
        """
        ...
