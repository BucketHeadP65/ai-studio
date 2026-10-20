"""Compare inspector evidence access on held-out tasks and retain every measured attempt."""

from __future__ import annotations

import asyncio
import inspect
import json
import platform

from dataclasses import dataclass, field
from importlib.metadata import version
from pathlib import Path
from typing import TYPE_CHECKING, Literal
from uuid import uuid4

from core.ai.evaluation.contracts import (
    ComparisonPlan,
    EvaluationError,
    EvaluationInput,
    ImprovementProposal,
    JsonLearningStore,
    LearningEvidence,
    LearningKey,
    Provenance,
    RunIdentity,
    TaskCase,
    TaskDataset,
    TaskGrade,
    compare_outcomes,
    content_fingerprint,
)
from core.ai.inspection.contracts import (
    INSPECTOR_INSTRUCTIONS,
    SCAN_INSTRUCTIONS,
    SCAN_PROMPT,
    TAXONOMY,
    ComparisonReport,
    DiagnosticDataset,
    DiagnosticPrediction,
    DiagnosticReport,
    DiagnosticResponse,
    LearningAttempt,
    LearningRecord,
    TaskExecution,
    Taxonomy,
    evaluate_diagnostics,
    inspector_agent,
    scan_agent,
    view_for,
)
from pydantic import BaseModel, ConfigDict

from harness.features.inspection.infrastructure import evaluation
from harness.features.inspection.infrastructure.evaluation import CaseInput, InspectorPredictor, ObservedModel

if TYPE_CHECKING:
    from pydantic_ai.models import Model


class OutcomeCriterion(BaseModel):
    """The independently specified correct diagnosis for one task."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    expected: Literal["issue", "clean", "uncertain"]
    symptoms: tuple[str, ...]
    causes: tuple[str, ...]


class DiagnosticOutcomeGrader:
    """Grade correctness, including supported causes, rather than how few symptoms were reported."""

    async def __call__(self, case: TaskCase, output: str) -> TaskGrade:
        """Judge the same task output without receiving its model or variant identity.

        Args:
            case: The task, whose success criterion holds the correct diagnosis.
            output: The diagnostic prediction the run produced, as JSON.

        Returns:
            The grade, successful when verdict, symptoms and causal support all match the criterion.
        """
        expected = OutcomeCriterion.model_validate_json(case.success_criterion)
        found = DiagnosticPrediction.model_validate_json(output)
        verdict = "abstain" if expected.expected == "uncertain" else expected.expected
        correct_verdict = found.verdict == verdict
        correct_symptoms = set(found.symptoms) == set(expected.symptoms)
        correct_causes = bool(found.causes) == bool(expected.causes) and set(found.causes) <= set(expected.causes)
        success = correct_verdict and correct_symptoms and correct_causes
        return TaskGrade(
            success=success,
            reason="Correct diagnostic outcome." if success else "Verdict, symptoms or causal support differ from the rubric.",
        )


@dataclass
class PairedInspector:
    """Run each variant once per slot and retain its response for diagnostic reporting."""

    predictors: dict[str, InspectorPredictor]
    responses: dict[tuple[str, str, int], DiagnosticResponse | EvaluationError] = field(default_factory=dict)

    async def __call__(self, case: EvaluationInput, identity: RunIdentity, repetition: int) -> TaskExecution:
        """Perform one paired trial with no expected labels in the model's input.

        Args:
            case: The task to run.
            identity: The variant to run it as.
            repetition: Which repetition of the case this is.

        Returns:
            The prediction as JSON, its symptom count and the usage it spent.

        Raises:
            EvaluationError: The variant's predictor failed. The failure is also kept for `RecordedPredictions`.
        """
        key = (identity.implementation_fingerprint, case.case_id, repetition)
        try:
            response = await self.predictors[identity.implementation_fingerprint](case, repetition)
        except EvaluationError as error:
            self.responses[key] = error
            raise
        self.responses[key] = response
        return TaskExecution(
            output=response.prediction.model_dump_json(), symptom_count=len(response.prediction.symptoms), usage=response.usage, diagnostics=()
        )


@dataclass(frozen=True)
class RecordedPredictions:
    """Read the paired trials back for metric aggregation without making another model call."""

    paired: PairedInspector
    identity: RunIdentity

    async def __call__(self, case: EvaluationInput, repetition: int) -> DiagnosticResponse:
        """Return the already recorded outcome and usage, or repeat its recorded failure.

        Args:
            case: The task whose trial was recorded.
            repetition: Which repetition of the case to read.

        Returns:
            The recorded response.

        Raises:
            EvaluationError: The recorded trial failed.
        """
        recorded = self.paired.responses[(self.identity.implementation_fingerprint, case.case_id, repetition)]
        if isinstance(recorded, EvaluationError):
            raise recorded
        return recorded


class InspectionExperiment(BaseModel):
    """One comparison and its diagnostic measurements, all derived from the same trials."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    comparison: ComparisonReport
    baseline: DiagnosticReport
    candidate: DiagnosticReport
    output_directory: str
    observed_models: tuple[str, ...] = ()
    observed_revisions: tuple[str, ...] = ()


def _identities(model: Model) -> tuple[ImprovementProposal, RunIdentity, RunIdentity]:
    """Freeze code, prompt, model and grading identities before any held-out trial runs."""
    sources = {}
    inspection_modules = (inspect.getmodule(inspector_agent), inspect.getmodule(scan_agent), inspect.getmodule(view_for))
    for module in (evaluation, *inspection_modules):
        if module is None or module.__file__ is None:
            msg = f"cannot fingerprint {module.__name__ if module is not None else 'an inspection module'}"
            raise ValueError(msg)
        sources[module.__name__] = Path(module.__file__).read_text(encoding="utf-8")
    taxonomy_module = inspect.getmodule(Taxonomy)
    if taxonomy_module is None or taxonomy_module.__file__ is None:
        msg = "cannot fingerprint the inspection taxonomy"
        raise ValueError(msg)
    for path in sorted(Path(taxonomy_module.__file__).parent.glob("*.py")):
        sources[f"taxonomy/{path.name}"] = path.read_text(encoding="utf-8")
    complete = json.dumps(
        {"evidence_access": "complete", "sources": sources, "taxonomy": TAXONOMY.model_dump(mode="json")}, sort_keys=True
    )
    ablation = json.dumps(
        {"evidence_access": "messages_only", "sources": sources, "taxonomy": TAXONOMY.model_dump(mode="json")}, sort_keys=True
    )
    proposal = ImprovementProposal(
        description="Expose recorded request instructions and tool definitions to the inspector.", content=complete
    )
    prompt = content_fingerprint(INSPECTOR_INSTRUCTIONS + SCAN_INSTRUCTIONS + SCAN_PROMPT)
    conditions = content_fingerprint(
        json.dumps(
            {
                "model": model.model_name,
                "provider": model.system,
                "settings": model.settings,
                "python": platform.python_version(),
                "pydantic": version("pydantic"),
                "pydantic_ai": version("pydantic-ai-slim"),
                "request_limit": evaluation.TRIAL_REQUEST_LIMIT,
                "timeout_seconds": evaluation.TRIAL_TIMEOUT_SECONDS,
                "taxonomy": TAXONOMY.model_dump(mode="json"),
            },
            sort_keys=True,
        )
    )
    grader = content_fingerprint(Path(__file__).read_text(encoding="utf-8"))
    shared = {
        "model": model.model_name,
        "model_version": "provider revision unresolved. Observed identities retained in traces",
        "prompt_fingerprint": prompt,
        "conditions_fingerprint": conditions,
        "grader_fingerprint": grader,
    }
    return (
        proposal,
        RunIdentity(**shared, implementation_fingerprint=content_fingerprint(ablation)),
        RunIdentity(**shared, implementation_fingerprint=proposal.fingerprint),
    )


def _plan(dataset: DiagnosticDataset, repetitions: int) -> ComparisonPlan:
    for case in dataset.cases:
        given = CaseInput.model_validate_json(case.input)
        if given.lead is not None:
            TAXONOMY.symptom(given.lead.symptom)
    cases = tuple(
        TaskCase(
            case_id=case.case_id,
            input=case.input,
            split=case.split,
            provenance=case.provenance,
            success_criterion=OutcomeCriterion(
                expected=case.expected, symptoms=case.expected_symptoms, causes=case.supported_causes
            ).model_dump_json(),
        )
        for case in dataset.cases
    )
    return ComparisonPlan(
        dataset=TaskDataset(name=dataset.name, version=dataset.version, cases=cases),
        optimization_case_ids=tuple(case.case_id for case in cases if case.split == "train"),
        evaluation_case_ids=tuple(case.case_id for case in cases if case.split == "test"),
        repetitions=repetitions,
    )


async def run_comparison(dataset: DiagnosticDataset, model: Model, *, output: Path, repetitions: int = 3) -> InspectionExperiment:
    """Measure complete evidence against missing metadata, preserve outcomes and accumulate reusable learning.

    This is an evidence-access ablation of the inspector, not a claim about downstream agent skill quality.
    Every invocation writes a new run directory. Prior comparisons and rejected proposals remain in the learning store.

    Args:
        dataset: The versioned diagnostic cases, split into train and test.
        model: The model both variants run on.
        output: The directory each run directory and the learning store are written under.
        repetitions: How many trials each variant runs per case.

    Returns:
        The comparison and both variants' diagnostic reports, all derived from the same trials.
    """
    plan = _plan(dataset, repetitions)
    proposal, baseline, candidate = await asyncio.to_thread(_identities, model)
    run_id = uuid4().hex
    directory = output / run_id
    observed = ObservedModel()
    paired = PairedInspector(
        predictors={
            baseline.implementation_fingerprint: InspectorPredictor(
                model, evidence="messages_only", trace_dir=directory / "traces", observed=observed
            ),
            candidate.implementation_fingerprint: InspectorPredictor(
                model, evidence="complete", trace_dir=directory / "traces", observed=observed
            ),
        }
    )
    comparison = await compare_outcomes(plan, paired, DiagnosticOutcomeGrader(), baseline=baseline, candidate=candidate)
    baseline_report = await evaluate_diagnostics(dataset, RecordedPredictions(paired, baseline), identity=baseline, repetitions=repetitions)
    candidate_report = await evaluate_diagnostics(
        dataset, RecordedPredictions(paired, candidate), identity=candidate, repetitions=repetitions
    )
    report = InspectionExperiment(
        comparison=comparison,
        baseline=baseline_report,
        candidate=candidate_report,
        output_directory=str(directory),
        observed_models=tuple(sorted(observed.names)),
        observed_revisions=tuple(sorted(observed.revisions)),
    )
    await asyncio.to_thread(_save_reports, directory, report)
    await _learn(output, dataset, report, proposal, run_id)
    return report


async def _learn(
    output: Path, dataset: DiagnosticDataset, report: InspectionExperiment, proposal: ImprovementProposal, run_id: str
) -> None:
    key = LearningKey(
        problem="Diagnoses made without access to recorded instructions and tool definitions", surface="inspector reading tools"
    )
    store = JsonLearningStore(output / "learning", record_type=LearningRecord)
    record = await store.load(key) or LearningRecord(key=key)
    for evidence in outcome_evidence(dataset, report, run_id):
        record = record.add_evidence(evidence)
    record = record.add_attempt(
        LearningAttempt(
            attempt_id=run_id,
            proposal=proposal,
            evaluation=report.comparison,
            rationale="Paired repeated held-out diagnostic tasks, independently graded for correct verdicts, symptoms and supported causes. "
            "Labels retain their stated review authority. This comparison does not establish downstream agent improvement.",
        )
    )
    await store.save(record)


def outcome_evidence(dataset: DiagnosticDataset, report: InspectionExperiment, run_id: str) -> list[LearningEvidence]:
    """Derive intervention evidence from executed paired outcomes, never from expected labels alone.

    A case with a failed or ungraded trial, or with no correct trial in either variant, gives no evidence.

    Args:
        dataset: The diagnostic cases the comparison ran on.
        report: The comparison's results.
        run_id: The comparison's run directory name, which each evidence id starts with.

    Returns:
        One piece of evidence per evaluation case that gives one.
    """
    cases = {case.case_id: case for case in dataset.cases}
    evidence: list[LearningEvidence] = []
    for case_id in report.comparison.plan.evaluation_case_ids:
        case = cases[case_id]
        trials = [trial for trial in report.comparison.trials if trial.case_id == case_id]
        if any(trial.failure is not None or trial.grade is None for trial in trials):
            continue
        baseline = sum(trial.variant == "baseline" and trial.grade is not None and trial.grade.success for trial in trials)
        candidate = sum(trial.variant == "candidate" and trial.grade is not None and trial.grade.success for trial in trials)
        stance: Literal["supporting", "contradicting", "successful_alternative"]
        if candidate > baseline:
            stance = "supporting"
        elif candidate < baseline:
            stance = "contradicting"
        elif baseline:
            stance = "successful_alternative"
        else:
            continue
        source = f"{report.output_directory}/comparison.json#{case_id}"
        evidence.append(
            LearningEvidence(
                evidence_id=f"{run_id}:{case_id}",
                source=source,
                observation=f"{case_id}: {baseline}/{report.comparison.plan.repetitions} baseline tasks correct and "
                f"{candidate}/{report.comparison.plan.repetitions} candidate tasks correct. "
                f"Labels were reviewed by {case.provenance.reviewer}. These small-sample results are descriptive, not causal proof.",
                stance=stance,
                provenance=Provenance(source=source, kind="recorded", review=case.provenance.review, reviewer=case.provenance.reviewer),
            )
        )
    return evidence


def _save_reports(directory: Path, report: InspectionExperiment) -> None:
    directory.mkdir(parents=True, exist_ok=True)
    for name, value in (("comparison", report.comparison), ("baseline", report.baseline), ("candidate", report.candidate)):
        (directory / f"{name}.json").write_text(value.model_dump_json(indent=2), encoding="utf-8")
    summary = {
        "experiment_kind": "evidence_access_ablation",
        "previous_release_comparison": False,
        "independent_holdout_verified": False,
        "training_performed": False,
        "scope_note": "Both variants use the same inspector with different evidence access. Disjoint split labels do not verify author blindness.",
        "verdict": report.comparison.verdict,
        "reasons": report.comparison.reasons,
        "baseline_task_success": report.comparison.baseline_success_rate,
        "candidate_task_success": report.comparison.candidate_success_rate,
        "baseline_diagnostics": report.baseline.metrics.model_dump(),
        "candidate_diagnostics": report.candidate.metrics.model_dump(),
        "owner_calibrated": report.candidate.owner_calibrated,
        "review_counts": report.candidate.review_counts,
        "dataset_fingerprint": report.candidate.dataset_fingerprint,
        "configured_model": report.comparison.candidate.model,
        "observed_models": report.observed_models,
        "observed_revisions": report.observed_revisions,
        "baseline_usage": report.comparison.baseline_usage.model_dump(),
        "candidate_usage": report.comparison.candidate_usage.model_dump(),
    }
    (directory / "summary.json").write_text(json.dumps(summary, indent=2), encoding="utf-8")
