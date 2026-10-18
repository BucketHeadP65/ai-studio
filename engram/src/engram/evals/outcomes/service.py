"""The outcome-evaluation use cases that read delivered evidence back: the strategy comparison and the layered metrics.

Each takes core's `DeliveredEvidenceReader` as an argument and never rereads a live source.
`assess_strategy_comparison` first holds the two recorded run identities to the plan: both arms agree on
model, model version, prompt, conditions and grader, their implementation fingerprints differ, and the
declared model, model version, grader and dataset are the ones recorded.
A run that fails any of that is rejected on that alone, and a comparison the general kernel rejected never reads accepted.
A graph credit needs a declared graph-eligible case, a payload a graph call delivered in that exact trial,
a claim the grader marked supported that rests on it, and a source in it the baseline never surfaced.
A payload the reader refuses earns nothing and makes graph value `unknown`.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import TYPE_CHECKING

from core.ai.evaluation.contracts import DeliveredEvidenceError, TaskExecution, Variant
from core.ai.retrieval.contracts import SearchResult

from engram.evals.outcomes.domain.comparison import (
    GRAPH_TOOLS,
    RESOURCE_MEASURES,
    GradedTrial,
    GraphEvidenceCredit,
    GraphIncrementalValue,
    MeasuredCostComparison,
    ResourceComparison,
    ResourceMeasure,
    ResourceRatio,
    StrategyArm,
    StrategyComparisonPlan,
    StrategyComparisonReport,
    StrategyRejection,
    objective_reading,
)
from engram.evals.outcomes.domain.evaluators import (
    DEFAULT_KS,
    Granularity,
    LayeredEvidenceMetrics,
    MetricStatus,
    SearchCallRanking,
    SearchRankingMetrics,
    bare_unit_id,
    citation_source_metrics,
    normalize_refs,
    page_of,
    ranked_metrics,
    surfaced_source_metrics,
)
from engram.evals.shared.contracts import SEARCH
from engram.evals.tuning.contracts import resolve_strategy

if TYPE_CHECKING:
    from collections.abc import Collection, Mapping, Sequence

    from core.ai.evaluation.contracts import (
        Assessment,
        ComparisonReport,
        DeliveredEvidenceReader,
        DeliveredEvidenceRef,
        ExecutionTrace,
        OutcomeTrial,
        RunIdentity,
        ToolCallTrace,
    )
    from core.ai.retrieval.contracts import SearchHit

    from engram.evals.outcomes.domain.answers import ClaimAssessment

_RATIO_EPSILON = 1e-9
_GRADE_RANK: dict[str, int] = {"fail": 0, "not_applicable": 1, "unknown": 1, "pass": 2}


@dataclass(frozen=True)
class _CandidateTrial:
    """One candidate trial reduced to its graph deliveries, every ok delivery, its claims and the baseline's pages."""

    case_id: str
    repetition: int
    graph_delivery: Mapping[tuple[str, str], tuple[ToolCallTrace, DeliveredEvidenceRef]]
    delivered: frozenset[tuple[str, str]]
    claims: tuple[ClaimAssessment, ...]
    baseline_pages: frozenset[str]


@dataclass(frozen=True)
class _TrialCredits:
    """One candidate trial's graph outcome: what it earned, and what its record could not account for."""

    earned: tuple[GraphEvidenceCredit, ...]
    unreadable: tuple[str, ...]
    unmatched: tuple[str, ...]


def _trace_of[ExecutionT: TaskExecution](trial: OutcomeTrial[ExecutionT]) -> ExecutionTrace | None:
    """The trial's execution trace, `None` when it ran untraced or never produced an execution."""
    return trial.execution.trace if trial.execution is not None else None


def _page(ref: str, collection: str) -> str:
    """The page `ref` names, off its collection qualification and off any claim anchor."""
    return page_of(bare_unit_id(ref, collection))


def _baseline_pages[ExecutionT: TaskExecution](trials: Sequence[OutcomeTrial[ExecutionT]], collection: str) -> dict[str, frozenset[str]]:
    """Every page the baseline surfaced, per case, from the references its ok tool calls returned."""
    pages: dict[str, set[str]] = {}
    for trial in trials:
        trace = _trace_of(trial)
        if trial.variant != "baseline" or trace is None:
            continue
        seen = pages.setdefault(trial.case_id, set())
        seen.update(_page(ref, collection) for call in trace.tool_calls if call.status == "ok" for ref in call.returned_unit_refs)
    return {case_id: frozenset(seen) for case_id, seen in pages.items()}


def _graph_delivery(trace: ExecutionTrace) -> dict[tuple[str, str], tuple[ToolCallTrace, DeliveredEvidenceRef]]:
    """Every payload a graph call delivered in this trial, keyed by the identity a claim cites it under."""
    return {
        (ref.evidence_id, ref.payload_sha256): (call, ref)
        for call in trace.tool_calls
        if call.tool_name in GRAPH_TOOLS and call.status == "ok"
        for ref in call.delivered_evidence
    }


def _delivered_keys(trace: ExecutionTrace) -> frozenset[tuple[str, str]]:
    """The identity of every payload any ok tool call delivered in this trial."""
    return frozenset(
        (ref.evidence_id, ref.payload_sha256) for call in trace.tool_calls if call.status == "ok" for ref in call.delivered_evidence
    )


async def _trial_credits(trial: _CandidateTrial, reader: DeliveredEvidenceReader, collection: str) -> _TrialCredits:
    """What one candidate trial's supported claims earned from its own graph calls, a citation no call delivered reported as unmatched."""
    earned: list[GraphEvidenceCredit] = []
    unreadable: list[str] = []
    unmatched: list[str] = []
    for claim in trial.claims:
        if claim.support.status != "pass":
            continue
        for cited in claim.evidence:
            key = (cited.evidence_id, cited.payload_sha256)
            delivered = trial.graph_delivery.get(key)
            if delivered is None:
                if key not in trial.delivered:
                    unmatched.append(cited.evidence_id)
                continue
            call, ref = delivered
            try:
                await reader.read(ref)
            except DeliveredEvidenceError:
                unreadable.append(ref.evidence_id)
                continue
            sources = tuple(source.source_ref for source in ref.sources)
            reached = {_page(source, collection) for source in sources}
            earned.append(
                GraphEvidenceCredit(
                    case_id=trial.case_id,
                    repetition=trial.repetition,
                    tool_name=call.tool_name,
                    evidence_id=ref.evidence_id,
                    claim=claim.claim,
                    sources=sources,
                    baseline_surfaced=not (reached - trial.baseline_pages),
                )
            )
    return _TrialCredits(earned=tuple(earned), unreadable=tuple(unreadable), unmatched=tuple(unmatched))


async def _graph_value[ExecutionT: TaskExecution](
    plan: StrategyComparisonPlan,
    comparison: ComparisonReport[ExecutionT],
    grades: Mapping[tuple[str, int, Variant], GradedTrial],
    reader: DeliveredEvidenceReader,
    collection: str,
) -> GraphIncrementalValue:
    """What the candidate's graph calls contributed on the plan's declared eligible cases."""
    if not GRAPH_TOOLS & resolve_strategy(plan.candidate.strategy.value).tools:
        return GraphIncrementalValue(status="not_applicable")
    eligible = frozenset(plan.graph_eligible_case_ids)
    baseline_pages = _baseline_pages(comparison.trials, collection)
    earned: list[GraphEvidenceCredit] = []
    unreadable: list[str] = []
    unmatched: list[str] = []
    for trial in comparison.trials:
        trace = _trace_of(trial)
        grade = grades.get((trial.case_id, trial.repetition, trial.variant))
        if trial.variant != "candidate" or trial.case_id not in eligible or trace is None or grade is None or grade.answer is None:
            continue
        outcome = await _trial_credits(
            _CandidateTrial(
                case_id=trial.case_id,
                repetition=trial.repetition,
                graph_delivery=_graph_delivery(trace),
                delivered=_delivered_keys(trace),
                claims=grade.answer.claims,
                baseline_pages=baseline_pages.get(trial.case_id, frozenset()),
            ),
            reader,
            collection,
        )
        earned.extend(outcome.earned)
        unreadable.extend(outcome.unreadable)
        unmatched.extend(outcome.unmatched)
    return GraphIncrementalValue(
        status="unknown" if unreadable or unmatched else "measured",
        credits=tuple(earned),
        unreadable_evidence_ids=tuple(unreadable),
        unmatched_evidence_ids=tuple(unmatched),
    )


def _measure(trace: ExecutionTrace, measure: ResourceMeasure) -> int:
    """One trial's spend on `measure`, read from its own trace."""
    if measure == "input_tokens":
        return sum(turn.input_tokens for turn in trace.model_turns)
    if measure == "delivered_bytes":
        return sum(call.response_bytes for call in trace.tool_calls)
    if measure == "requests":
        return len(trace.model_turns)
    return trace.run_duration_ms


def _means[ExecutionT: TaskExecution](trials: Sequence[OutcomeTrial[ExecutionT]], measure: ResourceMeasure) -> float | None:
    """The mean spend on `measure` over `trials`, `None` when any of them went untraced."""
    traces = [_trace_of(trial) for trial in trials]
    if not traces or any(trace is None for trace in traces):
        return None
    return sum(_measure(trace, measure) for trace in traces if trace is not None) / len(traces)


def _ratio_row(case_id: str, measure: ResourceMeasure, means: Mapping[Variant, float | None], allowance: float) -> ResourceRatio:
    """One case's ratio on one measure, unknown whenever either side could not be measured."""
    baseline, candidate = means["baseline"], means["candidate"]
    if baseline is None or candidate is None:
        return ResourceRatio(case_id=case_id, measure=measure, status="unknown")
    if baseline == 0:
        return ResourceRatio(
            case_id=case_id, measure=measure, status="measured", baseline_mean=baseline, candidate_mean=candidate, within_allowance=candidate == 0
        )
    ratio = candidate / baseline
    return ResourceRatio(
        case_id=case_id,
        measure=measure,
        status="measured",
        baseline_mean=baseline,
        candidate_mean=candidate,
        ratio=ratio,
        within_allowance=ratio <= 1 + allowance + _RATIO_EPSILON,
    )


def _resources[ExecutionT: TaskExecution](plan: StrategyComparisonPlan, comparison: ComparisonReport[ExecutionT]) -> ResourceComparison:
    """Each case's candidate-to-baseline ratios on the four measured resources."""
    allowance = plan.thresholds.resource_budget_pct
    rows: list[ResourceRatio] = []
    for case_id in comparison.plan.evaluation_case_ids:
        by_variant: dict[Variant, list[OutcomeTrial[ExecutionT]]] = {"baseline": [], "candidate": []}
        for trial in comparison.trials:
            if trial.case_id == case_id:
                by_variant[trial.variant].append(trial)
        rows.extend(
            _ratio_row(case_id, measure, {variant: _means(trials, measure) for variant, trials in by_variant.items()}, allowance)
            for measure in RESOURCE_MEASURES
        )
    status: MetricStatus = "not_applicable"
    if rows:
        status = "unknown" if any(row.status == "unknown" for row in rows) else "measured"
    return ResourceComparison(status=status, allowance_pct=allowance, ratios=tuple(rows))


def _measured_cost[ExecutionT: TaskExecution](comparison: ComparisonReport[ExecutionT]) -> MeasuredCostComparison:
    """What the two arms' model turns charged, unknown as soon as one turn or one trace is missing a price."""
    totals: dict[Variant, Decimal] = {"baseline": Decimal(0), "candidate": Decimal(0)}
    turns = 0
    for trial in comparison.trials:
        trace = _trace_of(trial)
        if trace is None:
            return MeasuredCostComparison(status="unknown")
        for turn in trace.model_turns:
            turns += 1
            if turn.cost_usd is None:
                return MeasuredCostComparison(status="unknown")
            totals[trial.variant] += turn.cost_usd
    if turns == 0:
        return MeasuredCostComparison(status="not_applicable")
    return MeasuredCostComparison(status="measured", baseline_usd=totals["baseline"], candidate_usd=totals["candidate"])


def _contaminated[ExecutionT: TaskExecution](comparison: ComparisonReport[ExecutionT]) -> int:
    """How many trials named a withheld reference in a tool call's own arguments."""
    return sum(1 for trial in comparison.trials if (trace := _trace_of(trial)) is not None and trace.withheld_exposures)


def _answer_regressions[ExecutionT: TaskExecution](
    comparison: ComparisonReport[ExecutionT], grades: Mapping[tuple[str, int, Variant], GradedTrial]
) -> tuple[str, ...]:
    """Every case whose candidate answer graded worse than its baseline in any one repetition."""
    regressed: list[str] = []
    for case_id in comparison.plan.evaluation_case_ids:
        for repetition in range(comparison.plan.repetitions):
            ranks = {variant: _overall_rank(grades.get((case_id, repetition, variant))) for variant in ("baseline", "candidate")}
            if ranks["candidate"] < ranks["baseline"] and case_id not in regressed:
                regressed.append(case_id)
    return tuple(regressed)


def _overall_rank(grade: GradedTrial | None) -> int:
    """How good one slot's overall answer verdict was, with a missing verdict ranked as unknown."""
    if grade is None or grade.answer is None:
        return _GRADE_RANK["unknown"]
    return _GRADE_RANK[grade.answer.overall.status]


def _unsupported_critical_claims(grades: Mapping[tuple[str, int, Variant], GradedTrial]) -> int:
    """How many critical claims the candidate made that the grader did not mark supported."""
    return sum(
        1
        for (_, _, variant), grade in grades.items()
        if variant == "candidate" and grade.answer is not None
        for claim in grade.answer.claims
        if claim.critical and claim.support.status != "pass"
    )


def _trial_rejections[ExecutionT: TaskExecution](
    plan: StrategyComparisonPlan, comparison: ComparisonReport[ExecutionT], contaminated: int
) -> list[StrategyRejection]:
    """What the paired trials themselves say against accepting the candidate."""
    thresholds = plan.thresholds
    reasons: list[StrategyRejection] = []
    if comparison.plan.repetitions < thresholds.repetitions:
        reasons.append(StrategyRejection.TOO_FEW_REPETITIONS)
    expected = len(comparison.plan.evaluation_case_ids) * comparison.plan.repetitions * 2
    if len(comparison.trials) != expected:
        reasons.append(StrategyRejection.INCOMPLETE_REPETITIONS)
    if sum(1 for trial in comparison.trials if trial.failure is not None) > thresholds.max_failed_trials:
        reasons.append(StrategyRejection.FAILED_TRIALS)
    if contaminated > thresholds.max_contaminated_trials:
        reasons.append(StrategyRejection.CONTAMINATED_TRIALS)
    if thresholds.require_strict_task_gain and comparison.candidate_success_rate <= comparison.baseline_success_rate:
        reasons.append(StrategyRejection.NO_STRICT_TASK_GAIN)
    return reasons


def _grade_rejections[ExecutionT: TaskExecution](
    plan: StrategyComparisonPlan,
    comparison: ComparisonReport[ExecutionT],
    grades: Mapping[tuple[str, int, Variant], GradedTrial],
    unsupported: int,
) -> list[StrategyRejection]:
    """What the independent grades say against accepting the candidate."""
    thresholds = plan.thresholds
    reasons: list[StrategyRejection] = []
    graded = tuple(trial for trial in comparison.trials if trial.failure is None)
    if any(_overall_rank(grades.get((trial.case_id, trial.repetition, trial.variant))) == _GRADE_RANK["unknown"] for trial in graded):
        reasons.append(StrategyRejection.UNKNOWN_REQUIRED_GRADE)
    if unsupported > thresholds.max_unsupported_critical_claims:
        reasons.append(StrategyRejection.UNSUPPORTED_CRITICAL_CLAIMS)
    candidate_grades = tuple(grade for (_, _, variant), grade in grades.items() if variant == "candidate")
    recalls = tuple(grade.required_source_recall for grade in candidate_grades)
    if thresholds.min_required_source_recall > 0 and any(recall is None for recall in recalls):
        reasons.append(StrategyRejection.REQUIRED_SOURCE_RECALL_UNKNOWN)
    if any(recall is not None and recall < thresholds.min_required_source_recall for recall in recalls):
        reasons.append(StrategyRejection.REQUIRED_SOURCE_RECALL_BELOW_THRESHOLD)
    if thresholds.require_workflow_design_pass and any(_design_unmet(grade.workflow_design) for grade in candidate_grades):
        reasons.append(StrategyRejection.WORKFLOW_DESIGN_NOT_PASSED)
    if thresholds.require_all_no_answer_abstentions and any(_abstention_unmet(grade) for grade in candidate_grades):
        reasons.append(StrategyRejection.MISSING_ABSTENTION)
    return reasons


def _design_unmet(design: Assessment | None) -> bool:
    """Whether a workflow-design dimension failed, went unknown, or was never graded at all."""
    return design is None or design.status in ("fail", "unknown")


def _abstention_unmet(grade: GradedTrial) -> bool:
    """Whether a trial the criterion asked to abstain did anything other than abstain."""
    if grade.answer is None:
        return False
    return grade.answer.abstention.status not in ("pass", "not_applicable")


def _held_conditions(identity: RunIdentity) -> tuple[str, str, str, str, str]:
    """Everything a strategy comparison holds equal, as the run itself recorded it."""
    return (
        identity.model,
        identity.model_version,
        identity.prompt_fingerprint,
        identity.conditions_fingerprint,
        identity.grader_fingerprint,
    )


def _declared_conditions(arm: StrategyArm) -> tuple[str, str, str]:
    """The conditions a plan's arm declares that a run identity records under the same names."""
    return (arm.model, arm.model_version, arm.grader_fingerprint)


def _recorded_conditions(identity: RunIdentity) -> tuple[str, str, str]:
    """Those same three conditions, as the run recorded them."""
    return (identity.model, identity.model_version, identity.grader_fingerprint)


def _isolation_rejections[ExecutionT: TaskExecution](
    plan: StrategyComparisonPlan, comparison: ComparisonReport[ExecutionT]
) -> list[StrategyRejection]:
    """Whether the recorded run is the run the plan declared, varying only the retrieval strategy."""
    baseline, candidate = comparison.baseline, comparison.candidate
    reasons: list[StrategyRejection] = []
    if _held_conditions(baseline) != _held_conditions(candidate):
        reasons.append(StrategyRejection.RUN_CONDITIONS_DIFFER)
    if baseline.implementation_fingerprint == candidate.implementation_fingerprint:
        reasons.append(StrategyRejection.IMPLEMENTATION_UNCHANGED)
    if (
        _declared_conditions(plan.baseline) != _recorded_conditions(baseline)
        or _declared_conditions(plan.candidate) != _recorded_conditions(candidate)
        or plan.baseline.dataset_fingerprint != comparison.dataset_fingerprint
    ):
        reasons.append(StrategyRejection.PLAN_DOES_NOT_MATCH_RUN)
    return reasons


def _graph_rejections(plan: StrategyComparisonPlan, graph_value: GraphIncrementalValue) -> list[StrategyRejection]:
    """What the graph question says against accepting the candidate, on a plan that asks it."""
    if plan.kind != "graph_incremental_value":
        return [StrategyRejection.KIND_RECOMMENDS_NOTHING]
    reasons: list[StrategyRejection] = []
    if not plan.graph_eligible_case_ids:
        reasons.append(StrategyRejection.NO_GRAPH_ELIGIBLE_CASES)
    if graph_value.status == "unknown":
        reasons.append(StrategyRejection.GRAPH_VALUE_UNKNOWN)
    elif not graph_value.novel_credits:
        reasons.append(StrategyRejection.NO_GRAPH_EVIDENCE)
    return reasons


def _gate_rejections(
    plan: StrategyComparisonPlan, graph_value: GraphIncrementalValue, resources: ResourceComparison, regressions: int
) -> list[StrategyRejection]:
    """What the declared gate says against accepting the candidate."""
    thresholds = plan.thresholds
    reasons: list[StrategyRejection] = []
    if regressions > thresholds.max_task_regressions:
        reasons.append(StrategyRejection.TASK_REGRESSIONS)
    reasons.extend(_graph_rejections(plan, graph_value))
    if resources.unmeasured:
        reasons.append(StrategyRejection.RESOURCE_COMPARISON_UNKNOWN)
    if resources.exceeded:
        reasons.append(StrategyRejection.RESOURCE_BUDGET_EXCEEDED)
    if plan.measured_dataset_staleness is None:
        reasons.append(StrategyRejection.DATASET_STALENESS_UNKNOWN)
    elif plan.measured_dataset_staleness > thresholds.max_dataset_staleness:
        reasons.append(StrategyRejection.DATASET_STALENESS_ABOVE_THRESHOLD)
    if plan.objective != "task_success":
        reasons.append(StrategyRejection.OBJECTIVE_NOT_DECIDED_HERE)
    return reasons


async def assess_strategy_comparison[ExecutionT: TaskExecution](
    plan: StrategyComparisonPlan,
    comparison: ComparisonReport[ExecutionT],
    *,
    grades: tuple[GradedTrial, ...],
    reader: DeliveredEvidenceReader,
    collection: str,
) -> StrategyComparisonReport[ExecutionT]:
    """Fold one strategy comparison's trials, grades and traces into its complete result.

    Args:
        plan: The declared comparison, already held to varying only the retrieval strategy.
        comparison: The paired-trial report the two arms produced.
        grades: The independent grades answering the trial slots, at most one per slot.
        reader: Reads back the exact payload a graph call delivered, refusing anything else.
        collection: The knowledge base both arms read, which is how a reference resolves to a page.

    Returns:
        The report: every measure, every reason standing against acceptance, and the verdict.
        `accepted` only for a graph incremental-value plan on the task-success objective with
        nothing standing against it. A run whose recorded identities do not match the plan, or whose
        two arms recorded the same implementation, is rejected on that alone with no threshold
        graded. A comparison the general kernel rejected carries its own reasons through
        `comparison_reasons` and can never read accepted.

    Raises:
        ValueError: Two grades claim one trial slot, or a grade names a slot the comparison does
            not hold.
    """
    slots = {(trial.case_id, trial.repetition, trial.variant) for trial in comparison.trials}
    by_slot: dict[tuple[str, int, Variant], GradedTrial] = {}
    for grade in grades:
        slot = (grade.case_id, grade.repetition, grade.variant)
        if slot in by_slot:
            msg = f"two grades claim the trial slot {slot}"
            raise ValueError(msg)
        if slot not in slots:
            msg = f"the grade for {slot} names no trial in this comparison"
            raise ValueError(msg)
        by_slot[slot] = grade
    graph_value = await _graph_value(plan, comparison, by_slot, reader, collection)
    resources = _resources(plan, comparison)
    contaminated = _contaminated(comparison)
    unsupported = _unsupported_critical_claims(by_slot)
    regressed = tuple(dict.fromkeys((*comparison.regressions, *_answer_regressions(comparison, by_slot))))
    core = (StrategyRejection.CORE_COMPARISON_REJECTED,) if comparison.verdict == "rejected" else ()
    isolation = _isolation_rejections(plan, comparison)
    graded = (
        *_trial_rejections(plan, comparison, contaminated),
        *_grade_rejections(plan, comparison, by_slot, unsupported),
        *_gate_rejections(plan, graph_value, resources, len(regressed)),
    )
    reasons = (*core, *isolation) if isolation else (*core, *graded)
    decidable = plan.kind == "graph_incremental_value" and plan.objective == "task_success"
    return StrategyComparisonReport[ExecutionT](
        plan=plan,
        comparison=comparison,
        objective=objective_reading(plan.objective),
        verdict=("rejected" if reasons else "accepted") if decidable else "reported",
        reasons=tuple(dict.fromkeys(reasons)),
        graph_value=graph_value,
        resources=resources,
        measured_generative_model_cost=_measured_cost(comparison),
        comparison_reasons=comparison.reasons,
        failed_trials=sum(1 for trial in comparison.trials if trial.failure is not None),
        contaminated_trials=contaminated,
        regressed_case_ids=regressed,
        unsupported_critical_claims=unsupported,
    )


def _hit_ref(hit: SearchHit) -> str:
    """A hit's own bare id, the vocabulary `expected_source_ids` and `cited_source_ids` share."""
    return hit.id


async def _search_call_ranking(
    call: ToolCallTrace, reader: DeliveredEvidenceReader, want: Collection[str], *, ks: tuple[int, ...], granularity: Granularity
) -> SearchCallRanking:
    """One search call's ranking from its stored payload, `unknown` when the call is not ok, stored nothing or is unreadable."""
    if call.status != "ok" or not call.delivered_evidence:
        return SearchCallRanking(ordinal=call.ordinal, status="unknown")
    try:
        evidence = await reader.read(call.delivered_evidence[0])
    except DeliveredEvidenceError:
        return SearchCallRanking(ordinal=call.ordinal, status="unknown")
    result = SearchResult.model_validate_json(evidence.payload_json)
    got = normalize_refs([_hit_ref(hit) for hit in result.hits], granularity)
    return SearchCallRanking(ordinal=call.ordinal, status="measured", metrics=ranked_metrics(got, want, ks=ks))


async def search_ranking_metrics(
    tool_calls: Sequence[ToolCallTrace],
    reader: DeliveredEvidenceReader,
    expected: Collection[str],
    *,
    ks: tuple[int, ...] = DEFAULT_KS,
    granularity: Granularity = "page",
) -> SearchRankingMetrics:
    """Score each `search` call's own ordered hits, parsed from its delivered-evidence payload.

    Args:
        tool_calls: The trial's own tool calls, in any order (each call's own `ordinal` is preserved
            on its `SearchCallRanking`, so a caller can restore call order itself).
        reader: Reads back the exact payload a call's `DeliveredEvidenceRef` names.
        expected: The expected ids (`ExpectedQa.expected_source_ids`).
        ks: The recall/precision cutoffs to report.
        granularity: `"page"` (the default) collapses a hit to its page. `"unit"` scores the exact
            hit id.

    Returns:
        `SearchRankingMetrics(calls=())` when the trial made no `search` call, otherwise one
        `SearchCallRanking` per `search` call, in the trial's own call order.
    """
    search_calls = [call for call in tool_calls if call.tool_name == SEARCH]
    if not search_calls:
        return SearchRankingMetrics()
    want = set(normalize_refs(tuple(expected), granularity))
    calls = tuple([await _search_call_ranking(call, reader, want, ks=ks, granularity=granularity) for call in search_calls])
    return SearchRankingMetrics(calls=calls)


async def layered_evidence_metrics(
    *,
    cited: Sequence[str],
    expected: Collection[str],
    trace: ExecutionTrace,
    reader: DeliveredEvidenceReader,
    collection: str,
    ks: tuple[int, ...] = DEFAULT_KS,
    granularity: Granularity = "page",
) -> LayeredEvidenceMetrics:
    """Score one trial's citation, surfaced-source and raw-ranking layers together.

    You must pass the trial's trace and a reader over the store its evidence was written to, so call it
    once the trial has finished, keyed by the trial id.

    Args:
        cited: The agent's ordered citations (`RetrievalAnswer.cited_source_ids`).
        expected: The expected ids (`ExpectedQa.expected_source_ids`).
        trace: The trial's `ExecutionTrace`.
        reader: Reads back a search call's stored payload.
        collection: The knowledge base the trial ran against.
        ks: The recall/precision cutoffs to report.
        granularity: `"page"` (the default) or `"unit"`, applied to every layer alike.

    Returns:
        The trial's `LayeredEvidenceMetrics`.
    """
    return LayeredEvidenceMetrics(
        citation=citation_source_metrics(cited, expected, ks=ks, granularity=granularity),
        surfaced=surfaced_source_metrics(trace.tool_calls, expected, collection=collection, ks=ks, granularity=granularity),
        ranking=await search_ranking_metrics(trace.tool_calls, reader, expected, ks=ks, granularity=granularity),
    )
