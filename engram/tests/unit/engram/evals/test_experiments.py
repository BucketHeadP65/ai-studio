"""Unit tests for factorial eval experiments."""

from __future__ import annotations

import pydantic
import pytest

from engram.evals.harness.contracts import ExperimentAxes, ExperimentRun, ExperimentVariant, expand_variants, run_experiment
from engram.evals.tuning.strategy import DEFAULT_STRATEGY, RetrievalStrategy


class TestExpandVariants:
    def test_hold_model_holds_baseline_and_varies_the_rest(self):
        axes = ExperimentAxes(model=("a", "b"), instructions=("x",), dataset=("p", "q"))

        variants = expand_variants(axes, hold="model")

        assert len(variants) == 2  # baseline model "a" × instructions {x} × dataset {p, q}
        assert all(variant.model == "a" for variant in variants)
        assert {variant.dataset for variant in variants} == {"p", "q"}

    def test_hold_dataset_varies_model_and_instructions(self):
        axes = ExperimentAxes(model=("a", "b"), instructions=("x", "y"), dataset=("p", "q"))

        variants = expand_variants(axes, hold="dataset")

        assert len(variants) == 4  # {a, b} × {x, y} × baseline dataset "p"
        assert all(variant.dataset == "p" for variant in variants)

    def test_hold_instructions_pins_the_first_instruction(self):
        axes = ExperimentAxes(model=("a",), instructions=("x", "y"), dataset=("p",))

        variants = expand_variants(axes, hold="instructions")

        assert len(variants) == 1
        assert variants[0].instructions == "x"


class TestExperimentAxes:
    def test_an_empty_axis_is_rejected(self):
        with pytest.raises(pydantic.ValidationError, match="at least one value"):
            ExperimentAxes(model=(), instructions=("x",), dataset=("p",))

    def test_an_empty_strategy_axis_is_rejected(self):
        with pytest.raises(pydantic.ValidationError, match="at least one value"):
            ExperimentAxes(model=("a",), instructions=("x",), dataset=("p",), retrieval_strategy=())

    def test_the_strategy_axis_defaults_to_the_default_strategy(self):
        axes = ExperimentAxes(model=("a",), instructions=("x",), dataset=("p",))

        assert axes.retrieval_strategy == (DEFAULT_STRATEGY.value,)

    def test_an_unknown_strategy_is_rejected(self):
        with pytest.raises(pydantic.ValidationError, match="unknown retrieval strategy"):
            ExperimentAxes(model=("a",), instructions=("x",), dataset=("p",), retrieval_strategy=("bogus",))

    def test_code_mode_is_not_offered_as_an_axis_value(self):
        # `code-mode` resolves to the same tools as `full` for a toolset builder, so offering it as
        # an axis value would present a choice that changes nothing measurable.
        with pytest.raises(pydantic.ValidationError, match="unknown retrieval strategy"):
            ExperimentAxes(model=("a",), instructions=("x",), dataset=("p",), retrieval_strategy=(RetrievalStrategy.CODE_MODE.value,))


class TestStrategyAxis:
    def test_the_variant_defaults_to_the_default_strategy(self):
        assert ExperimentVariant(model="a", instructions="x", dataset="p").retrieval_strategy == DEFAULT_STRATEGY.value

    def test_holding_the_strategy_pins_the_first_one(self):
        axes = ExperimentAxes(
            model=("a", "b"),
            instructions=("x",),
            dataset=("p",),
            retrieval_strategy=(RetrievalStrategy.SEARCH_HYBRID.value, RetrievalStrategy.FULL.value),
        )

        variants = expand_variants(axes, hold="retrieval_strategy")

        assert len(variants) == 2  # {a, b} x baseline strategy "search-hybrid"
        assert all(variant.retrieval_strategy == RetrievalStrategy.SEARCH_HYBRID.value for variant in variants)

    def test_holding_the_model_varies_the_strategy(self):
        axes = ExperimentAxes(
            model=("a", "b"),
            instructions=("x",),
            dataset=("p",),
            retrieval_strategy=(RetrievalStrategy.SEARCH_HYBRID.value, RetrievalStrategy.FULL.value),
        )

        variants = expand_variants(axes, hold="model")

        assert len(variants) == 2  # baseline model "a" x {search-hybrid, full}
        assert [variant.retrieval_strategy for variant in variants] == [RetrievalStrategy.SEARCH_HYBRID.value, RetrievalStrategy.FULL.value]


class TestRunExperiment:
    async def test_runs_every_variant_through_the_evaluator(self):
        axes = ExperimentAxes(model=("a", "b"), instructions=("x",), dataset=("p", "q"))
        seen: list[ExperimentVariant] = []

        async def evaluate(variant: ExperimentVariant) -> dict[str, float]:
            seen.append(variant)
            return {"recall@5": 1.0}

        runs = await run_experiment(axes, hold="model", evaluate=evaluate)

        assert len(runs) == 2
        assert all(isinstance(run, ExperimentRun) for run in runs)
        assert all(run.metrics == {"recall@5": 1.0} for run in runs)
        assert len(seen) == 2
