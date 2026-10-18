from __future__ import annotations

import pytest

from pydantic import ValidationError

from core.ai.evaluation.contracts import (
    ComparisonPlan,
    DiagnosticObservation,
    EvaluationError,
    ResourceUsage,
    TaskDataset,
    TaskExecution,
    TaskGrade,
    assess_comparison,
    compare_outcomes,
)
from tests.support.fakes.evaluation import FakeTaskGrader, FakeTaskRunner, identity, task_case


def test_general_execution_has_no_inspection_field() -> None:
    execution = TaskExecution(
        output="workflow draft",
        usage=ResourceUsage(input_tokens=10),
        diagnostics=(DiagnosticObservation(code="budget", detail="within limit"),),
    )
    assert "symptom_count" not in type(execution).model_fields
    assert execution.diagnostics[0].code == "budget"


def test_general_execution_refuses_an_unrecognized_key() -> None:
    with pytest.raises(ValidationError, match="symptom_count"):
        TaskExecution.model_validate({"output": "workflow draft", "symptom_count": 2})


def comparison_plan() -> ComparisonPlan:
    return ComparisonPlan(
        dataset=TaskDataset(name="outcomes", version="1", cases=(task_case("train", "train"), task_case("positive"), task_case("healthy"))),
        optimization_case_ids=("train",),
        evaluation_case_ids=("positive", "healthy"),
        repetitions=2,
    )


@pytest.mark.parametrize("contamination", ["same_id", "same_content", "training_eval"])
def test_contaminated_optimization_and_evaluation_cases_are_refused(contamination: str) -> None:
    plan = comparison_plan()
    payload = plan.model_dump()
    if contamination == "same_id":
        payload["optimization_case_ids"] = ("positive",)
    elif contamination == "same_content":
        payload["dataset"]["cases"][1]["input"] = payload["dataset"]["cases"][0]["input"]
    else:
        payload["evaluation_case_ids"] = ("train",)
        payload["optimization_case_ids"] = ()
    with pytest.raises(ValidationError, match=r"disjoint|held-out|contaminat"):
        ComparisonPlan.model_validate(payload)


async def test_accepts_task_success_gain_with_no_case_regressions_and_records_conditions() -> None:
    runner = FakeTaskRunner({("candidate", "positive"): "success", ("candidate", "healthy"): "success", ("baseline", "healthy"): "success"})
    report = await compare_outcomes(
        comparison_plan(), runner, FakeTaskGrader(), baseline=identity("baseline"), candidate=identity("candidate")
    )

    assert report.verdict == "accepted"
    assert report.candidate_success_rate == 1
    assert report.baseline_success_rate == 0.5
    assert report.regressions == ()
    assert report.baseline_usage.input_tokens == 40
    assert report.candidate_usage.input_tokens == 40
    assert report.statistical_claim == "descriptive_only"
    assert report.dataset_fingerprint == comparison_plan().dataset.fingerprint
    assert report.model_dump()["dataset_fingerprint"] == comparison_plan().dataset.fingerprint
    assert report == type(report).model_validate_json(report.model_dump_json())


async def test_one_case_regression_rejects_even_an_aggregate_gain() -> None:
    cases = (task_case("a"), task_case("b"), task_case("c"))
    plan = ComparisonPlan(
        dataset=TaskDataset(name="regression", version="1", cases=cases), evaluation_case_ids=("a", "b", "c"), repetitions=2
    )
    runner = FakeTaskRunner({("baseline", "a"): "success", ("candidate", "b"): "success", ("candidate", "c"): "success"})
    report = await compare_outcomes(plan, runner, FakeTaskGrader(), baseline=identity("baseline"), candidate=identity("candidate"))
    assert report.candidate_success_rate > report.baseline_success_rate
    assert report.verdict == "rejected"
    assert report.regressions == ("a",)


async def test_missing_or_duplicate_repetitions_cannot_be_accepted() -> None:
    plan = comparison_plan()
    report = await compare_outcomes(
        plan, FakeTaskRunner({}), FakeTaskGrader(), baseline=identity("baseline"), candidate=identity("candidate")
    )
    incomplete = assess_comparison(plan, baseline=report.baseline, candidate=report.candidate, trials=report.trials[:-1])
    assert incomplete.verdict == "rejected"
    assert "incomplete repetitions" in incomplete.reasons
    with pytest.raises(ValueError, match="duplicate"):
        assess_comparison(plan, baseline=report.baseline, candidate=report.candidate, trials=(*report.trials, report.trials[0]))
    with pytest.raises(ValidationError):
        ComparisonPlan.model_validate({**plan.model_dump(), "repetitions": 1})


async def test_cost_improvement_requires_explicit_objective_matched_success_and_known_cost() -> None:
    runner = FakeTaskRunner({}, baseline_cost=0.2, candidate_cost=0.1)
    plan = comparison_plan()
    usual = await compare_outcomes(plan, runner, FakeTaskGrader(), baseline=identity("baseline"), candidate=identity("candidate"))
    assert usual.verdict == "rejected"
    cost_plan = ComparisonPlan.model_validate({**plan.model_dump(), "objective": "cost"})
    cheaper = await compare_outcomes(cost_plan, runner, FakeTaskGrader(), baseline=identity("baseline"), candidate=identity("candidate"))
    assert cheaper.verdict == "accepted"
    assert cheaper.baseline_usage.cost_usd == 0.8
    assert cheaper.candidate_usage.cost_usd == 0.4
    unknown = await compare_outcomes(
        cost_plan, FakeTaskRunner({}, candidate_cost=None), FakeTaskGrader(), baseline=identity("baseline"), candidate=identity("candidate")
    )
    assert unknown.verdict == "rejected"
    assert unknown.candidate_usage.cost_usd is None


async def test_incomparable_conditions_and_failure_trials_are_refused() -> None:
    baseline = identity("baseline")
    candidate = identity("candidate").model_copy(update={"model": "other"})
    with pytest.raises(ValueError, match="comparable"):
        await compare_outcomes(comparison_plan(), FakeTaskRunner({}), FakeTaskGrader(), baseline=baseline, candidate=candidate)

    runner = FakeTaskRunner({("candidate", "positive"): EvaluationError("timeout", usage=ResourceUsage(cost_usd=0.3))})
    report = await compare_outcomes(comparison_plan(), runner, FakeTaskGrader(), baseline=baseline, candidate=identity("candidate"))
    assert report.verdict == "rejected"
    assert "failed trials" in report.reasons
    assert report.candidate_usage.cost_usd == 0.8


async def test_grader_usage_is_included_in_paired_trial_totals() -> None:
    grader = FakeTaskGrader(usage=ResourceUsage(input_tokens=3, output_tokens=1, requests=1, cost_usd=0.05))
    report = await compare_outcomes(
        comparison_plan(), FakeTaskRunner({}), grader, baseline=identity("baseline"), candidate=identity("candidate")
    )
    assert report.baseline_usage.input_tokens == 52
    assert report.baseline_usage.requests == 8
    assert report.baseline_usage.cost_usd == pytest.approx(0.6)
    assert TaskGrade(success=True, reason="deterministic").usage.cost_usd == 0


async def test_unknown_grader_cost_and_grader_failure_keep_execution_usage() -> None:
    unknown = await compare_outcomes(
        comparison_plan(),
        FakeTaskRunner({}),
        FakeTaskGrader(usage=ResourceUsage(requests=1)),
        baseline=identity("baseline"),
        candidate=identity("candidate"),
    )
    assert unknown.baseline_usage.cost_usd is None
    failed = await compare_outcomes(
        comparison_plan(),
        FakeTaskRunner({}),
        FakeTaskGrader(failure=EvaluationError("grader failure", usage=ResourceUsage(cost_usd=0.05))),
        baseline=identity("baseline"),
        candidate=identity("candidate"),
    )
    assert failed.baseline_usage.input_tokens == 40
    assert failed.baseline_usage.cost_usd == pytest.approx(0.6)
    assert "failed trials" in failed.reasons


async def test_validation_split_is_supported_and_unchanged_candidate_is_rejected() -> None:
    plan = ComparisonPlan(
        dataset=TaskDataset(name="validation", version="1", cases=(task_case("held-out", "validation"),)),
        evaluation_case_ids=("held-out",),
        repetitions=2,
    )
    report = await compare_outcomes(
        plan,
        FakeTaskRunner({("candidate", "held-out"): "success"}),
        FakeTaskGrader(),
        baseline=identity("candidate"),
        candidate=identity("candidate"),
    )
    assert report.verdict == "rejected"
    assert "unchanged candidate" in report.reasons
