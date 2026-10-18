"""Inspection evaluation runs the actual reading surface without exposing expected labels."""

from decimal import Decimal
from pathlib import Path

import pytest

from core.ai.evaluation.contracts import EvaluationError, EvaluationInput, Provenance, TaskCase
from core.ai.inspection.contracts import DiagnosticDataset, DiagnosticPrediction
from core.ai.inspection.domain.evaluation import DiagnosticCase
from core.ai.transcripts.contracts import Step, Transcript
from pydantic import ValidationError
from pydantic_ai.messages import ModelMessage, ModelRequest, ModelResponse, ToolCallPart, ToolReturnPart, UserPromptPart
from pydantic_ai.models.function import AgentInfo, FunctionModel
from pydantic_ai.usage import RequestUsage
from typer.testing import CliRunner

from harness.cli import app
from harness.features.inspection.experiments import DiagnosticOutcomeGrader, OutcomeCriterion, run_comparison
from harness.features.inspection.infrastructure.evaluation import CaseInput, InspectorPredictor, ObservedModel, evidence_transcript


def test_inspection_evaluation_cli_explains_the_comparison_and_requires_repeated_trials() -> None:
    runner = CliRunner()
    help_result = runner.invoke(app, ["evals", "inspection", "--help"])
    assert help_result.exit_code == 0
    assert "--dataset" in help_result.output
    assert "--repetitions" in help_result.output
    assert runner.invoke(app, ["evals", "inspection", "--repetitions", "1"]).exit_code == 2


def a_case() -> CaseInput:
    return CaseInput(
        transcript=Transcript(
            source="record:example",
            steps=(
                Step(
                    messages=(ModelRequest(parts=[UserPromptPart("Explain the task.")], instructions="RECORDED_INSTRUCTION"),),
                    ordinals=(0,),
                ),
            ),
        )
    )


def test_baseline_evidence_ablation_keeps_the_task_and_does_not_mutate_the_source() -> None:
    given = a_case()

    reduced = evidence_transcript(given.transcript, "messages_only")

    assert reduced.steps[0].messages[0].parts == given.transcript.steps[0].messages[0].parts
    assert isinstance(reduced.steps[0].messages[0], ModelRequest)
    assert reduced.steps[0].messages[0].instructions is None
    assert isinstance(given.transcript.steps[0].messages[0], ModelRequest)
    assert given.transcript.steps[0].messages[0].instructions == "RECORDED_INSTRUCTION"


async def test_predictor_reads_recorded_instructions_through_the_real_inspector_tools() -> None:
    returned: list[str] = []

    def reading_model(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        results = [str(part.content) for message in messages for part in message.parts if isinstance(part, ToolReturnPart)]
        if not results:
            return ModelResponse(parts=[ToolCallPart("read", {"step": 0, "resource": "instructions"}, "read-1")])
        returned.extend(results)
        output = next(tool for tool in info.output_tools if tool.name.endswith("NothingSeen"))
        return ModelResponse(parts=[ToolCallPart(output.name, {"reason": "The task was followed."}, "output-1")])

    predictor = InspectorPredictor(FunctionModel(reading_model), evidence="complete")
    case = EvaluationInput(case_id="example", input=a_case().model_dump_json())

    response = await predictor(case, 0)

    assert response.prediction.verdict == "clean"
    assert any("RECORDED_INSTRUCTION" in text for text in returned)
    assert response.usage.requests == 2


async def test_search_continuations_never_expose_answer_bearing_source_ids() -> None:
    returned: list[str] = []

    def searching_model(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        results = [str(part.content) for message in messages for part in message.parts if isinstance(part, ToolReturnPart)]
        if not results:
            return ModelResponse(parts=[ToolCallPart("find", {"text": "Explain"}, "find-1")])
        returned.extend(results)
        tool = next(tool for tool in info.output_tools if tool.name.endswith("NothingSeen"))
        return ModelResponse(parts=[ToolCallPart(tool.name, {"reason": "The evidence was read."})])

    given = a_case()
    given = given.model_copy(update={"transcript": given.transcript.model_copy(update={"source": "fixture:answer-is-clean"})})
    await InspectorPredictor(FunctionModel(searching_model))(EvaluationInput(case_id="expected-clean", input=given.model_dump_json()), 0)

    assert returned
    assert all("answer-is-clean" not in text and "expected-clean" not in text for text in returned)


@pytest.mark.parametrize(("helped", "verdict"), [(True, "accepted"), (False, "rejected")])
async def test_comparison_records_one_improvement_and_reuses_trials_for_diagnostic_metrics(
    tmp_path: Path, helped: bool, verdict: str
) -> None:
    calls: list[str] = []

    def checking_model(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        results = [str(part.content) for message in messages for part in message.parts if isinstance(part, ToolReturnPart)]
        if not results:
            return ModelResponse(parts=[ToolCallPart("read", {"step": 0, "resource": "instructions"}, "read-1")])
        has_evidence = any("RECORDED_INSTRUCTION" in result for result in results)
        name = "NothingSeen" if has_evidence == helped else "ScanUndecided"
        calls.append(name)
        tool = next(tool for tool in info.output_tools if tool.name.endswith(name))
        return ModelResponse(
            parts=[ToolCallPart(tool.name, {"reason": "Instructions checked" if has_evidence else "Instructions missing"})]
        )

    dataset = DiagnosticDataset(
        name="review-fixtures",
        version="1",
        cases=(
            DiagnosticCase(
                case_id="held-out",
                input=a_case().model_dump_json(),
                split="test",
                expected="clean",
                provenance=Provenance(source="unit test", kind="synthetic", review="deterministic", reviewer="scripted model"),
            ),
        ),
    )

    report = await run_comparison(dataset, FunctionModel(checking_model), output=tmp_path, repetitions=2)

    assert report.comparison.verdict == verdict
    assert report.baseline.metrics.verdict_accuracy == int(not helped)
    assert report.candidate.metrics.verdict_accuracy == int(helped)
    assert report.candidate.owner_calibrated is False
    assert len(calls) == 4
    assert list((tmp_path / "learning").glob("*.json"))
    assert list(tmp_path.glob("*/comparison.json"))
    import json

    (summary_path,) = tmp_path.glob("*/summary.json")
    summary = json.loads(summary_path.read_text())
    assert summary["previous_release_comparison"] is False
    assert summary["independent_holdout_verified"] is False
    assert summary["training_performed"] is False
    from core.ai.inspection.contracts import LearningRecord

    (stored,) = (tmp_path / "learning").glob("*.json")
    learning = LearningRecord.model_validate_json(stored.read_text())
    assert [item.stance for item in learning.evidence] == ["supporting" if helped else "contradicting"]
    assert len(learning.attempts) == 1


async def test_outcome_grader_rejects_an_asserted_cause_without_support_in_the_rubric() -> None:
    case = TaskCase(
        case_id="unsupported-cause",
        input="A duplicate call with no recorded cause evidence.",
        split="test",
        success_criterion=OutcomeCriterion(expected="issue", symptoms=("duplicate_call",), causes=()).model_dump_json(),
        provenance=Provenance(source="unit test", kind="synthetic", review="deterministic", reviewer="test"),
    )
    prediction = DiagnosticPrediction(
        verdict="issue", symptoms=("duplicate_call",), causes=("invented_cause",), reason="Unsupported guess."
    )

    grade = await DiagnosticOutcomeGrader()(case, prediction.model_dump_json())

    assert grade.success is False


async def test_timeout_after_a_billed_response_preserves_unknown_total_cost() -> None:
    def times_out(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        if any(isinstance(part, ToolReturnPart) for message in messages for part in message.parts):
            message = "response was not received"
            raise TimeoutError(message)
        return ModelResponse(parts=[ToolCallPart("read", {"step": 0}, "c1")], usage=RequestUsage(cost=Decimal("0.01")))

    with pytest.raises(EvaluationError) as failed:
        await InspectorPredictor(FunctionModel(times_out))(EvaluationInput(case_id="timeout", input=a_case().model_dump_json()), 0)

    assert failed.value.usage.cost_usd is None


async def test_malformed_nested_case_fails_before_any_provider_call(tmp_path: Path) -> None:
    calls: list[int] = []

    def never_called(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        calls.append(1)
        return ModelResponse(parts=[])

    provenance = Provenance(source="unit test", kind="synthetic", review="deterministic", reviewer="test")
    dataset = DiagnosticDataset(
        name="bad-case",
        version="1",
        cases=(
            DiagnosticCase(case_id="first", input=a_case().model_dump_json(), split="test", provenance=provenance, expected="clean"),
            DiagnosticCase(case_id="last", input="{}", split="test", provenance=provenance, expected="clean"),
        ),
    )
    with pytest.raises(ValidationError):
        await run_comparison(dataset, FunctionModel(never_called), output=tmp_path, repetitions=2)

    assert calls == []


def test_observed_model_drift_cannot_be_presented_as_a_frozen_comparison() -> None:
    observed = ObservedModel()
    observed.observe([ModelResponse(parts=[], model_name="provider-revision-1")])
    with pytest.raises(ValueError, match="different model identities"):
        observed.observe([ModelResponse(parts=[], model_name="provider-revision-2")])


def test_taxonomy_changes_change_the_experiment_fingerprint(monkeypatch: pytest.MonkeyPatch) -> None:
    from pydantic_ai.models.test import TestModel

    from harness.features.inspection import experiments

    before, _, before_identity = experiments._identities(TestModel())
    current = experiments.TAXONOMY
    changed = current.model_copy(
        update={"symptoms": (current.symptoms[0].model_copy(update={"what_you_see": "Changed criterion"}), *current.symptoms[1:])}
    )
    monkeypatch.setattr(experiments, "TAXONOMY", changed)

    after, _, after_identity = experiments._identities(TestModel())

    assert before.fingerprint != after.fingerprint
    assert before_identity.conditions_fingerprint != after_identity.conditions_fingerprint


async def test_failed_trials_do_not_become_supporting_learning_evidence(tmp_path: Path) -> None:
    from core.ai.inspection.contracts import LearningRecord

    def unavailable_baseline(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        results = [str(part.content) for message in messages for part in message.parts if isinstance(part, ToolReturnPart)]
        if not results:
            return ModelResponse(parts=[ToolCallPart("read", {"step": 0, "resource": "instructions"}, "r1")])
        if not any("RECORDED_INSTRUCTION" in result for result in results):
            message = "provider response timed out"
            raise TimeoutError(message)
        tool = next(tool for tool in info.output_tools if tool.name.endswith("NothingSeen"))
        return ModelResponse(parts=[ToolCallPart(tool.name, {"reason": "The evidence was read."})])

    dataset = DiagnosticDataset(name="failed-pair", version="1", cases=(DiagnosticCase(
        case_id="held-out", input=a_case().model_dump_json(), split="test", expected="clean",
        provenance=Provenance(source="unit test", kind="synthetic", review="deterministic", reviewer="test"),
    ),))
    report = await run_comparison(dataset, FunctionModel(unavailable_baseline), output=tmp_path, repetitions=2)

    (stored,) = (tmp_path / "learning").glob("*.json")
    learning = LearningRecord.model_validate_json(stored.read_text())
    assert report.comparison.verdict == "rejected"
    assert learning.evidence == ()
    assert len(learning.attempts) == 1
