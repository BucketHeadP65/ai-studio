"""Factorial experiments: hold one axis constant and vary the others.

An axis is the agent's model, its instructions, the dataset or the retrieval strategy.
`expand_variants` turns a set of `ExperimentAxes` into the concrete variants to run, in a stable order.
A variant always names the retrieval strategy its run exposes, so two strategies compare in one ordinary run.
"""

from __future__ import annotations

import itertools

from typing import Literal

from pydantic import BaseModel, ConfigDict, model_validator

from engram.evals.tuning.contracts import DEFAULT_STRATEGY, EVAL_STRATEGY_NAMES

ExperimentAxis = Literal["model", "instructions", "dataset", "retrieval_strategy"]
"""One setting an experiment can vary. `expand_variants` holds one axis at its first value and varies the others."""


class ExperimentAxes(BaseModel):
    """The values to try on each axis. Every axis carries at least one value.

    `retrieval_strategy` defaults to the default strategy alone.
    """

    model_config = ConfigDict(frozen=True)

    model: tuple[str, ...]
    instructions: tuple[str, ...]
    dataset: tuple[str, ...]
    retrieval_strategy: tuple[str, ...] = (DEFAULT_STRATEGY.value,)

    @model_validator(mode="after")
    def _all_axes_non_empty(self) -> ExperimentAxes:
        """Require every axis to carry at least one value, and every strategy to be a known one."""
        if not (self.model and self.instructions and self.dataset and self.retrieval_strategy):
            msg = "every experiment axis (model, instructions, dataset, retrieval_strategy) must have at least one value"
            raise ValueError(msg)
        unknown = tuple(name for name in self.retrieval_strategy if name not in EVAL_STRATEGY_NAMES)
        if unknown:
            msg = f"unknown retrieval strategy: {', '.join(unknown)} (expected one of {', '.join(EVAL_STRATEGY_NAMES)})"
            raise ValueError(msg)
        return self


class ExperimentVariant(BaseModel):
    """One point in the experiment space: one model, one set of instructions, one dataset and one strategy."""

    model_config = ConfigDict(frozen=True)

    model: str
    instructions: str
    dataset: str
    retrieval_strategy: str = DEFAULT_STRATEGY.value


class ExperimentRun(BaseModel):
    """One evaluated variant: the variant plus the aggregate retrieval metrics it scored."""

    model_config = ConfigDict(frozen=True)

    variant: ExperimentVariant
    metrics: dict[str, float]


def expand_variants(axes: ExperimentAxes, *, hold: ExperimentAxis) -> list[ExperimentVariant]:
    """List the variants of a factorial run that holds `hold` constant and varies the other axes.

    The held axis contributes only its first value. The other axes contribute every value, and the
    variants are every combination of them. Holding `model` over `instructions=(a, b)` and
    `dataset=(p, q)` with one strategy gives four variants.

    Args:
        axes: The values to try on each axis.
        hold: The axis to hold constant at its first value.

    Returns:
        The variants to evaluate, ordered by model, then instructions, then dataset, then retrieval strategy.
    """
    models = axes.model[:1] if hold == "model" else axes.model
    instructions = axes.instructions[:1] if hold == "instructions" else axes.instructions
    datasets = axes.dataset[:1] if hold == "dataset" else axes.dataset
    strategies = axes.retrieval_strategy[:1] if hold == "retrieval_strategy" else axes.retrieval_strategy
    return [
        ExperimentVariant(model=model, instructions=instruction, dataset=dataset, retrieval_strategy=strategy)
        for model, instruction, dataset, strategy in itertools.product(models, instructions, datasets, strategies)
    ]
