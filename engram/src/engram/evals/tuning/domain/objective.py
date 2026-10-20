"""The tuning loop's accept and convergence rules.

`TuningObjective` holds both per-round decisions, whether to accept a candidate and whether to stop the loop,
as pure predicates over plain metric mappings. It does no I/O.
"""

from __future__ import annotations

import hashlib

from enum import StrEnum
from typing import TYPE_CHECKING, Self

from core.statistics import paired_bootstrap_lower_bound
from pydantic import BaseModel, ConfigDict, Field, model_validator

from engram.evals.shared.contracts import is_known_metric_key

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence


# The byte width of the sha256 slice the bootstrap seed folds into an integer.
_RESAMPLE_INDEX_BYTES = 8


class AcceptDecision(BaseModel):
    """The accept verdict for one candidate: whether it wins and the two signals that decided it.

    Attributes:
        accepted: `True` when the candidate cleared the mean-gain floor, the bootstrap lower bound,
            and the cost guardrail.
        mean_gain: The mean of the per-case (candidate minus incumbent) deltas on the paired cases.
        ci_lower: The bootstrap confidence-interval lower bound of that mean.
    """

    model_config = ConfigDict(frozen=True)

    accepted: bool
    mean_gain: float
    ci_lower: float


class StopReason(StrEnum):
    """The convergence trigger that halted the loop, each value is its lowercased member name.

    Members:
        MAX_ROUNDS: The round budget was exhausted.
        PATIENCE: No round improved on the running best for `patience` rounds.
        TOKEN_BUDGET: The cumulative token spend reached the hard cap.
    """

    MAX_ROUNDS = "max_rounds"
    PATIENCE = "patience"
    TOKEN_BUDGET = "token_budget"


class StopDecision(BaseModel):
    """The convergence verdict for one round: whether to stop and the trigger that fired.

    Attributes:
        should_stop: `True` when the loop should halt this round.
        reason: The trigger that fired, or `None` when the loop continues.
    """

    model_config = ConfigDict(frozen=True)

    should_stop: bool
    reason: StopReason | None = None


class TuningObjective(BaseModel):
    """The tuning loop's accept and convergence rules as a frozen config.

    A numeric knob outside its bounds fails at construction.

    Attributes:
        primary_metric: The accuracy metric to maximize, a key naming its intended layer: the
            unprefixed citation keys (`recall@{k}`, `precision@{k}`, `mrr`) or a layer-namespaced key
            (`citation_` / `surfaced_` / `ranking_` `recall@{k}` / `precision@{k}` / `mrr`), validated
            against `engram.evals.shared.metrics.is_known_metric_key` at construction.
        cost_metric: The key under which the loop stores a policy's mean total tokens per case, the cost
            the accept rules guard against regression.
        cost_budget_pct: The fraction by which a candidate's cost may exceed the incumbent's.
        min_gain: The minimum accuracy improvement that counts as better.
        max_rounds: The hard cap on tuning rounds.
        patience: Stop after this many rounds with no new best.
        token_budget: An optional hard cap on cumulative token spend (`None` disables it).
        val_split: The validation hold-out fraction of the loop's stratified train and validation split
            (`0` disables the split).
        core_size: The target size of the decision core, the fixed stratified subset every accept
            decision is measured on.
        tail_size: The size of the rotating tail drawn fresh each round for `diagnose` and overfit
            detection, never for acceptance.
        min_core_per_stratum: The floor below which a core stratum folds into its parent difficulty
            band and is flagged rather than trusted.
        bootstrap_resamples: The number of resamples the paired-bootstrap accept guard draws.
        bootstrap_confidence: The two-sided confidence level of that bootstrap interval.
        apply_staleness_max: The freshness check's threshold: `tune --apply-runtime` refuses when the
            dataset or frozen-split stale fraction exceeds it.
    """

    model_config = ConfigDict(frozen=True)

    primary_metric: str = "recall@5"
    cost_metric: str = "total_tokens"
    cost_budget_pct: float = Field(default=0.10, ge=0)
    min_gain: float = Field(default=0.01, ge=0)
    max_rounds: int = Field(default=10, ge=1)
    patience: int = Field(default=3, ge=1)
    token_budget: int | None = Field(default=None, gt=0)
    val_split: float = Field(default=0.3, ge=0, lt=1)
    core_size: int = 100
    tail_size: int = 25
    min_core_per_stratum: int = 5
    bootstrap_resamples: int = Field(default=1000, ge=100)
    bootstrap_confidence: float = Field(default=0.95, gt=0, lt=1)
    apply_staleness_max: float = Field(default=0.0, ge=0, le=1)

    @model_validator(mode="after")
    def primary_metric_names_a_known_key(self) -> Self:
        """Refuse a `primary_metric` that names no metric key this project's evaluators can emit.

        Returns:
            The objective itself, unchanged.

        Raises:
            ValueError: `primary_metric` matches neither an unprefixed citation key nor a layer-namespaced
                key.
        """
        if not is_known_metric_key(self.primary_metric):
            msg = f"primary_metric {self.primary_metric!r} names no known metric key (unprefixed or layer-namespaced recall@k / precision@k / mrr)"
            raise ValueError(msg)
        return self

    def is_improvement(self, candidate: Mapping[str, float], incumbent: Mapping[str, float]) -> bool:
        """Report whether the candidate beats the incumbent on accuracy without busting the cost budget.

        The candidate wins iff its accuracy gain on `primary_metric` is at least `min_gain` and its
        `cost_metric` stays within `cost_budget_pct` of the incumbent's. When `cost_metric` is absent
        from either mapping, the cost guardrail is skipped and accuracy alone decides.

        Args:
            candidate: The candidate run's metrics, carrying at least `primary_metric`.
            incumbent: The incumbent run's metrics, carrying at least `primary_metric`.

        Returns:
            `True` when the candidate clears the accuracy gain and the cost guardrail.

        Raises:
            ValueError: When `primary_metric` is absent from either mapping.
        """
        if self.primary_metric not in candidate or self.primary_metric not in incumbent:
            msg = f"primary metric {self.primary_metric!r} is missing from the candidate or incumbent metrics"
            raise ValueError(msg)

        accuracy_gain = candidate[self.primary_metric] - incumbent[self.primary_metric]
        cost_ok = self._cost_within_budget(candidate.get(self.cost_metric), incumbent.get(self.cost_metric))

        return accuracy_gain >= self.min_gain and cost_ok

    def accepts(
        self,
        candidate_cases: Mapping[str, float],
        incumbent_cases: Mapping[str, float],
        candidate_cost: float | None,
        incumbent_cost: float | None,
    ) -> AcceptDecision:
        """Decide whether a candidate wins on the decision core, guarding the mean gain with a paired bootstrap.

        The primary-metric score of each case is paired on the shared case names. The candidate is accepted
        iff its mean per-case gain clears `min_gain`, the paired-bootstrap confidence lower bound over those
        deltas stays strictly above zero, and the cost stays within budget. An all-zero delta vector and an
        empty pairing are both rejected. A `None` cost on either side leaves cost unconstrained. The bootstrap
        seed derives from the paired case names, so the same inputs always reach the same verdict.

        Args:
            candidate_cases: The candidate run's primary-metric score per case name.
            incumbent_cases: The incumbent run's primary-metric score per case name.
            candidate_cost: The candidate's cost metric, or `None` when it was not measured.
            incumbent_cost: The incumbent's cost metric, or `None` when it was not measured.

        Returns:
            An `AcceptDecision` carrying the verdict, the mean gain, and the bootstrap lower bound.
        """
        shared = sorted(candidate_cases.keys() & incumbent_cases.keys())
        if not shared:
            return AcceptDecision(accepted=False, mean_gain=0.0, ci_lower=0.0)

        deltas = [candidate_cases[name] - incumbent_cases[name] for name in shared]
        mean_gain = sum(deltas) / len(deltas)
        seed = int.from_bytes(hashlib.sha256("\x00".join(shared).encode()).digest()[:_RESAMPLE_INDEX_BYTES], "big")
        ci_lower = paired_bootstrap_lower_bound(
            deltas, n_resamples=self.bootstrap_resamples, confidence=self.bootstrap_confidence, seed=seed
        )
        cost_ok = self._cost_within_budget(candidate_cost, incumbent_cost)

        accepted = mean_gain >= self.min_gain and ci_lower > 0.0 and cost_ok
        return AcceptDecision(accepted=accepted, mean_gain=mean_gain, ci_lower=ci_lower)

    def frozen_beat(self, winner_cases: Mapping[str, float], incumbent_cases: Mapping[str, float]) -> bool:
        """Whether the winner beats the incumbent on the frozen yardstick (the machine-rung gate).

        It applies the `accepts` rule with the cost unconstrained: the winner beats the incumbent iff the
        mean per-case gain clears `min_gain` and the paired-bootstrap lower bound stays strictly above zero.
        An identical winner or an empty pairing does not beat, so a baseline-only run never arms the
        machine rung.

        Args:
            winner_cases: The winner's primary-metric score per frozen case name.
            incumbent_cases: The incumbent's primary-metric score per frozen case name.

        Returns:
            `True` when the winner beats the incumbent on the frozen split.
        """
        return self.accepts(winner_cases, incumbent_cases, None, None).accepted

    def _cost_within_budget(self, candidate_cost: float | None, incumbent_cost: float | None) -> bool:
        """Whether a candidate cost stays within `cost_budget_pct` of the incumbent's, a `None` on either side passing."""
        if candidate_cost is None or incumbent_cost is None:
            return True
        return candidate_cost <= incumbent_cost * (1 + self.cost_budget_pct)

    def should_stop(self, trajectory: Sequence[Mapping[str, float]], *, tokens_spent: float = 0.0) -> StopDecision:
        """Decide whether the tuning loop has converged, naming the trigger that fired.

        The triggers are checked in order, the round cap first, then the optional cumulative-token
        cap, then a patience streak with no new best. An empty trajectory always continues.

        Args:
            trajectory: The per-round metric mappings so far, each carrying at least `primary_metric`.
            tokens_spent: The cumulative token spend across the rounds run so far.

        Returns:
            A `StopDecision` whose `reason` names the trigger when stopping, or
            `StopDecision(should_stop=False, reason=None)` when the loop continues.

        Raises:
            ValueError: When a trajectory round lacks `primary_metric`.
        """
        if not trajectory:
            return StopDecision(should_stop=False, reason=None)

        if len(trajectory) >= self.max_rounds:
            return StopDecision(should_stop=True, reason=StopReason.MAX_ROUNDS)

        if self.token_budget is not None and tokens_spent >= self.token_budget:
            return StopDecision(should_stop=True, reason=StopReason.TOKEN_BUDGET)

        scores = [self._round_score(round_metrics) for round_metrics in trajectory]
        best_index = scores.index(max(scores))  # `index` returns the first match, so ties keep the earliest best
        if len(scores) - 1 - best_index >= self.patience:
            return StopDecision(should_stop=True, reason=StopReason.PATIENCE)

        return StopDecision(should_stop=False, reason=None)

    def _round_score(self, round_metrics: Mapping[str, float]) -> float:
        """One round's primary-metric score, the value the patience streak tracks, raising `ValueError` when it is missing."""
        if self.primary_metric not in round_metrics:
            msg = f"trajectory round is missing the primary metric {self.primary_metric!r}"
            raise ValueError(msg)
        return round_metrics[self.primary_metric]
