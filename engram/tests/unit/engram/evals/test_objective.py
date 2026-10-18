"""Unit specs for the tuning objective (`engram.evals.tuning.domain.objective`).

The `TuningObjective` is the loop's accept and convergence policy: a frozen config plus two
pure predicates over plain metric mappings (`is_improvement` accepts a candidate iff it beats the
incumbent on accuracy without busting the cost budget, `should_stop` decides when to halt). These
specs pin the contract: the accuracy gain and cost-budget boundaries, the absent-metric semantics
(missing primary metric is a contract violation, an absent cost metric leaves cost unconstrained),
each stop trigger, and the field constraints that turn an out-of-range config into a `ValidationError`.
"""

from __future__ import annotations

import pydantic
import pytest

from polyfactory.factories.pydantic_factory import ModelFactory

from engram.evals.tuning.domain.objective import StopDecision, StopReason, TuningObjective


class TuningObjectiveFactory(ModelFactory[TuningObjective]):
    """A `TuningObjective` with the shipped defaults (polyfactory's random floats bust the bounds)."""

    primary_metric = "recall@5"
    cost_metric = "total_tokens"
    cost_budget_pct = 0.10
    min_gain = 0.01
    max_rounds = 10
    patience = 3
    token_budget = None
    val_split = 0.3
    core_size = 100
    tail_size = 25
    min_core_per_stratum = 5
    bootstrap_resamples = 1000
    bootstrap_confidence = 0.95


class TestTuningObjectiveDefaults:
    def test_defaults_match_the_shipped_policy(self):
        objective = TuningObjective()

        assert objective.primary_metric == "recall@5"
        assert objective.cost_metric == "total_tokens"
        assert objective.cost_budget_pct == 0.10
        assert objective.min_gain == 0.01
        assert objective.max_rounds == 10
        assert objective.patience == 3
        assert objective.token_budget is None
        assert objective.val_split == 0.3

    def test_is_frozen(self):
        objective = TuningObjective()

        with pytest.raises(pydantic.ValidationError):
            objective.min_gain = 0.5

    def test_the_factory_builds_a_valid_instance(self):
        objective = TuningObjectiveFactory.build()

        assert objective.primary_metric == "recall@5"


class TestTuningObjectiveValidation:
    @pytest.mark.parametrize(
        ("field", "value"),
        [
            ("cost_budget_pct", -0.01),
            ("min_gain", -0.01),
            ("max_rounds", 0),
            ("patience", 0),
            ("token_budget", 0),
            ("token_budget", -1),
            ("val_split", 1.0),
            ("val_split", 1.5),
            ("val_split", -0.1),
        ],
    )
    def test_out_of_range_config_is_rejected(self, field: str, value: float):
        # The out-of-range value arrives as raw payload data, so validate it at the boundary.
        with pytest.raises(pydantic.ValidationError):
            TuningObjective.model_validate({field: value})

    @pytest.mark.parametrize("value", [0.0, 0.05, 1.0])
    def test_cost_budget_pct_accepts_non_negative(self, value: float):
        assert TuningObjective(cost_budget_pct=value).cost_budget_pct == value

    @pytest.mark.parametrize("value", [0.0, 0.5, 0.99])
    def test_val_split_accepts_zero_to_just_under_one(self, value: float):
        assert TuningObjective(val_split=value).val_split == value

    def test_token_budget_accepts_a_positive_cap(self):
        assert TuningObjective(token_budget=50_000).token_budget == 50_000


class TestIsImprovement:
    def test_accepts_when_gain_meets_min_and_cost_within_budget(self):
        objective = TuningObjective(min_gain=0.01, cost_budget_pct=0.10)
        candidate = {"recall@5": 0.62, "total_tokens": 1000.0}
        incumbent = {"recall@5": 0.60, "total_tokens": 1000.0}

        assert objective.is_improvement(candidate, incumbent) is True

    def test_accepts_at_the_exact_min_gain_boundary(self):
        objective = TuningObjective(min_gain=0.01, cost_budget_pct=0.10)
        candidate = {"recall@5": 0.61, "total_tokens": 1000.0}
        incumbent = {"recall@5": 0.60, "total_tokens": 1000.0}

        assert objective.is_improvement(candidate, incumbent) is True

    def test_rejects_just_below_the_min_gain_boundary(self):
        objective = TuningObjective(min_gain=0.01, cost_budget_pct=0.10)
        candidate = {"recall@5": 0.6099, "total_tokens": 1000.0}
        incumbent = {"recall@5": 0.60, "total_tokens": 1000.0}

        assert objective.is_improvement(candidate, incumbent) is False

    def test_rejects_an_accuracy_regression(self):
        objective = TuningObjective()
        candidate = {"recall@5": 0.55, "total_tokens": 900.0}
        incumbent = {"recall@5": 0.60, "total_tokens": 1000.0}

        assert objective.is_improvement(candidate, incumbent) is False

    def test_accepts_cost_at_the_exact_budget_boundary(self):
        objective = TuningObjective(min_gain=0.01, cost_budget_pct=0.10)
        candidate = {"recall@5": 0.70, "total_tokens": 1100.0}  # exactly 1000 * 1.10
        incumbent = {"recall@5": 0.60, "total_tokens": 1000.0}

        assert objective.is_improvement(candidate, incumbent) is True

    def test_rejects_cost_just_over_the_budget_boundary(self):
        objective = TuningObjective(min_gain=0.01, cost_budget_pct=0.10)
        candidate = {"recall@5": 0.70, "total_tokens": 1100.01}  # over 1000 * 1.10
        incumbent = {"recall@5": 0.60, "total_tokens": 1000.0}

        assert objective.is_improvement(candidate, incumbent) is False

    def test_cost_guardrail_skipped_when_cost_metric_absent_in_candidate(self):
        # Cost cannot penalize what was not measured, so accuracy alone decides.
        objective = TuningObjective(min_gain=0.01)
        candidate = {"recall@5": 0.62}
        incumbent = {"recall@5": 0.60, "total_tokens": 1.0}

        assert objective.is_improvement(candidate, incumbent) is True

    def test_cost_guardrail_skipped_when_cost_metric_absent_in_incumbent(self):
        objective = TuningObjective(min_gain=0.01)
        candidate = {"recall@5": 0.62, "total_tokens": 9_999_999.0}
        incumbent = {"recall@5": 0.60}

        assert objective.is_improvement(candidate, incumbent) is True

    def test_missing_primary_metric_in_candidate_raises(self):
        objective = TuningObjective()

        with pytest.raises(ValueError, match="recall@5"):
            objective.is_improvement({"total_tokens": 1.0}, {"recall@5": 0.6, "total_tokens": 1.0})

    def test_missing_primary_metric_in_incumbent_raises(self):
        objective = TuningObjective()

        with pytest.raises(ValueError, match="recall@5"):
            objective.is_improvement({"recall@5": 0.6, "total_tokens": 1.0}, {"total_tokens": 1.0})

    def test_honours_an_alternate_primary_metric(self):
        objective = TuningObjective(primary_metric="mrr", min_gain=0.05)
        candidate = {"mrr": 0.80, "total_tokens": 1.0}
        incumbent = {"mrr": 0.70, "total_tokens": 1.0}

        assert objective.is_improvement(candidate, incumbent) is True


class TestStopReason:
    def test_has_exactly_the_three_stop_reasons(self):
        assert {member.value for member in StopReason} == {"max_rounds", "patience", "token_budget"}

    @pytest.mark.parametrize("member", list(StopReason))
    def test_member_value_equals_its_lowercase_name(self, member: StopReason):
        assert member.value == member.name.lower()


class TestStopDecision:
    def test_carries_should_stop_and_reason(self):
        decision = StopDecision(should_stop=True, reason=StopReason.PATIENCE)

        assert decision.should_stop is True
        assert decision.reason is StopReason.PATIENCE

    def test_reason_defaults_to_none(self):
        assert StopDecision(should_stop=False).reason is None

    def test_is_frozen(self):
        decision = StopDecision(should_stop=False)

        with pytest.raises(pydantic.ValidationError):
            decision.should_stop = True


class TestShouldStop:
    def test_empty_trajectory_continues(self):
        objective = TuningObjective()

        decision = objective.should_stop([])

        assert decision == StopDecision(should_stop=False, reason=None)

    def test_stops_at_max_rounds(self):
        objective = TuningObjective(max_rounds=3, patience=10)
        # Three improving rounds (so patience never fires) at the max-rounds cap.
        trajectory = [{"recall@5": 0.1}, {"recall@5": 0.2}, {"recall@5": 0.3}]

        decision = objective.should_stop(trajectory)

        assert decision == StopDecision(should_stop=True, reason=StopReason.MAX_ROUNDS)

    def test_max_rounds_takes_precedence_over_token_budget(self):
        # Both triggers would fire; max-rounds is checked first.
        objective = TuningObjective(max_rounds=2, patience=10, token_budget=100)
        trajectory = [{"recall@5": 0.1}, {"recall@5": 0.2}]

        decision = objective.should_stop(trajectory, tokens_spent=999.0)

        assert decision.reason is StopReason.MAX_ROUNDS

    def test_stops_on_token_budget(self):
        objective = TuningObjective(max_rounds=10, patience=10, token_budget=1000)
        trajectory = [{"recall@5": 0.1}, {"recall@5": 0.2}]

        decision = objective.should_stop(trajectory, tokens_spent=1000.0)

        assert decision == StopDecision(should_stop=True, reason=StopReason.TOKEN_BUDGET)

    def test_does_not_stop_below_the_token_budget(self):
        objective = TuningObjective(max_rounds=10, patience=10, token_budget=1000)
        trajectory = [{"recall@5": 0.1}, {"recall@5": 0.2}]

        decision = objective.should_stop(trajectory, tokens_spent=999.0)

        assert decision.should_stop is False

    def test_token_budget_ignored_when_unset(self):
        objective = TuningObjective(max_rounds=10, patience=10, token_budget=None)
        trajectory = [{"recall@5": 0.1}, {"recall@5": 0.2}]

        decision = objective.should_stop(trajectory, tokens_spent=1_000_000.0)

        assert decision.should_stop is False

    def test_stops_on_patience_after_no_new_best(self):
        # Best is round 0; three later rounds fail to beat it -> patience=3 fires.
        objective = TuningObjective(max_rounds=10, patience=3)
        trajectory = [{"recall@5": 0.50}, {"recall@5": 0.40}, {"recall@5": 0.45}, {"recall@5": 0.30}]

        decision = objective.should_stop(trajectory)

        assert decision == StopDecision(should_stop=True, reason=StopReason.PATIENCE)

    def test_does_not_stop_when_a_new_best_is_within_patience(self):
        # Best is the latest round, so the stale-streak is 0.
        objective = TuningObjective(max_rounds=10, patience=3)
        trajectory = [{"recall@5": 0.40}, {"recall@5": 0.45}, {"recall@5": 0.50}]

        decision = objective.should_stop(trajectory)

        assert decision.should_stop is False

    def test_patience_uses_the_first_index_on_a_tie(self):
        # Two rounds tie for the max; the earlier wins, lengthening the stale streak to fire patience.
        objective = TuningObjective(max_rounds=10, patience=2)
        trajectory = [{"recall@5": 0.50}, {"recall@5": 0.50}, {"recall@5": 0.30}]

        decision = objective.should_stop(trajectory)

        assert decision == StopDecision(should_stop=True, reason=StopReason.PATIENCE)

    def test_continues_when_no_trigger_fires(self):
        objective = TuningObjective(max_rounds=10, patience=3)
        trajectory = [{"recall@5": 0.40}, {"recall@5": 0.45}]

        decision = objective.should_stop(trajectory)

        assert decision == StopDecision(should_stop=False, reason=None)

    def test_a_round_missing_the_primary_metric_raises(self):
        objective = TuningObjective(max_rounds=10, patience=3)
        trajectory = [{"recall@5": 0.40}, {"precision@5": 0.9}]

        with pytest.raises(ValueError, match="recall@5"):
            objective.should_stop(trajectory)

    def test_honours_an_alternate_primary_metric_for_patience(self):
        objective = TuningObjective(primary_metric="mrr", max_rounds=10, patience=2)
        trajectory = [{"mrr": 0.80}, {"mrr": 0.50}, {"mrr": 0.40}]

        decision = objective.should_stop(trajectory)

        assert decision == StopDecision(should_stop=True, reason=StopReason.PATIENCE)


class TestFrozenBeat:
    """`frozen_beat` reuses the accept guard's paired arithmetic with cost unconstrained."""

    def test_a_uniformly_better_winner_beats(self):
        objective = TuningObjective()

        assert objective.frozen_beat({"c1": 0.8, "c2": 0.7, "c3": 0.9}, {"c1": 0.5, "c2": 0.5, "c3": 0.6}) is True

    def test_an_identical_winner_does_not_beat(self):
        # All-zero deltas: the bootstrap lower bound is exactly zero, no evidence is not improvement.
        objective = TuningObjective()
        scores = {"c1": 0.5, "c2": 0.5}

        assert objective.frozen_beat(scores, dict(scores)) is False

    def test_disjoint_case_names_do_not_beat(self):
        objective = TuningObjective()

        assert objective.frozen_beat({"a": 1.0}, {"b": 0.0}) is False

    def test_cost_never_constrains_the_frozen_verdict(self):
        # The tightest cost budget cannot refuse a frozen beat: the gate passes None on both sides.
        objective = TuningObjective(cost_budget_pct=0.0)

        assert objective.frozen_beat({"c1": 0.9, "c2": 0.9}, {"c1": 0.4, "c2": 0.4}) is True
