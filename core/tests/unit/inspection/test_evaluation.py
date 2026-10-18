from __future__ import annotations

import pytest

from pydantic import ValidationError

from core.ai.evaluation.contracts import ComparisonPlan, EvaluationError, ResourceUsage, TaskDataset, compare_outcomes
from core.ai.inspection import contracts as inspection_contracts
from core.ai.inspection.domain.evaluation import (
    DiagnosticCase,
    DiagnosticDataset,
    DiagnosticPrediction,
    DiagnosticReport,
    DiagnosticResponse,
    evaluate_diagnostics,
)
from core.ai.inspection.domain.evaluation import TaskExecution as InspectionTaskExecution
from tests.support.fakes.inspection_evaluation import (
    PROVENANCE,
    FakeDiagnosticPredictor,
    FakeTaskGrader,
    FakeTaskRunner,
    identity,
    task_case,
)


def test_inspection_execution_keeps_symptom_count() -> None:
    execution = InspectionTaskExecution(output="finding", symptom_count=2)
    assert execution.symptom_count == 2


def test_inspection_execution_refuses_a_negative_symptom_count() -> None:
    with pytest.raises(ValidationError, match="symptom_count"):
        InspectionTaskExecution(output="finding", symptom_count=-1)


def diagnostic_case(case_id: str, expected: str = "issue") -> DiagnosticCase:
    return DiagnosticCase.model_validate(
        {
            "case_id": case_id,
            "input": f"transcript {case_id}",
            "split": "test",
            "provenance": PROVENANCE,
            "expected": expected,
            "expected_symptoms": ("retry_loop",) if expected == "issue" else (),
            "supported_causes": ("missing_instructions",) if expected == "issue" else (),
        }
    )


async def test_diagnostic_metrics_include_false_positives_misses_and_unjustified_causes() -> None:
    dataset = DiagnosticDataset(
        name="diagnostics", version="1", cases=(diagnostic_case("hit"), diagnostic_case("miss"), diagnostic_case("healthy", "clean"))
    )
    issue = DiagnosticPrediction(verdict="issue", symptoms=("retry_loop",), causes=("wrong_model",), reason="hypothesis")
    clean = DiagnosticPrediction(verdict="clean", reason="healthy")
    predictor = FakeDiagnosticPredictor({"hit": issue, "miss": clean, "healthy": issue})

    report = await evaluate_diagnostics(dataset, predictor, identity=identity("baseline"), repetitions=2)

    assert report.metrics.precision == 0.5
    assert report.metrics.recall == 0.5
    assert report.metrics.cause_accuracy == 0
    assert report.metrics.false_positives == 2
    assert report.metrics.false_negatives == 2
    assert len(report.trials) == 6
    assert report.review_counts == {"agent": 3}
    assert report.owner_calibrated is False
    assert all(set(type(case).model_fields) == {"case_id", "input"} for case in predictor.inputs)
    assert report.model_dump()["dataset_fingerprint"] == dataset.fingerprint


async def test_abstention_is_distinct_from_a_clean_verdict() -> None:
    dataset = DiagnosticDataset(
        name="uncertainty", version="1", cases=(diagnostic_case("uncertain", "uncertain"), diagnostic_case("healthy", "clean"))
    )
    predictor = FakeDiagnosticPredictor(
        {
            "uncertain": DiagnosticPrediction(verdict="abstain", reason="insufficient evidence"),
            "healthy": DiagnosticPrediction(verdict="abstain", reason="unsure"),
        }
    )
    report = await evaluate_diagnostics(dataset, predictor, identity=identity("baseline"), repetitions=2)

    assert report.metrics.abstention_rate == 1
    assert report.metrics.correct_abstentions == 2
    assert report.metrics.clean_accuracy == 0
    assert report.metrics.verdict_accuracy == 0.5
    assert report.metrics.precision is None
    assert report.metrics.cause_accuracy is None


async def test_diagnostic_failure_is_not_abstention_and_preserves_usage() -> None:
    dataset = DiagnosticDataset(name="failures", version="1", cases=(diagnostic_case("failed"),))
    predictor = FakeDiagnosticPredictor({"failed": EvaluationError("provider unavailable", usage=ResourceUsage(cost_usd=0.2))})

    report = await evaluate_diagnostics(dataset, predictor, identity=identity("baseline"), repetitions=2)

    assert report.metrics.failures == 2
    assert report.metrics.abstention_rate == 0
    assert report.metrics.recall == 0
    assert report.usage.cost_usd == 0.4
    assert all(trial.failure == "provider unavailable" for trial in report.trials)


def test_dataset_refuses_duplicate_ids_and_inconsistent_labels() -> None:
    case = diagnostic_case("duplicate")
    with pytest.raises(ValidationError, match="duplicate"):
        DiagnosticDataset(name="duplicate", version="1", cases=(case, case))
    with pytest.raises(ValidationError, match="symptoms"):
        DiagnosticCase(case_id="bad", input="healthy", split="test", provenance=PROVENANCE, expected="clean", expected_symptoms=("loop",))


def comparison_plan() -> ComparisonPlan:
    return ComparisonPlan(
        dataset=TaskDataset(name="outcomes", version="1", cases=(task_case("train", "train"), task_case("positive"), task_case("healthy"))),
        optimization_case_ids=("train",),
        evaluation_case_ids=("positive", "healthy"),
        repetitions=2,
    )


async def test_symptom_disappearance_cannot_mask_a_task_regression() -> None:
    runner = FakeTaskRunner({("baseline", "positive"): "success", ("baseline", "healthy"): "success"})
    report = await compare_outcomes(
        comparison_plan(), runner, FakeTaskGrader(), baseline=identity("baseline"), candidate=identity("candidate")
    )

    assert report.verdict == "rejected"
    assert report.baseline_success_rate == 1
    assert report.candidate_success_rate == 0
    assert report.regressions == ("positive", "healthy")
    candidate_executions = (trial.execution for trial in report.trials if trial.variant == "candidate")
    assert sum(execution.symptom_count for execution in candidate_executions if execution is not None) == 0


def test_model_and_dataset_fingerprints_are_immutable_and_content_sensitive() -> None:
    dataset = DiagnosticDataset(name="hash", version="1", cases=(diagnostic_case("one"),))
    assert dataset.fingerprint == DiagnosticDataset.model_validate_json(dataset.model_dump_json()).fingerprint
    changed = DiagnosticDataset(name="hash", version="2", cases=dataset.cases)
    assert changed.fingerprint != dataset.fingerprint
    with pytest.raises(ValidationError, match="frozen"):
        dataset.version = "3"
    with pytest.raises(ValidationError):
        DiagnosticResponse(prediction=DiagnosticPrediction(verdict="clean", symptoms=("loop",), reason="contradictory"))


@pytest.mark.parametrize("field", ["owner_calibrated", "metrics", "trials", "dataset_fingerprint"])
async def test_diagnostic_artifacts_validate_scores_coverage_and_review_authority(field: str) -> None:
    dataset = DiagnosticDataset(name="validation", version="1", cases=(diagnostic_case("case"),))
    predictor = FakeDiagnosticPredictor({"case": DiagnosticPrediction(verdict="clean", reason="missed issue")})
    report = await evaluate_diagnostics(dataset, predictor, identity=identity("baseline"), repetitions=2)
    payload = report.model_dump()
    if field == "owner_calibrated":
        payload[field] = True
    elif field == "metrics":
        payload[field]["recall"] = 1
    elif field == "trials":
        payload[field] = payload[field][:-1]
    else:
        payload[field] = "0" * 64
    with pytest.raises(ValidationError, match="report"):
        DiagnosticReport.model_validate(payload)


def test_the_inspection_contracts_name_no_kernel_error_under_another_name() -> None:
    assert "EvaluationFailure" not in inspection_contracts.__all__
    assert not hasattr(inspection_contracts, "EvaluationFailure")
