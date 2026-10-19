"""The strategy-comparison table: which pairs are fair, what graph value requires, and what rejects.

The parametrized table is the spec for `engram.evals.outcomes.domain.comparison`. Every row states one
complete scenario and the result it must produce, from the plan that is refused before any trial
runs to the report that accepts a narrow, described win.

Every scenario runs over a real `FileDeliveredEvidenceStore` in `tmp_path`, the adapter a guarded
trial actually writes through, so a graph payload the reader refuses is refused for the same reason
a live one would be.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from decimal import Decimal
from typing import TYPE_CHECKING, Literal

import pydantic
import pytest

from core.ai.evaluation.contracts import (
    Assessment,
    ComparisonPlan,
    DeliveredEvidenceRef,
    DeliveredSource,
    EvaluationLimits,
    ExecutionTrace,
    FileDeliveredEvidenceStore,
    OutcomeTrial,
    Provenance,
    ResourceUsage,
    RunIdentity,
    TaskCase,
    TaskDataset,
    TaskExecution,
    TaskGrade,
    ToolCallTrace,
    Variant,
    assess_comparison,
    content_fingerprint,
)
from core.ai.evaluation.domain.trace import ModelTurnTrace

from engram.evals.outcomes.domain.answers import AnswerCriterion, EvidenceRequirement
from engram.evals.outcomes.domain.comparison import (
    EvaluationThresholds,
    GradedTrial,
    StrategyArm,
    StrategyComparisonPlan,
    StrategyComparisonType,
    StrategyRejection,
    graph_pair_verdict,
    objective_reading,
)
from engram.evals.outcomes.service import assess_strategy_comparison
from engram.evals.shared.serve_bridge import GRAPH_EXPAND, SEARCH
from engram.evals.tuning.strategy import RetrievalStrategy
from tests.support.fakes.outcome_grader import assessment_at, claim_at

if TYPE_CHECKING:
    from pathlib import Path

    from engram.evals.outcomes.domain.comparison import ComparisonObjective

_COLLECTION = "research"
_CASE = "case-connection"
_SEARCHED_PAGE = "page:research/paper.a"
_GRAPH_PAGE = "page:research/paper.b"
_CLAIM = "the two papers share a method"
_REPETITIONS = 3

_CRITERION = AnswerCriterion(
    answerability="answerable",
    evidence=(EvidenceRequirement(source_ref=_GRAPH_PAGE, role="support", claim=_CLAIM),),
)
_NO_WORKFLOW = Assessment(status="not_applicable", reason="a retrieval trial produces no workflow to grade")
_DATASET = TaskDataset(
    name="graph-value",
    version="v1",
    cases=(
        TaskCase(
            case_id=_CASE,
            input="which method do the two papers share?",
            success_criterion="names the shared method and cites both papers",
            split="test",
            provenance=Provenance(source="curated", kind="recorded", review="owner", reviewer="owner"),
        ),
    ),
)
_OTHER_LIMITS = EvaluationLimits(
    cycle_cost_stop_usd=Decimal(10),
    trial_cost_stop_usd=Decimal(2),
    trial_deadline_seconds=180,
    trial_request_limit=16,
    trial_attempt_limit=3,
)

type _ArmChange = Literal["model", "instructions", "dataset", "budget", "grader"]
type _IdentityChange = Literal["prompt", "unchanged", "plan_mismatch"]


@dataclass(frozen=True)
class _Scenario:
    """One row's complete inputs: the plan it declares and how its trials differ from a healthy pair."""

    kind: StrategyComparisonType = "graph_incremental_value"
    baseline_strategy: RetrievalStrategy = RetrievalStrategy.SEARCH_HYBRID
    candidate_strategy: RetrievalStrategy = RetrievalStrategy.FULL
    objective: ComparisonObjective = "task_success"
    arm_change: _ArmChange | None = None
    candidate_graph: bool = True
    graph_readable: bool = True
    baseline_surfaces_graph_page: bool = False
    failed: bool = False
    contaminated: bool = False
    regression: bool = False
    unknown_grade: bool = False
    priced: bool = True
    candidate_duration_ms: int = 100
    identity_change: _IdentityChange | None = None
    drop_trial: bool = False
    cite_search_evidence: bool = False
    cite_unknown_evidence: bool = False


@dataclass(frozen=True)
class _Observed:
    """What one row actually produced, normalized to the facts the row asserts."""

    plan_valid: bool
    refusal: str = ""
    verdict: str = ""
    reasons: frozenset[StrategyRejection] = field(default_factory=frozenset)
    graph_status: str = ""
    cost_status: str = ""
    label: str = ""
    pricing_scope: str = ""
    comparison_reasons: tuple[str, ...] = ()


@dataclass(frozen=True)
class _Expected:
    """What the row requires. An empty string or an empty tuple is a fact the row does not assert."""

    plan_valid: bool
    refusal: str = ""
    verdict: str = ""
    reasons: tuple[StrategyRejection, ...] = ()
    graph_status: str = ""
    cost_status: str = ""
    label: str = ""
    pricing_scope: str = ""


def _fingerprint(text: str) -> str:
    """A 64-hex fingerprint standing for `text`."""
    return content_fingerprint(text)


def _identity(strategy: RetrievalStrategy) -> RunIdentity:
    """The run identity of the arm running `strategy`, which is the implementation under test."""
    return RunIdentity(
        model="test-model",
        model_version="2026-09-01",
        prompt_fingerprint=_fingerprint("instructions"),
        implementation_fingerprint=_fingerprint(strategy.value),
        conditions_fingerprint=_fingerprint("conditions"),
        grader_fingerprint=_fingerprint("grader"),
    )


def _identity_for(scenario: _Scenario, variant: Variant) -> RunIdentity:
    """What the run actually recorded for one arm, which the row may put at odds with the plan."""
    strategy = scenario.baseline_strategy if variant == "baseline" else scenario.candidate_strategy
    identity = _identity(strategy)
    if scenario.identity_change == "plan_mismatch":
        return identity.model_copy(update={"model": "other-model"})
    if variant == "baseline":
        return identity
    if scenario.identity_change == "prompt":
        return identity.model_copy(update={"prompt_fingerprint": _fingerprint("other instructions")})
    if scenario.identity_change == "unchanged":
        return identity.model_copy(update={"implementation_fingerprint": _identity(scenario.baseline_strategy).implementation_fingerprint})
    return identity


def _arm(strategy: RetrievalStrategy, change: _ArmChange | None = None) -> StrategyArm:
    """One side of the comparison, optionally changing the one held condition `change` names."""
    arm = StrategyArm(
        strategy=strategy,
        model="test-model",
        model_version="2026-09-01",
        instructions="answer from the knowledge base",
        dataset_fingerprint=_DATASET.fingerprint,
        grader_fingerprint=_fingerprint("grader"),
    )
    if change == "model":
        return arm.model_copy(update={"model": "other-model"})
    if change == "instructions":
        return arm.model_copy(update={"instructions": "answer from memory"})
    if change == "dataset":
        return arm.model_copy(update={"dataset_fingerprint": _fingerprint("other dataset")})
    if change == "budget":
        return arm.model_copy(update={"limits": _OTHER_LIMITS})
    if change == "grader":
        return arm.model_copy(update={"grader_fingerprint": _fingerprint("other grader")})
    return arm


def _strategy_plan(scenario: _Scenario) -> StrategyComparisonPlan:
    """The strategy comparison `scenario` declares, refused here when it varies more than the strategy."""
    return StrategyComparisonPlan(
        kind=scenario.kind,
        baseline=_arm(scenario.baseline_strategy),
        candidate=_arm(scenario.candidate_strategy, scenario.arm_change),
        objective=scenario.objective,
        graph_eligible_case_ids=(_CASE,),
        measured_dataset_staleness=0.0,
    )


def _comparison_plan() -> ComparisonPlan:
    """The core paired-comparison plan every scenario runs: one held-out case, three repetitions."""
    return ComparisonPlan(dataset=_DATASET, evaluation_case_ids=(_CASE,), repetitions=_REPETITIONS)


def _model_turn(scenario: _Scenario, variant: Variant, repetition: int) -> ModelTurnTrace:
    """The trial's one model turn, unpriced on the candidate's first repetition when the row says so."""
    unpriced = not scenario.priced and variant == "candidate" and repetition == 0
    return ModelTurnTrace(
        ordinal=0, input_tokens=1000, output_tokens=100, duration_ms=50, cost_usd=None if unpriced else Decimal("0.01")
    )


def _baseline_trace(scenario: _Scenario, repetition: int) -> ExecutionTrace:
    """The baseline trial's trace: one search call, surfacing the graph page only when the row says so."""
    surfaced = (_SEARCHED_PAGE, _GRAPH_PAGE) if scenario.baseline_surfaces_graph_page else (_SEARCHED_PAGE,)
    search = ToolCallTrace(
        tool_name=SEARCH, ordinal=0, status="ok", returned_unit_refs=surfaced, response_bytes=2000, duration_ms=10
    )
    return ExecutionTrace(tool_calls=(search,), model_turns=(_model_turn(scenario, "baseline", repetition),), run_duration_ms=100)


def _candidate_trace(
    scenario: _Scenario, repetition: int, graph_ref: DeliveredEvidenceRef | None, search_ref: DeliveredEvidenceRef | None = None
) -> ExecutionTrace:
    """The candidate trial's trace: the same search plus the graph call the row gives it."""
    search_bytes = 1800 if graph_ref is not None else 2000
    calls = [
        ToolCallTrace(
            tool_name=SEARCH,
            ordinal=0,
            status="ok",
            delivered_evidence=() if search_ref is None else (search_ref,),
            returned_unit_refs=(_SEARCHED_PAGE,),
            response_bytes=search_bytes,
            duration_ms=10,
        )
    ]
    if graph_ref is not None:
        calls.append(
            ToolCallTrace(
                tool_name=GRAPH_EXPAND,
                ordinal=1,
                status="ok",
                delivered_evidence=(graph_ref,),
                returned_unit_refs=(_GRAPH_PAGE,),
                response_bytes=200,
                duration_ms=10,
            )
        )
    if scenario.contaminated and repetition == 0:
        calls.append(ToolCallTrace(tool_name=SEARCH, ordinal=2, status="refused", response_bytes=0, duration_ms=1, withheld_exposure=True))
    return ExecutionTrace(
        tool_calls=tuple(calls),
        model_turns=(_model_turn(scenario, "candidate", repetition),),
        run_duration_ms=scenario.candidate_duration_ms,
    )


async def _graph_ref(scenario: _Scenario, store: FileDeliveredEvidenceStore, repetition: int) -> DeliveredEvidenceRef | None:
    """The candidate trial's stored graph payload, or a reference to bytes the store never held."""
    if not scenario.candidate_graph:
        return None
    stored = await store.append(
        f"{_CASE}:{repetition}:candidate",
        1,
        '{"nodes": ["paper.b"]}',
        sources=(DeliveredSource(source_ref=_GRAPH_PAGE),),
    )
    if scenario.graph_readable:
        return stored
    return stored.model_copy(update={"payload_sha256": _fingerprint("a payload the store never held")})


async def _search_ref(scenario: _Scenario, store: FileDeliveredEvidenceStore, repetition: int) -> DeliveredEvidenceRef | None:
    """The candidate trial's stored search payload, present only for the row that cites one."""
    if not scenario.cite_search_evidence:
        return None
    return await store.append(
        f"{_CASE}:{repetition}:candidate", 0, '{"hits": ["paper.a"]}', sources=(DeliveredSource(source_ref=_SEARCHED_PAGE),)
    )


def _unfiled_ref(repetition: int) -> DeliveredEvidenceRef:
    """A reference to a payload no tool call in the trial ever delivered."""
    return DeliveredEvidenceRef(
        evidence_id=f"{_CASE}:{repetition}:candidate:9", ordinal=9, payload_sha256=_fingerprint("never delivered"), payload_bytes=12
    )


def _succeeds(scenario: _Scenario, variant: Variant, repetition: int) -> bool:
    """Whether the trial's task grade succeeds, giving the candidate a strict gain unless the row removes it."""
    if variant == "baseline":
        return scenario.regression or repetition != 0
    return not (scenario.regression and repetition == 0)


def _answer_status(scenario: _Scenario, variant: Variant, repetition: int) -> Literal["pass", "fail", "unknown"]:
    """The independent answer verdict for one trial slot."""
    if scenario.unknown_grade and variant == "candidate" and repetition == 0:
        return "unknown"
    return "pass" if _succeeds(scenario, variant, repetition) else "fail"


def _cited(
    scenario: _Scenario, variant: Variant, repetition: int, graph_ref: DeliveredEvidenceRef | None, search_ref: DeliveredEvidenceRef | None
) -> tuple[DeliveredEvidenceRef, ...]:
    """What the trial's claim cites: its graph payload, plus whatever else the row adds to it."""
    if variant != "candidate":
        return ()
    refs = [ref for ref in (graph_ref, search_ref) if ref is not None]
    if scenario.cite_unknown_evidence:
        refs.append(_unfiled_ref(repetition))
    return tuple(refs)


def _graded(
    scenario: _Scenario,
    variant: Variant,
    repetition: int,
    graph_ref: DeliveredEvidenceRef | None,
    search_ref: DeliveredEvidenceRef | None = None,
) -> GradedTrial:
    """The independent grades for one trial slot, the candidate's claim resting on its graph payload."""
    evidence = _cited(scenario, variant, repetition, graph_ref, search_ref)
    status = _answer_status(scenario, variant, repetition)
    answer = assessment_at(_CRITERION, status, claims=(claim_at(_CLAIM, status, evidence),))
    return GradedTrial(
        case_id=_CASE,
        repetition=repetition,
        variant=variant,
        answer=answer,
        required_source_recall=1.0,
        workflow_design=_NO_WORKFLOW,
    )


async def _trials(
    scenario: _Scenario, store: FileDeliveredEvidenceStore
) -> tuple[tuple[OutcomeTrial[TaskExecution], ...], tuple[GradedTrial, ...]]:
    """Every paired trial slot the plan declares, with the grades that answer them."""
    trials: list[OutcomeTrial[TaskExecution]] = []
    grades: list[GradedTrial] = []
    for repetition in range(_REPETITIONS):
        graph_ref = await _graph_ref(scenario, store, repetition)
        search_ref = await _search_ref(scenario, store, repetition)
        for variant in ("baseline", "candidate"):
            identity = _identity_for(scenario, variant)
            if scenario.drop_trial and variant == "candidate" and repetition == 0:
                continue
            if scenario.failed and variant == "candidate" and repetition == 0:
                trials.append(
                    OutcomeTrial[TaskExecution](
                        case_id=_CASE,
                        repetition=repetition,
                        variant=variant,
                        identity=identity,
                        failure="the provider stopped answering",
                        failure_usage=ResourceUsage(input_tokens=10, requests=1),
                    )
                )
                continue
            trace = (
                _baseline_trace(scenario, repetition)
                if variant == "baseline"
                else _candidate_trace(scenario, repetition, graph_ref, search_ref)
            )
            succeeded = _succeeds(scenario, variant, repetition)
            trials.append(
                OutcomeTrial[TaskExecution](
                    case_id=_CASE,
                    repetition=repetition,
                    variant=variant,
                    identity=identity,
                    execution=TaskExecution(output="the shared method is spectral clustering", usage=ResourceUsage(requests=1), trace=trace),
                    grade=TaskGrade(success=succeeded, reason="graded against the case criterion"),
                )
            )
            grades.append(_graded(scenario, variant, repetition, graph_ref, search_ref))
    return tuple(trials), tuple(grades)


async def _observe(scenario: _Scenario, root: Path) -> _Observed:
    """Run one row end to end and report only the facts the table asserts."""
    try:
        plan = _strategy_plan(scenario)
    except pydantic.ValidationError as error:
        return _Observed(plan_valid=False, refusal=str(error))
    store = FileDeliveredEvidenceStore(root)
    trials, grades = await _trials(scenario, store)
    comparison = assess_comparison(
        _comparison_plan(),
        baseline=_identity_for(scenario, "baseline"),
        candidate=_identity_for(scenario, "candidate"),
        trials=trials,
    )
    report = await assess_strategy_comparison(plan, comparison, grades=grades, reader=store, collection=_COLLECTION)
    return _Observed(
        plan_valid=True,
        verdict=report.verdict,
        reasons=frozenset(report.reasons),
        graph_status=report.graph_value.status,
        cost_status=report.measured_generative_model_cost.status,
        label=report.objective.label,
        pricing_scope=report.objective.pricing_scope,
        comparison_reasons=report.comparison_reasons,
    )


_TABLE = (
    pytest.param(_Scenario(), _Expected(plan_valid=True), id="graph_incremental_value uses search-hybrid and full"),
    pytest.param(
        _Scenario(candidate_strategy=RetrievalStrategy.GRAPH_ONLY),
        _Expected(plan_valid=False, refusal="endpoint-discovery confound"),
        id="graph_incremental_value uses graph-only as the candidate",
    ),
    pytest.param(
        _Scenario(baseline_strategy=RetrievalStrategy.GRAPH_ONLY, candidate_strategy=RetrievalStrategy.FULL),
        _Expected(plan_valid=False, refusal="endpoint-discovery confound"),
        id="graph_incremental_value uses graph-only as the baseline",
    ),
    pytest.param(
        _Scenario(arm_change="model"), _Expected(plan_valid=False, refusal="these differ: model"), id="the model changes with the strategy"
    ),
    pytest.param(
        _Scenario(arm_change="instructions"),
        _Expected(plan_valid=False, refusal="these differ: instructions"),
        id="the prompt changes with the strategy",
    ),
    pytest.param(
        _Scenario(arm_change="dataset"),
        _Expected(plan_valid=False, refusal="these differ: dataset"),
        id="the dataset changes with the strategy",
    ),
    pytest.param(
        _Scenario(arm_change="budget"), _Expected(plan_valid=False, refusal="these differ: budget"), id="the budget changes with the strategy"
    ),
    pytest.param(
        _Scenario(arm_change="grader"), _Expected(plan_valid=False, refusal="these differ: grader"), id="the grader changes with the strategy"
    ),
    pytest.param(
        _Scenario(candidate_graph=False),
        _Expected(plan_valid=True, verdict="rejected", reasons=(StrategyRejection.NO_GRAPH_EVIDENCE,), graph_status="measured"),
        id="full makes no graph call",
    ),
    pytest.param(
        _Scenario(graph_readable=False),
        _Expected(plan_valid=True, verdict="rejected", reasons=(StrategyRejection.GRAPH_VALUE_UNKNOWN,), graph_status="unknown"),
        id="the retained graph payload is missing or changed",
    ),
    pytest.param(
        _Scenario(),
        _Expected(plan_valid=True, verdict="accepted", graph_status="measured", cost_status="measured"),
        id="full adds useful graph evidence, gains task success, regresses nothing and stays within limits",
    ),
    pytest.param(
        _Scenario(regression=True),
        _Expected(plan_valid=True, verdict="rejected", reasons=(StrategyRejection.TASK_REGRESSIONS,)),
        id="one case regresses",
    ),
    pytest.param(
        _Scenario(failed=True),
        _Expected(plan_valid=True, verdict="rejected", reasons=(StrategyRejection.FAILED_TRIALS,)),
        id="one trial failed",
    ),
    pytest.param(
        _Scenario(contaminated=True),
        _Expected(plan_valid=True, verdict="rejected", reasons=(StrategyRejection.CONTAMINATED_TRIALS,)),
        id="one trial was contaminated",
    ),
    pytest.param(
        _Scenario(unknown_grade=True),
        _Expected(plan_valid=True, verdict="rejected", reasons=(StrategyRejection.UNKNOWN_REQUIRED_GRADE,)),
        id="one required grade is unknown",
    ),
    pytest.param(_Scenario(priced=False), _Expected(plan_valid=True, cost_status="unknown"), id="one model turn carries no price"),
    pytest.param(
        _Scenario(objective="measured_generative_model_cost"),
        _Expected(
            plan_valid=True,
            verdict="reported",
            label="measured generative-model response cost",
            pricing_scope="measured_generative_model_response",
        ),
        id="the new objective is measured_generative_model_cost",
    ),
)


@pytest.mark.parametrize(("scenario", "expected"), _TABLE)
async def test_the_strategy_comparison_table(scenario: _Scenario, expected: _Expected, tmp_path: Path):
    observed = await _observe(scenario, tmp_path)

    assert observed.plan_valid is expected.plan_valid
    assert expected.refusal in observed.refusal
    assert set(expected.reasons) <= observed.reasons
    if expected.verdict:
        assert observed.verdict == expected.verdict
    if expected.graph_status:
        assert observed.graph_status == expected.graph_status
    if expected.cost_status:
        assert observed.cost_status == expected.cost_status
    if expected.label:
        assert observed.label == expected.label
    if expected.pricing_scope:
        assert observed.pricing_scope == expected.pricing_scope


class TestGraphPairVerdict:
    """Which ordered strategy pairs can carry a graph incremental-value claim."""

    @pytest.mark.parametrize(
        ("baseline", "candidate", "fair"),
        [
            (RetrievalStrategy.SEARCH_HYBRID, RetrievalStrategy.FULL, True),
            (RetrievalStrategy.SEARCH_KEYWORD, RetrievalStrategy.KEYWORD_GRAPH, True),
            (RetrievalStrategy.SEARCH_HYBRID, RetrievalStrategy.KEYWORD_GRAPH, False),
            (RetrievalStrategy.SEARCH_KEYWORD, RetrievalStrategy.FULL, False),
            (RetrievalStrategy.SEARCH_HYBRID, RetrievalStrategy.GRAPH_ONLY, False),
            (RetrievalStrategy.GRAPH_ONLY, RetrievalStrategy.FULL, False),
            (RetrievalStrategy.FULL, RetrievalStrategy.SEARCH_HYBRID, False),
            (RetrievalStrategy.SEARCH_HYBRID, RetrievalStrategy.SEARCH_KEYWORD, False),
        ],
    )
    def test_the_fair_pairs(self, baseline: RetrievalStrategy, candidate: RetrievalStrategy, fair: bool):
        assert graph_pair_verdict(baseline, candidate).fair is fair

    def test_a_graph_only_side_names_the_endpoint_discovery_confound(self):
        verdict = graph_pair_verdict(RetrievalStrategy.SEARCH_HYBRID, RetrievalStrategy.GRAPH_ONLY)

        assert "endpoint-discovery confound" in verdict.reason

    def test_a_changed_search_leg_names_what_else_moved(self):
        verdict = graph_pair_verdict(RetrievalStrategy.SEARCH_HYBRID, RetrievalStrategy.KEYWORD_GRAPH)

        assert "search leg" in verdict.reason


class TestStrategyComparisonPlan:
    """What a plan refuses before a single trial runs."""

    def test_the_same_strategy_on_both_sides_is_refused(self):
        with pytest.raises(pydantic.ValidationError, match="differ"):
            StrategyComparisonPlan(
                kind="descriptive_only", baseline=_arm(RetrievalStrategy.FULL), candidate=_arm(RetrievalStrategy.FULL)
            )

    def test_code_mode_is_not_an_experiment_axis_value(self):
        with pytest.raises(pydantic.ValidationError, match="code-mode"):
            StrategyComparisonPlan(
                kind="descriptive_only", baseline=_arm(RetrievalStrategy.SEARCH_HYBRID), candidate=_arm(RetrievalStrategy.CODE_MODE)
            )

    def test_an_ablation_must_actually_name_a_graph_only_side(self):
        with pytest.raises(pydantic.ValidationError, match="endpoint-discovery ablation"):
            StrategyComparisonPlan(
                kind="endpoint_discovery_ablation",
                baseline=_arm(RetrievalStrategy.SEARCH_HYBRID),
                candidate=_arm(RetrievalStrategy.FULL),
            )

    def test_the_ablation_pair_is_accepted_and_labeled(self):
        plan = StrategyComparisonPlan(
            kind="endpoint_discovery_ablation",
            baseline=_arm(RetrievalStrategy.SEARCH_HYBRID),
            candidate=_arm(RetrievalStrategy.GRAPH_ONLY),
        )

        assert plan.kind == "endpoint_discovery_ablation"

    def test_an_empty_eligible_set_is_left_to_assess_time(self):
        # The eligible set is declared, not inferred, so an empty one is a legal plan. The rejection
        # for naming no eligible case belongs to the assessment, where the trials are in hand.
        plan = StrategyComparisonPlan(
            kind="graph_incremental_value", baseline=_arm(RetrievalStrategy.SEARCH_HYBRID), candidate=_arm(RetrievalStrategy.FULL)
        )

        assert plan.graph_eligible_case_ids == ()


class TestEvaluationThresholds:
    """The conservative defaults, which are the shipped gate."""

    def test_the_shipped_defaults(self):
        thresholds = EvaluationThresholds()

        assert thresholds.repetitions == 3
        assert thresholds.max_failed_trials == 0
        assert thresholds.max_contaminated_trials == 0
        assert thresholds.max_task_regressions == 0
        assert thresholds.require_strict_task_gain is True
        assert thresholds.min_required_source_recall == 1.0
        assert thresholds.max_unsupported_critical_claims == 0
        assert thresholds.require_workflow_design_pass is True
        assert thresholds.require_all_no_answer_abstentions is True
        assert thresholds.resource_budget_pct == 0.10
        assert thresholds.max_dataset_staleness == 0.0

    def test_fewer_than_two_repetitions_is_refused(self):
        with pytest.raises(pydantic.ValidationError):
            EvaluationThresholds(repetitions=1)


class TestObjectiveReading:
    """Every objective a report can carry, and the pricing scope its label admits to."""

    def test_task_success_claims_no_pricing_scope(self):
        reading = objective_reading("task_success")

        assert reading.label == "task success"
        assert reading.pricing_scope == "not_applicable"

    def test_the_measured_objective_names_the_generative_model_scope(self):
        reading = objective_reading("measured_generative_model_cost")

        assert reading.label == "measured generative-model response cost"
        assert reading.pricing_scope == "measured_generative_model_response"


class TestResourceGuardrail:
    """The four measures a candidate must stay within, and what an unmeasurable one does."""

    async def test_a_slower_candidate_is_rejected(self, tmp_path: Path):
        observed = await _observe(_Scenario(candidate_duration_ms=1000), tmp_path)

        assert observed.verdict == "rejected"
        assert StrategyRejection.RESOURCE_BUDGET_EXCEEDED in observed.reasons

    async def test_a_candidate_inside_the_allowance_is_not_rejected_for_resources(self, tmp_path: Path):
        observed = await _observe(_Scenario(candidate_duration_ms=109), tmp_path)

        assert StrategyRejection.RESOURCE_BUDGET_EXCEEDED not in observed.reasons

    async def test_an_untraced_trial_leaves_the_comparison_unknown(self, tmp_path: Path):
        observed = await _observe(_Scenario(failed=True), tmp_path)

        assert StrategyRejection.RESOURCE_COMPARISON_UNKNOWN in observed.reasons


class TestGraphValue:
    """What the candidate must show before any of its evidence counts as graph value."""

    async def test_evidence_the_baseline_already_surfaced_earns_nothing(self, tmp_path: Path):
        observed = await _observe(_Scenario(baseline_surfaces_graph_page=True), tmp_path)

        assert observed.verdict == "rejected"
        assert StrategyRejection.NO_GRAPH_EVIDENCE in observed.reasons

    async def test_an_undeclared_eligible_case_cannot_earn_value(self, tmp_path: Path):
        scenario = _Scenario()
        plan = _strategy_plan(scenario).model_copy(update={"graph_eligible_case_ids": ()})
        store = FileDeliveredEvidenceStore(tmp_path)
        trials, grades = await _trials(scenario, store)
        comparison = assess_comparison(
            _comparison_plan(),
            baseline=_identity_for(scenario, "baseline"),
            candidate=_identity_for(scenario, "candidate"),
            trials=trials,
        )

        report = await assess_strategy_comparison(plan, comparison, grades=grades, reader=store, collection=_COLLECTION)

        assert report.verdict == "rejected"
        assert StrategyRejection.NO_GRAPH_ELIGIBLE_CASES in report.reasons

    async def test_the_accepted_report_names_the_credited_source(self, tmp_path: Path):
        scenario = _Scenario()
        plan = _strategy_plan(scenario)
        store = FileDeliveredEvidenceStore(tmp_path)
        trials, grades = await _trials(scenario, store)
        comparison = assess_comparison(
            _comparison_plan(),
            baseline=_identity_for(scenario, "baseline"),
            candidate=_identity_for(scenario, "candidate"),
            trials=trials,
        )

        report = await assess_strategy_comparison(plan, comparison, grades=grades, reader=store, collection=_COLLECTION)

        assert report.verdict == "accepted"
        assert report.statistical_claim == "descriptive_only"
        earned = report.graph_value.novel_credits
        assert earned
        assert earned[0].tool_name == GRAPH_EXPAND
        assert earned[0].sources == (_GRAPH_PAGE,)


class TestRunIsolation:
    """The gate holds the recorded run against the plan before it grades anything."""

    async def test_the_declared_pair_with_matching_identities_is_accepted(self, tmp_path: Path):
        observed = await _observe(_Scenario(), tmp_path)

        assert observed.verdict == "accepted"
        assert observed.reasons == frozenset()

    async def test_a_prompt_that_moved_with_the_strategy_is_rejected(self, tmp_path: Path):
        observed = await _observe(_Scenario(identity_change="prompt"), tmp_path)

        assert observed.verdict == "rejected"
        assert StrategyRejection.RUN_CONDITIONS_DIFFER in observed.reasons

    async def test_an_isolation_failure_is_reported_before_any_threshold(self, tmp_path: Path):
        # The row also regresses a case. A run whose variable was not isolated has nothing to
        # conclude from, so the threshold reasons are never derived from it.
        observed = await _observe(_Scenario(identity_change="prompt", regression=True), tmp_path)

        assert observed.verdict == "rejected"
        assert StrategyRejection.RUN_CONDITIONS_DIFFER in observed.reasons
        assert StrategyRejection.TASK_REGRESSIONS not in observed.reasons

    async def test_two_arms_recording_one_implementation_are_rejected(self, tmp_path: Path):
        observed = await _observe(_Scenario(identity_change="unchanged"), tmp_path)

        assert observed.verdict == "rejected"
        assert StrategyRejection.IMPLEMENTATION_UNCHANGED in observed.reasons
        assert StrategyRejection.CORE_COMPARISON_REJECTED in observed.reasons
        assert "unchanged candidate" in observed.comparison_reasons

    async def test_a_plan_declaring_a_model_the_run_did_not_record_is_rejected(self, tmp_path: Path):
        observed = await _observe(_Scenario(identity_change="plan_mismatch"), tmp_path)

        assert observed.verdict == "rejected"
        assert StrategyRejection.PLAN_DOES_NOT_MATCH_RUN in observed.reasons

    async def test_a_comparison_the_kernel_rejected_can_never_read_accepted(self, tmp_path: Path):
        observed = await _observe(_Scenario(drop_trial=True), tmp_path)

        assert observed.verdict == "rejected"
        assert StrategyRejection.CORE_COMPARISON_REJECTED in observed.reasons
        assert "incomplete repetitions" in observed.comparison_reasons


class TestCitedEvidenceTheTrialDidNotDeliver:
    """A supported claim citing a payload the trial's record does not hold."""

    async def test_a_citation_no_call_delivered_reads_unknown(self, tmp_path: Path):
        observed = await _observe(_Scenario(cite_unknown_evidence=True), tmp_path)

        assert observed.graph_status == "unknown"
        assert observed.verdict == "rejected"
        assert StrategyRejection.GRAPH_VALUE_UNKNOWN in observed.reasons

    async def test_a_citation_another_tool_delivered_is_ordinary(self, tmp_path: Path):
        observed = await _observe(_Scenario(cite_search_evidence=True), tmp_path)

        assert observed.graph_status == "measured"
        assert observed.verdict == "accepted"

    async def test_the_unmatched_citation_is_named(self, tmp_path: Path):
        scenario = _Scenario(cite_unknown_evidence=True)
        plan = _strategy_plan(scenario)
        store = FileDeliveredEvidenceStore(tmp_path)
        trials, grades = await _trials(scenario, store)
        comparison = assess_comparison(
            _comparison_plan(),
            baseline=_identity_for(scenario, "baseline"),
            candidate=_identity_for(scenario, "candidate"),
            trials=trials,
        )

        report = await assess_strategy_comparison(plan, comparison, grades=grades, reader=store, collection=_COLLECTION)

        assert report.graph_value.unmatched_evidence_ids == tuple(f"{_CASE}:{repetition}:candidate:9" for repetition in range(_REPETITIONS))
        assert report.graph_value.unreadable_evidence_ids == ()


class TestDescriptiveKinds:
    """A kind that recommends nothing still reports every measure."""

    async def test_descriptive_only_reports_and_recommends_nothing(self, tmp_path: Path):
        observed = await _observe(replace(_Scenario(), kind="descriptive_only"), tmp_path)

        assert observed.verdict == "reported"
        assert StrategyRejection.KIND_RECOMMENDS_NOTHING in observed.reasons
        assert observed.graph_status == "measured"
