"""Diagnostic evaluation of the inspector: labeled cases, repeated predictions, and scores that keep every failure.

The kernel's outcome comparison is specialized here on an execution that also carries the run's symptom count.
"""

from __future__ import annotations

from collections import Counter
from typing import Annotated, Literal, Protocol, Self

from pydantic import Field, model_validator

from core.ai.evaluation.contracts import ComparisonReport as _ComparisonReport
from core.ai.evaluation.contracts import (
    EvaluationError,
    EvaluationInput,
    EvaluationModel,
    Fingerprint,
    Nonempty,
    Provenance,
    ResourceUsage,
    ReviewAuthority,
    RunIdentity,
    Split,
    model_fingerprint,
    require_unique,
    total_usage,
)
from core.ai.evaluation.contracts import OutcomeTrial as _OutcomeTrial
from core.ai.evaluation.contracts import TaskExecution as _TaskExecution
from core.ai.evaluation.contracts import TaskRunner as _TaskRunner


class DiagnosticPrediction(EvaluationModel):
    """Observed symptoms and causal hypotheses, or explicit clean/uncertain outcomes."""

    verdict: Literal["issue", "clean", "abstain"]
    symptoms: tuple[Nonempty, ...] = ()
    causes: tuple[Nonempty, ...] = ()
    reason: Nonempty

    @model_validator(mode="after")
    def consistent_labels(self) -> Self:
        """Refuse a clean or uncertain verdict that also diagnoses an issue, and an issue without symptoms.

        Returns:
            The prediction.
        """
        require_unique(self.symptoms, "predicted symptoms")
        require_unique(self.causes, "predicted causes")
        if (self.verdict == "issue") != bool(self.symptoms) or (self.verdict != "issue" and self.causes):
            msg = "only issue verdicts carry symptoms or causes, and an issue requires symptoms"
            raise ValueError(msg)
        return self


class DiagnosticResponse(EvaluationModel):
    """One validated prediction together with the usage that produced it."""

    prediction: DiagnosticPrediction
    usage: ResourceUsage = ResourceUsage()


class TaskExecution(_TaskExecution):
    """Observed output and usage, together with the run's own symptom count."""

    symptom_count: int = Field(default=0, ge=0)


OutcomeTrial = _OutcomeTrial[TaskExecution]
ComparisonReport = _ComparisonReport[TaskExecution]
TaskRunner = _TaskRunner[TaskExecution]


_MIN_REPETITIONS = 2


class DiagnosticCase(EvaluationModel):
    """A labeled input. Its supported causes are what the evidence supports, not proven causes."""

    case_id: Nonempty
    input: Nonempty
    split: Split
    provenance: Provenance
    expected: Literal["issue", "clean", "uncertain"]
    expected_symptoms: tuple[Nonempty, ...] = ()
    supported_causes: tuple[Nonempty, ...] = ()

    @model_validator(mode="after")
    def consistent_labels(self) -> Self:
        """Refuse a clean or uncertain case that carries symptoms or causes, and an issue without symptoms.

        Returns:
            The case.
        """
        require_unique(self.expected_symptoms, "expected symptoms")
        require_unique(self.supported_causes, "supported causes")
        if (self.expected == "issue") != bool(self.expected_symptoms) or (self.expected != "issue" and self.supported_causes):
            msg = "only issue cases carry symptoms or supported causes, and an issue requires symptoms"
            raise ValueError(msg)
        return self


class DiagnosticDataset(EvaluationModel):
    """Versioned diagnostic labels with explicit split and review provenance."""

    name: Nonempty
    version: Nonempty
    cases: tuple[DiagnosticCase, ...] = Field(min_length=1)

    @model_validator(mode="after")
    def unique_cases(self) -> Self:
        """Refuse two cases with one case id.

        Returns:
            The dataset.
        """
        require_unique((case.case_id for case in self.cases), "case ids")
        return self

    @property
    def fingerprint(self) -> str:
        """Hash of all case content, labels, review metadata and dataset version."""
        return model_fingerprint(self)


class DiagnosticPredictor(Protocol):
    """Predict from input content only, translating recoverable errors to EvaluationError."""

    async def __call__(self, case: EvaluationInput, repetition: int) -> DiagnosticResponse:
        """Produce an independent trial without receiving expected labels.

        Args:
            case: The input to predict from.
            repetition: Which repetition of the case this trial is.

        Returns:
            The predicted diagnosis for the case.
        """
        ...


class DiagnosticTrial(EvaluationModel):
    """One repetition of one case: a prediction or a failure. Both count in every denominator."""

    case_id: Nonempty
    repetition: int = Field(ge=0)
    prediction: DiagnosticPrediction | None = None
    failure: Nonempty | None = None
    usage: ResourceUsage = ResourceUsage()

    @model_validator(mode="after")
    def one_outcome(self) -> Self:
        """Refuse a trial that records both a prediction and a failure, or neither.

        Returns:
            The trial.
        """
        if (self.prediction is None) == (self.failure is None):
            msg = "a diagnostic trial requires exactly one prediction or failure"
            raise ValueError(msg)
        return self


class DiagnosticMetrics(EvaluationModel):
    """Micro-averaged label scores. A ratio with a zero denominator is null."""

    trials: int
    true_positives: int
    false_positives: int
    false_negatives: int
    failures: int
    precision: float | None
    recall: float | None
    abstention_rate: float
    correct_abstentions: int
    clean_accuracy: float | None
    verdict_accuracy: float
    cause_accuracy: float | None


class DiagnosticReport(EvaluationModel):
    """Repeated diagnostic measurements. Label accuracy does not establish causal effects."""

    dataset: DiagnosticDataset
    dataset_fingerprint: Fingerprint
    identity: RunIdentity
    repetitions: Annotated[int, Field(ge=2)]
    split: Split
    trials: tuple[DiagnosticTrial, ...]
    metrics: DiagnosticMetrics
    usage: ResourceUsage
    review_counts: dict[ReviewAuthority, int]
    owner_calibrated: bool

    @model_validator(mode="after")
    def exact_dataset(self) -> Self:
        """Refuse a report whose coverage, scores or review authority differ from its trials and cases.

        Returns:
            The report.
        """
        if self.dataset_fingerprint != self.dataset.fingerprint:
            msg = "report dataset fingerprint does not match its cases"
            raise ValueError(msg)
        cases = tuple(case for case in self.dataset.cases if case.split == self.split)
        expected = {(case.case_id, repetition) for case in cases for repetition in range(self.repetitions)}
        actual = {(trial.case_id, trial.repetition) for trial in self.trials}
        if not cases or expected != actual or len(actual) != len(self.trials):
            msg = "report trial coverage differs from the required cases and repetitions"
            raise ValueError(msg)
        reviews: Counter[ReviewAuthority] = Counter(case.provenance.review for case in cases)
        if self.review_counts != dict(reviews) or self.owner_calibrated != (reviews["owner"] == len(cases)):
            msg = "report review authority differs from its case provenance"
            raise ValueError(msg)
        if self.metrics != _score_diagnostics(cases, self.trials) or self.usage != total_usage(trial.usage for trial in self.trials):
            msg = "report scores or usage differ from its trial evidence"
            raise ValueError(msg)
        return self


def _ratio(numerator: int, denominator: int) -> float | None:
    return numerator / denominator if denominator else None


def _score_diagnostics(cases: tuple[DiagnosticCase, ...], trials: tuple[DiagnosticTrial, ...]) -> DiagnosticMetrics:
    by_id = {case.case_id: case for case in cases}
    counts: Counter[str] = Counter()
    for trial in trials:
        case = by_id[trial.case_id]
        prediction = trial.prediction
        expected = set(case.expected_symptoms)
        predicted = set(prediction.symptoms) if prediction else set()
        causes = set(prediction.causes) if prediction else set()
        counts.update(
            {
                "tp": len(expected & predicted),
                "fp": len(predicted - expected),
                "fn": len(expected - predicted),
                "failures": int(trial.failure is not None),
                "abstentions": int(prediction is not None and prediction.verdict == "abstain"),
                "clean_cases": int(case.expected == "clean"),
                "causes": len(causes),
                "correct_causes": len(causes & set(case.supported_causes)),
            }
        )
        if prediction:
            verdict = "uncertain" if prediction.verdict == "abstain" else prediction.verdict
            correct = verdict == case.expected
            counts.update(
                {
                    "verdicts": int(correct),
                    "clean": int(correct and verdict == "clean"),
                    "abstained": int(correct and verdict == "uncertain"),
                }
            )
    return DiagnosticMetrics(
        trials=len(trials),
        true_positives=counts["tp"],
        false_positives=counts["fp"],
        false_negatives=counts["fn"],
        failures=counts["failures"],
        precision=_ratio(counts["tp"], counts["tp"] + counts["fp"]),
        recall=_ratio(counts["tp"], counts["tp"] + counts["fn"]),
        abstention_rate=counts["abstentions"] / len(trials),
        correct_abstentions=counts["abstained"],
        clean_accuracy=_ratio(counts["clean"], counts["clean_cases"]),
        verdict_accuracy=counts["verdicts"] / len(trials),
        cause_accuracy=_ratio(counts["correct_causes"], counts["causes"]),
    )


async def evaluate_diagnostics(
    dataset: DiagnosticDataset, predictor: DiagnosticPredictor, *, identity: RunIdentity, repetitions: int = 3, split: Split = "test"
) -> DiagnosticReport:
    """Evaluate every selected case repeatedly, preserving failures and their resource usage.

    Trials run one after another. The predictor handles provider timeouts and raises `EvaluationError` for a recoverable failure.
    A failed prediction counts as missed expected symptoms, never a clean or abstaining result.

    Args:
        dataset: The labeled cases.
        predictor: The predictor under evaluation.
        identity: What is being evaluated, recorded on the report.
        repetitions: How many times each case is predicted, at least two.
        split: Which split's cases to evaluate.

    Returns:
        The report with every trial and its scores.

    Raises:
        ValueError: `repetitions` is below two, or the dataset has no case in `split`.
    """
    if repetitions < _MIN_REPETITIONS:
        msg = "diagnostic evaluation requires at least two repetitions"
        raise ValueError(msg)
    cases = tuple(case for case in dataset.cases if case.split == split)
    if not cases:
        msg = f"dataset has no {split} cases"
        raise ValueError(msg)
    trials: list[DiagnosticTrial] = []
    for repetition in range(repetitions):
        for case in cases:
            try:
                response = await predictor(EvaluationInput(case_id=case.case_id, input=case.input), repetition)
            except EvaluationError as error:
                trials.append(DiagnosticTrial(case_id=case.case_id, repetition=repetition, failure=str(error), usage=error.usage))
            else:
                trials.append(
                    DiagnosticTrial(case_id=case.case_id, repetition=repetition, prediction=response.prediction, usage=response.usage)
                )
    reviews: Counter[ReviewAuthority] = Counter(case.provenance.review for case in cases)
    return DiagnosticReport(
        dataset=dataset,
        dataset_fingerprint=dataset.fingerprint,
        identity=identity,
        repetitions=repetitions,
        split=split,
        trials=tuple(trials),
        metrics=_score_diagnostics(cases, tuple(trials)),
        usage=total_usage(trial.usage for trial in trials),
        review_counts=dict(reviews),
        owner_calibrated=reviews["owner"] == len(cases),
    )
