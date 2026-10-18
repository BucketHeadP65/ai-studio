"""Unit specs for the paired-bootstrap accept guard (`engram.evals.tuning.domain.objective`).

The accept rule promotes a candidate only when its per-case mean gain clears `min_gain`, a
percentile bootstrap over the per-case
(candidate minus incumbent) deltas keeps its confidence lower bound strictly above zero, and the
reused cost guardrail passes. These specs pin the bootstrap's determinism per seed, its all-zero and
single-element behaviour, the boundary accept and reject on known delta vectors, and that `accepts`
folds the mean-gain, bootstrap, and cost checks into one `AcceptDecision`.
"""

from __future__ import annotations

import pydantic
import pytest

from core.statistics import paired_bootstrap_lower_bound

from engram.evals.tuning.domain.objective import AcceptDecision, TuningObjective


class TestPairedBootstrapLowerBound:
    def test_is_deterministic_for_a_fixed_seed(self):
        deltas = [0.1, -0.05, 0.2, 0.0, 0.15, -0.1, 0.3, 0.05]

        first = paired_bootstrap_lower_bound(deltas, n_resamples=500, confidence=0.95, seed=7)
        second = paired_bootstrap_lower_bound(deltas, n_resamples=500, confidence=0.95, seed=7)

        assert first == second

    def test_identical_positive_deltas_pin_the_bound_to_that_value(self):
        # Every resample of a constant vector has the same mean, so the lower bound equals it exactly.
        bound = paired_bootstrap_lower_bound([0.2] * 10, n_resamples=300, confidence=0.95, seed=1)

        assert bound == pytest.approx(0.2)

    def test_all_zero_deltas_give_a_zero_bound(self):
        bound = paired_bootstrap_lower_bound([0.0] * 12, n_resamples=300, confidence=0.95, seed=2)

        assert bound == pytest.approx(0.0)

    def test_all_positive_deltas_keep_the_bound_positive(self):
        deltas = [0.05, 0.1, 0.12, 0.2, 0.08, 0.15, 0.09, 0.11]

        bound = paired_bootstrap_lower_bound(deltas, n_resamples=1000, confidence=0.95, seed=3)

        assert bound > 0.0

    def test_symmetric_mixed_deltas_drop_the_bound_below_zero(self):
        # A zero-mean, high-variance vector: the lower tail of resampled means is clearly negative.
        deltas = [1.0, -1.0, 1.0, -1.0, 1.0, -1.0]

        bound = paired_bootstrap_lower_bound(deltas, n_resamples=1000, confidence=0.95, seed=4)

        assert bound < 0.0

    def test_single_delta_returns_that_delta(self):
        assert paired_bootstrap_lower_bound([0.3], n_resamples=100, confidence=0.95, seed=5) == pytest.approx(0.3)

    def test_empty_deltas_raise(self):
        with pytest.raises(ValueError, match="delta"):
            paired_bootstrap_lower_bound([], n_resamples=100, confidence=0.95, seed=6)

    def test_a_wider_interval_lowers_the_bound(self):
        # A lower confidence keeps more of the distribution, so the (1-confidence)/2 quantile rises;
        # a higher confidence pushes the lower bound further into the left tail (lower or equal).
        deltas = [0.05, 0.1, 0.12, 0.2, 0.08, 0.15, 0.09, 0.11]

        tight = paired_bootstrap_lower_bound(deltas, n_resamples=1000, confidence=0.99, seed=8)
        loose = paired_bootstrap_lower_bound(deltas, n_resamples=1000, confidence=0.80, seed=8)

        assert tight <= loose


class TestAcceptDecision:
    def test_carries_the_three_fields(self):
        decision = AcceptDecision(accepted=True, mean_gain=0.05, ci_lower=0.01)

        assert decision.accepted is True
        assert decision.mean_gain == pytest.approx(0.05)
        assert decision.ci_lower == pytest.approx(0.01)

    def test_is_frozen(self):
        decision = AcceptDecision(accepted=False, mean_gain=0.0, ci_lower=0.0)

        with pytest.raises(pydantic.ValidationError):
            decision.accepted = True


class TestTuningObjectiveSamplingFields:
    def test_defaults_match_the_contract(self):
        objective = TuningObjective()

        assert objective.core_size == 100
        assert objective.tail_size == 25
        assert objective.min_core_per_stratum == 5
        assert objective.bootstrap_resamples == 1000
        assert objective.bootstrap_confidence == pytest.approx(0.95)

    @pytest.mark.parametrize(
        ("field", "value"),
        [
            ("bootstrap_resamples", 99),
            ("bootstrap_confidence", 0.0),
            ("bootstrap_confidence", 1.0),
            ("bootstrap_confidence", 1.5),
            ("bootstrap_confidence", -0.1),
        ],
    )
    def test_out_of_range_sampling_knobs_are_rejected(self, field: str, value: float):
        with pytest.raises(pydantic.ValidationError):
            TuningObjective.model_validate({field: value})

    def test_bootstrap_resamples_accepts_the_lower_bound(self):
        assert TuningObjective(bootstrap_resamples=100).bootstrap_resamples == 100


class TestAccepts:
    def test_accepts_consistent_positive_gains(self):
        objective = TuningObjective(min_gain=0.01, bootstrap_resamples=500)
        candidate = {"a": 0.70, "b": 0.85, "c": 0.72}
        incumbent = {"a": 0.50, "b": 0.60, "c": 0.55}

        decision = objective.accepts(candidate, incumbent, candidate_cost=None, incumbent_cost=None)

        assert decision.accepted is True
        assert decision.mean_gain > 0.0
        assert decision.ci_lower > 0.0

    def test_rejects_all_zero_deltas(self):
        # No case moved: the mean gain is zero and the bootstrap bound is zero, so there is no evidence.
        objective = TuningObjective(min_gain=0.0, bootstrap_resamples=300)
        cases = {"a": 0.5, "b": 0.6, "c": 0.7}

        decision = objective.accepts(cases, dict(cases), candidate_cost=None, incumbent_cost=None)

        assert decision.accepted is False
        assert decision.mean_gain == pytest.approx(0.0)
        assert decision.ci_lower == pytest.approx(0.0)

    def test_rejects_an_empty_pairing(self):
        objective = TuningObjective(bootstrap_resamples=300)

        decision = objective.accepts({"a": 0.9}, {"b": 0.1}, candidate_cost=None, incumbent_cost=None)

        assert decision == AcceptDecision(accepted=False, mean_gain=0.0, ci_lower=0.0)

    def test_rejects_a_positive_mean_with_a_non_positive_bound(self):
        # Mean gain 0.05 clears min_gain, but the deltas are too noisy for the bootstrap bound to hold.
        objective = TuningObjective(min_gain=0.01, bootstrap_resamples=1000)
        candidate = {"a": 1.0, "b": 0.0}
        incumbent = {"a": 0.0, "b": 0.9}

        decision = objective.accepts(candidate, incumbent, candidate_cost=None, incumbent_cost=None)

        assert decision.mean_gain == pytest.approx(0.05)
        assert decision.ci_lower <= 0.0
        assert decision.accepted is False

    def test_cost_guardrail_rejects_a_budget_bust(self):
        objective = TuningObjective(min_gain=0.01, cost_budget_pct=0.10, bootstrap_resamples=500)
        candidate = {"a": 0.70, "b": 0.85, "c": 0.72}
        incumbent = {"a": 0.50, "b": 0.60, "c": 0.55}

        decision = objective.accepts(candidate, incumbent, candidate_cost=2000.0, incumbent_cost=1000.0)

        assert decision.accepted is False

    def test_cost_guardrail_accepts_within_budget(self):
        objective = TuningObjective(min_gain=0.01, cost_budget_pct=0.10, bootstrap_resamples=500)
        candidate = {"a": 0.70, "b": 0.85, "c": 0.72}
        incumbent = {"a": 0.50, "b": 0.60, "c": 0.55}

        decision = objective.accepts(candidate, incumbent, candidate_cost=1100.0, incumbent_cost=1000.0)

        assert decision.accepted is True

    def test_cost_guardrail_skipped_when_costs_are_none(self):
        objective = TuningObjective(min_gain=0.01, bootstrap_resamples=500)
        candidate = {"a": 0.70, "b": 0.85, "c": 0.72}
        incumbent = {"a": 0.50, "b": 0.60, "c": 0.55}

        decision = objective.accepts(candidate, incumbent, candidate_cost=9_999_999.0, incumbent_cost=None)

        assert decision.accepted is True

    def test_pairs_only_on_shared_keys(self):
        objective = TuningObjective(min_gain=0.01, bootstrap_resamples=500)
        candidate = {"a": 0.70, "b": 0.80, "extra_candidate": 0.99}
        incumbent = {"a": 0.50, "b": 0.60, "extra_incumbent": 0.01}

        decision = objective.accepts(candidate, incumbent, candidate_cost=None, incumbent_cost=None)

        # Only a and b pair: mean of (0.20, 0.20) is 0.20; the unmatched extras never enter the mean.
        assert decision.mean_gain == pytest.approx(0.20)
        assert decision.accepted is True

    def test_is_deterministic_across_calls(self):
        objective = TuningObjective(min_gain=0.01, bootstrap_resamples=500)
        candidate = {"a": 0.70, "b": 0.85, "c": 0.72}
        incumbent = {"a": 0.50, "b": 0.60, "c": 0.55}

        first = objective.accepts(candidate, incumbent, candidate_cost=None, incumbent_cost=None)
        second = objective.accepts(candidate, incumbent, candidate_cost=None, incumbent_cost=None)

        assert first == second


class TestPrimaryMetricValidation:
    def test_the_default_primary_metric_is_a_known_key(self):
        assert TuningObjective().primary_metric == "recall@5"

    @pytest.mark.parametrize("primary_metric", ["mrr", "precision@1", "citation_recall@5", "surfaced_mrr", "ranking_precision@10"])
    def test_a_known_metric_key_constructs_cleanly(self, primary_metric: str):
        assert TuningObjective(primary_metric=primary_metric).primary_metric == primary_metric

    @pytest.mark.parametrize("primary_metric", ["recallat5", "citation_recall", "bogus_metric", "recall@0"])
    def test_an_unknown_metric_key_is_refused_at_construction(self, primary_metric: str):
        with pytest.raises(pydantic.ValidationError):
            TuningObjective(primary_metric=primary_metric)
