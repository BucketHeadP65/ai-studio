"""The plan, thresholds and report of a comparison between two retrieval strategies.

`StrategyComparisonPlan` refuses a pair that varies anything but `strategy`: the model, its version,
the instructions, the dataset, the budget and the grader must be identical on both sides.
`graph_pair_verdict` derives from the strategy registry whether a pair isolates graph value: the baseline
exposes `search` and no graph tool, the candidate is exactly the baseline plus `graph_expand` and
`find_paths`, and both share one search leg.
A strategy with no `search` tool on either side is refused as an endpoint-discovery confound, and that
surface is declared as the `endpoint_discovery_ablation` type, which reports its measures and recommends nothing.
`EvaluationThresholds` is the conservative gate, where every unknown blocks acceptance rather than passing.
`objective_reading` names the pricing scope each objective admits to.
"""

from __future__ import annotations

from decimal import Decimal
from enum import StrEnum
from typing import Literal, Self

from core.ai.evaluation.contracts import (
    Assessment,
    ComparisonReport,
    EvaluationLimits,
    EvaluationModel,
    Fingerprint,
    Nonempty,
    TaskExecution,
    Variant,
)
from pydantic import Field, model_validator

from engram.evals.outcomes.domain.answers import AnswerAssessment
from engram.evals.outcomes.domain.evaluators import MetricStatus
from engram.evals.shared.contracts import FIND_PATHS, GRAPH_EXPAND, SEARCH
from engram.evals.tuning.contracts import EVAL_STRATEGY_NAMES, RetrievalStrategy, resolve_strategy

ComparisonObjective = Literal["task_success", "measured_generative_model_cost"]
"""What a new comparison is decided on. Money means measured generative-model response charges."""

PricingScope = Literal["not_applicable", "measured_generative_model_response"]
"""Which charges an objective's money claim actually covers."""

StrategyComparisonType = Literal["graph_incremental_value", "endpoint_discovery_ablation", "descriptive_only"]
"""What a strategy comparison is for. Only `graph_incremental_value` can reach an accepted verdict."""

ResourceMeasure = Literal["input_tokens", "delivered_bytes", "requests", "duration_ms"]
"""The four resources a candidate is held to, each read from the trial's own trace."""

RESOURCE_MEASURES: tuple[ResourceMeasure, ...] = ("input_tokens", "delivered_bytes", "requests", "duration_ms")
"""Every resource measure, in the order a report lists them."""

GRAPH_TOOLS = frozenset({GRAPH_EXPAND, FIND_PATHS})
"""The tools that make up the graph leg of a retrieval strategy."""


class EvaluationThresholds(EvaluationModel):
    """The gate a comparison must clear before it may recommend anything.

    Required evidence is what a curated case marks necessary, so its default recall is 1.0 without
    claiming the curated list is exhaustive. `resource_budget_pct` is the fraction of the baseline a
    candidate may add on each measured resource.
    """

    repetitions: int = Field(default=3, ge=2)
    max_failed_trials: int = Field(default=0, ge=0)
    max_contaminated_trials: int = Field(default=0, ge=0)
    max_task_regressions: int = Field(default=0, ge=0)
    require_strict_task_gain: bool = True
    min_required_source_recall: float = Field(default=1.0, ge=0, le=1)
    max_unsupported_critical_claims: int = Field(default=0, ge=0)
    require_workflow_design_pass: bool = True
    require_all_no_answer_abstentions: bool = True
    resource_budget_pct: float = Field(default=0.10, ge=0)
    max_dataset_staleness: float = Field(default=0.0, ge=0, le=1)


class StrategyRejection(StrEnum):
    """Everything that can stand between a comparison and an accepted result, named once."""

    TOO_FEW_REPETITIONS = "too_few_repetitions"
    INCOMPLETE_REPETITIONS = "incomplete_repetitions"
    FAILED_TRIALS = "failed_trials"
    CONTAMINATED_TRIALS = "contaminated_trials"
    TASK_REGRESSIONS = "task_regressions"
    NO_STRICT_TASK_GAIN = "no_strict_task_gain"
    UNKNOWN_REQUIRED_GRADE = "unknown_required_grade"
    UNSUPPORTED_CRITICAL_CLAIMS = "unsupported_critical_claims"
    REQUIRED_SOURCE_RECALL_UNKNOWN = "required_source_recall_unknown"
    REQUIRED_SOURCE_RECALL_BELOW_THRESHOLD = "required_source_recall_below_threshold"
    WORKFLOW_DESIGN_NOT_PASSED = "workflow_design_not_passed"
    MISSING_ABSTENTION = "missing_abstention"
    NO_GRAPH_ELIGIBLE_CASES = "no_graph_eligible_cases"
    NO_GRAPH_EVIDENCE = "no_independently_useful_graph_evidence"
    GRAPH_VALUE_UNKNOWN = "graph_value_unknown"
    RESOURCE_BUDGET_EXCEEDED = "resource_budget_exceeded"
    RESOURCE_COMPARISON_UNKNOWN = "resource_comparison_unknown"
    DATASET_STALENESS_ABOVE_THRESHOLD = "dataset_staleness_above_threshold"
    DATASET_STALENESS_UNKNOWN = "dataset_staleness_unknown"
    KIND_RECOMMENDS_NOTHING = "kind_recommends_nothing"
    OBJECTIVE_NOT_DECIDED_HERE = "objective_not_decided_here"
    RUN_CONDITIONS_DIFFER = "run_conditions_differ"
    IMPLEMENTATION_UNCHANGED = "implementation_unchanged"
    PLAN_DOES_NOT_MATCH_RUN = "plan_does_not_match_run"
    CORE_COMPARISON_REJECTED = "core_comparison_rejected"


class ObjectiveReading(EvaluationModel):
    """What a report's objective is called, and which charges its money claim covers."""

    objective: ComparisonObjective
    label: Nonempty
    pricing_scope: PricingScope


_OBJECTIVE_READINGS: dict[ComparisonObjective, ObjectiveReading] = {
    "task_success": ObjectiveReading(objective="task_success", label="task success", pricing_scope="not_applicable"),
    "measured_generative_model_cost": ObjectiveReading(
        objective="measured_generative_model_cost",
        label="measured generative-model response cost",
        pricing_scope="measured_generative_model_response",
    ),
}


def objective_reading(objective: ComparisonObjective) -> ObjectiveReading:
    """How `objective` is labeled in a report.

    Args:
        objective: The objective a plan carries.

    Returns:
        Its label and the pricing scope that label admits to.
    """
    return _OBJECTIVE_READINGS[objective]


class StrategyPairVerdict(EvaluationModel):
    """Whether one ordered pair of strategies can carry a graph incremental-value claim, and why."""

    fair: bool
    reason: Nonempty


def graph_pair_verdict(baseline: RetrievalStrategy, candidate: RetrievalStrategy) -> StrategyPairVerdict:
    """Whether `candidate` is `baseline` plus the graph leg and nothing else.

    Args:
        baseline: The strategy the comparison measures from.
        candidate: The strategy the comparison measures.

    Returns:
        `fair` with the pair's description, or a refusal naming what else moved. A strategy with no
        `search` tool on either side is refused as an endpoint-discovery confound.
    """
    profiles = {strategy: resolve_strategy(strategy.value) for strategy in (baseline, candidate)}
    for strategy, profile in profiles.items():
        if SEARCH not in profile.tools:
            reason = f"endpoint-discovery confound: {strategy.value} exposes no search tool, so the pair measures entry-point discovery rather than graph value"
            return StrategyPairVerdict(fair=False, reason=reason)
    if profiles[baseline].tools & GRAPH_TOOLS:
        return StrategyPairVerdict(fair=False, reason=f"the baseline {baseline.value} already exposes the graph leg, so no evidence is left to attribute to it")
    if not GRAPH_TOOLS.issubset(profiles[candidate].tools):
        return StrategyPairVerdict(fair=False, reason=f"the candidate {candidate.value} exposes no graph leg to attribute evidence to")
    if profiles[baseline].search_hybrid != profiles[candidate].search_hybrid:
        return StrategyPairVerdict(fair=False, reason=f"the search leg changes with the graph leg: {baseline.value} and {candidate.value} do not share one search mode")
    added = profiles[candidate].tools - profiles[baseline].tools
    if added != GRAPH_TOOLS:
        extra = ", ".join(sorted(added - GRAPH_TOOLS))
        return StrategyPairVerdict(fair=False, reason=f"the candidate {candidate.value} adds more than the graph leg: {extra}")
    return StrategyPairVerdict(fair=True, reason=f"{candidate.value} is {baseline.value} plus the graph leg")


class StrategyArm(EvaluationModel):
    """One side of a strategy comparison: its retrieval strategy and the conditions it ran under.

    Everything but `strategy` is held equal across the pair. `limits` is the live budget both sides
    ran under, `None` when no live budget was declared.
    """

    strategy: RetrievalStrategy
    model: Nonempty
    model_version: Nonempty
    instructions: Nonempty
    dataset_fingerprint: Fingerprint
    grader_fingerprint: Fingerprint
    limits: EvaluationLimits | None = None


class StrategyComparisonPlan(EvaluationModel):
    """What the comparison is for, which strategies it pairs, and the gate it must clear.

    `graph_eligible_case_ids` is declared here rather than inferred from results, so a case cannot
    be called graph-eligible after seeing which cases the candidate happened to win.
    `measured_dataset_staleness` is the fraction of those cases a freshness check found stale when
    the plan was declared. Left unset it reads unknown, which blocks acceptance.
    """

    kind: StrategyComparisonType
    baseline: StrategyArm
    candidate: StrategyArm
    objective: ComparisonObjective = "task_success"
    thresholds: EvaluationThresholds = EvaluationThresholds()
    graph_eligible_case_ids: tuple[Nonempty, ...] = ()
    measured_dataset_staleness: float | None = Field(default=None, ge=0, le=1)

    @model_validator(mode="after")
    def only_the_strategy_differs(self) -> Self:
        """Refuse a pair that moves anything but the retrieval strategy.

        Returns:
            The plan itself, unchanged.
        """
        if self.baseline.strategy == self.candidate.strategy:
            msg = "a strategy comparison needs two different retrieval strategies"
            raise ValueError(msg)
        differences = tuple(
            label
            for label, mine, theirs in (
                ("model", self.baseline.model, self.candidate.model),
                ("model version", self.baseline.model_version, self.candidate.model_version),
                ("instructions", self.baseline.instructions, self.candidate.instructions),
                ("dataset", self.baseline.dataset_fingerprint, self.candidate.dataset_fingerprint),
                ("grader", self.baseline.grader_fingerprint, self.candidate.grader_fingerprint),
                ("budget", self.baseline.limits, self.candidate.limits),
            )
            if mine != theirs
        )
        if differences:
            msg = f"a strategy comparison may vary only the retrieval strategy, but these differ: {', '.join(differences)}"
            raise ValueError(msg)
        return self

    @model_validator(mode="after")
    def the_pair_matches_the_kind(self) -> Self:
        """Refuse a pair that cannot answer the question its `kind` names.

        Returns:
            The plan itself, unchanged.
        """
        for arm in (self.baseline, self.candidate):
            if arm.strategy.value not in EVAL_STRATEGY_NAMES:
                msg = f"{arm.strategy.value} is not an experiment-axis strategy (expected one of {', '.join(EVAL_STRATEGY_NAMES)})"
                raise ValueError(msg)
        if self.kind == "graph_incremental_value":
            verdict = graph_pair_verdict(self.baseline.strategy, self.candidate.strategy)
            if not verdict.fair:
                msg = f"a graph incremental-value comparison cannot use this pair: {verdict.reason}"
                raise ValueError(msg)
        if self.kind == "endpoint_discovery_ablation" and not self._names_a_search_free_arm():
            msg = "an endpoint-discovery ablation names a strategy with no search tool on one of its sides"
            raise ValueError(msg)
        return self

    def _names_a_search_free_arm(self) -> bool:
        """Whether either side runs without a `search` tool, which is what the ablation measures."""
        return any(SEARCH not in resolve_strategy(arm.strategy.value).tools for arm in (self.baseline, self.candidate))


class GradedTrial(EvaluationModel):
    """One paired-trial slot's independent grades, matched to the trial that produced it.

    `answer` is the blind grader's verdict, `None` when the trial produced none. Both read as an
    unknown required grade. `required_source_recall` is the share of the evidence the case marks
    required that the trial actually surfaced, `None` when it was not measured. `workflow_design`
    is `not_applicable` for a trial that produces no workflow, and `None` when it was not graded.
    """

    case_id: Nonempty
    repetition: int = Field(ge=0)
    variant: Variant
    answer: AnswerAssessment | None = None
    required_source_recall: float | None = Field(default=None, ge=0, le=1)
    workflow_design: Assessment | None = None


class GraphEvidenceCredit(EvaluationModel):
    """One graph-delivered payload a supported claim rested on, and what the baseline had shown.

    `baseline_surfaced` is true when every source in the payload was already surfaced by the
    baseline on that case, which is the case where traversal added nothing new. A payload naming no
    source at all cannot be shown to have added one, so it reads as already surfaced.
    """

    case_id: Nonempty
    repetition: int = Field(ge=0)
    tool_name: Nonempty
    evidence_id: Nonempty
    claim: Nonempty
    sources: tuple[Nonempty, ...] = ()
    baseline_surfaced: bool


class GraphIncrementalValue(EvaluationModel):
    """What the graph leg contributed on the declared eligible cases.

    `not_applicable` when the candidate exposes no graph leg. `unknown` when a cited graph payload
    could not be read back exactly as it was stored (never resolved by rereading the source), or
    when a supported claim cited a payload no tool call in that trial delivered at all, which means
    the record and the verdict disagree. `measured` otherwise, including a measured zero. A claim
    citing a payload some other tool delivered is ordinary and earns nothing.
    """

    status: MetricStatus
    credits: tuple[GraphEvidenceCredit, ...] = ()
    unreadable_evidence_ids: tuple[Nonempty, ...] = ()
    unmatched_evidence_ids: tuple[Nonempty, ...] = ()

    @property
    def novel_credits(self) -> tuple[GraphEvidenceCredit, ...]:
        """Every credit carrying a source the baseline never surfaced."""
        return tuple(credit for credit in self.credits if not credit.baseline_surfaced)


class ResourceRatio(EvaluationModel):
    """One case's candidate-to-baseline ratio on one resource, and whether it stayed in allowance.

    `ratio` is `None` when the baseline mean was zero, which no division describes. The candidate is
    then within allowance only when it also spent nothing.
    """

    case_id: Nonempty
    measure: ResourceMeasure
    status: MetricStatus
    baseline_mean: float | None = None
    candidate_mean: float | None = None
    ratio: float | None = None
    within_allowance: bool = False


class ResourceComparison(EvaluationModel):
    """Every case's resource ratios, and whether the whole comparison could be measured at all."""

    status: MetricStatus
    allowance_pct: float = Field(ge=0)
    ratios: tuple[ResourceRatio, ...] = ()

    @property
    def exceeded(self) -> tuple[ResourceRatio, ...]:
        """Every measured ratio outside the allowance."""
        return tuple(ratio for ratio in self.ratios if ratio.status == "measured" and not ratio.within_allowance)

    @property
    def unmeasured(self) -> tuple[ResourceRatio, ...]:
        """Every ratio that could not be measured, which never counts as within allowance."""
        return tuple(ratio for ratio in self.ratios if ratio.status == "unknown")


class MeasuredCostComparison(EvaluationModel):
    """What the two arms' model turns charged, `unknown` the moment one turn carries no price."""

    status: MetricStatus
    baseline_usd: Decimal | None = Field(default=None, ge=0)
    candidate_usd: Decimal | None = Field(default=None, ge=0)


class StrategyComparisonReport[ExecutionT: TaskExecution](EvaluationModel):
    """One strategy comparison's complete result: every measure, and what stands against accepting it.

    `verdict` is `reported` for a comparison type or an objective this gate does not decide: the measures are
    all present and nothing is recommended. `accepted` requires the graph incremental-value type,
    the task-success objective and an empty `reasons`. The claim is descriptive throughout: these
    are the observed results of the declared repetitions, not a population-level finding.
    """

    plan: StrategyComparisonPlan
    comparison: ComparisonReport[ExecutionT]
    objective: ObjectiveReading
    verdict: Literal["accepted", "rejected", "reported"]
    reasons: tuple[StrategyRejection, ...] = ()
    graph_value: GraphIncrementalValue
    resources: ResourceComparison
    measured_generative_model_cost: MeasuredCostComparison
    comparison_reasons: tuple[str, ...] = ()
    failed_trials: int = Field(ge=0)
    contaminated_trials: int = Field(ge=0)
    regressed_case_ids: tuple[Nonempty, ...] = ()
    unsupported_critical_claims: int = Field(ge=0)
    statistical_claim: Literal["descriptive_only"] = "descriptive_only"
