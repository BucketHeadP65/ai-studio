"""The reflection's change-manifest models, the pure gate over a drafted reflection and the grading of its predictions.

A reflection model reads a `Diagnosis` and drafts ranked findings, each a falsifiable change-manifest.
`gate_reflection` re-validates a draft before anything downstream sees it, and `grade_predictions`
scores its predictions against the next round's measured deltas. The model never grades itself.

* `DraftReflection`, the model's structured output: ranked `ReflectiveFinding`s, most important
  first. Each finding names the retrieval surface it blames, the error type it explains, a prose
  narrative, and a `ChangeManifest` of falsifiable per-bucket predictions plus the buckets it puts
  at risk.
* `gate_reflection` drops any finding referencing a bucket or metric absent from the diagnosis and
  passes the rest through in rank order.
* `PredictionOutcome`, the graded record `grade_predictions` fills from the next round's measured
  deltas.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Literal

from pydantic import BaseModel, ConfigDict

from engram.evals.rca.domain.diagnosis import ErrorType
from engram.evals.shared.contracts import Archetype, RatedDifficulty

if TYPE_CHECKING:
    from collections.abc import Sequence

    from engram.evals.rca.domain.diagnosis import Diagnosis


# -- the change-manifest models (LLM-authored, gate-validated) ----------------


class BucketPrediction(BaseModel):
    """One falsifiable claim about how a change moves a single bucket's metric.

    A prediction names an `(archetype, difficulty)` bucket, the metric it should move, and the
    direction it should move in. The gate checks that the bucket and the metric both exist in the
    diagnosis, and `grade_predictions` grades the direction against the next round's measured delta.

    Attributes:
        archetype: The bucket's structural archetype, or `None` for the unlabelled bucket.
        difficulty: The bucket's difficulty band.
        metric: The metric name the claim is about (must name a metric present in the diagnosis,
            checked by `gate_reflection`).
        direction: Whether the metric is predicted to rise (`up`) or fall (`down`).
    """

    model_config = ConfigDict(frozen=True)

    archetype: Archetype | None
    difficulty: RatedDifficulty
    metric: str
    direction: Literal["up", "down"]


class ChangeManifest(BaseModel):
    """A finding's falsifiable forecast: the buckets a change should move, and the ones it risks.

    Attributes:
        predictions: The per-bucket metric moves the finding forecasts if its change is applied.
        at_risk: The buckets the finding warns the change might regress (a cost the proposer weighs).
    """

    model_config = ConfigDict(frozen=True)

    predictions: tuple[BucketPrediction, ...]
    at_risk: tuple[BucketPrediction, ...]


class ReflectiveFinding(BaseModel):
    """One reflective finding: a blamed surface, the error it explains, and a change-manifest.

    Attributes:
        surface: The retrieval surface the finding blames. The LLM may hypothesize `S3` (schema and
            ingestion), which the gate allows even though the deterministic router only ever emits
            `S1` / `S2`.
        error_type: The retrieval-layer error type the finding explains, one of the `ErrorType` values.
        narrative: The LLM's case-specific reasoning for the finding.
        manifest: The finding's falsifiable change-manifest.
    """

    model_config = ConfigDict(frozen=True)

    surface: Literal["S1", "S2", "S3"]
    error_type: ErrorType
    narrative: str
    manifest: ChangeManifest


class DraftReflection(BaseModel):
    """The reflection agent's structured output: ranked findings, most important first.

    The pre-gate emission: LLM-authored content only, valid downstream input only after
    `gate_reflection`.

    Attributes:
        findings: The drafted findings in rank order (the model ranks them, the gate preserves the
            order while dropping the invalid ones).
    """

    model_config = ConfigDict(frozen=True)

    findings: tuple[ReflectiveFinding, ...]


class PredictionOutcome(BaseModel):
    """One prediction graded against a later round's measured bucket delta (scored, never scoring).

    A prediction hits when the sign of the measured delta matches its direction (`up` needs a
    positive delta, `down` a negative one). A delta within `1e-9` of zero counts as a miss, no
    measurable move confirms no prediction. `grade_predictions` writes it.

    Attributes:
        prediction: The `BucketPrediction` being graded.
        measured_delta: The measured change in the prediction's metric for its bucket between rounds.
        hit: Whether `measured_delta`'s sign matches `prediction.direction` (a near-zero delta misses).
    """

    model_config = ConfigDict(frozen=True)

    prediction: BucketPrediction
    measured_delta: float
    hit: bool


# -- the pure gate -----------------------------------------------------------


def _finding_grounded(finding: ReflectiveFinding, *, buckets: set[tuple[Archetype | None, RatedDifficulty]], metrics: set[str]) -> bool:
    """Whether every bucket and metric in the finding's predictions and at-risk entries exists in the diagnosis."""
    manifest = finding.manifest
    return all(
        (prediction.archetype, prediction.difficulty) in buckets and prediction.metric in metrics
        for prediction in (*manifest.predictions, *manifest.at_risk)
    )


def gate_reflection(draft: DraftReflection, diagnosis: Diagnosis) -> tuple[ReflectiveFinding, ...]:
    """Re-validate a drafted reflection against the diagnosis, dropping ungrounded findings.

    Pure. A finding survives only when every bucket and metric its manifest references exists in the
    diagnosis (the LLM may still blame surface `S3`, which the deterministic router never emits).
    Surviving findings pass through in the draft's rank order, so the most important grounded finding
    stays first. An empty or wholly-ungrounded draft gates to an empty tuple.

    Args:
        draft: The reflection agent's pre-gate emission.
        diagnosis: The deterministic diagnosis the findings are validated against.

    Returns:
        The grounded findings in rank order.
    """
    buckets: set[tuple[Archetype | None, RatedDifficulty]] = {(bucket.archetype, bucket.difficulty) for bucket in diagnosis.buckets}
    metrics: set[str] = set(diagnosis.overall)
    return tuple(finding for finding in draft.findings if _finding_grounded(finding, buckets=buckets, metrics=metrics))


# A measured delta within this of zero counts as a miss, no measurable move confirms no prediction.
_ZERO_DELTA = 1e-9


def _predicted_metric(diagnosis: Diagnosis, prediction: BucketPrediction) -> float | None:
    """The measured value of the prediction's metric in its `(archetype, difficulty)` bucket, if present."""
    for bucket in diagnosis.buckets:
        if (bucket.archetype, bucket.difficulty) == (prediction.archetype, prediction.difficulty):
            return bucket.metrics.get(prediction.metric)
    return None


def grade_predictions(findings: Sequence[ReflectiveFinding], *, before: Diagnosis, after: Diagnosis) -> tuple[PredictionOutcome, ...]:
    """Grade every finding's predictions against the measured per-bucket deltas (scored, never scoring).

    Pure. For each prediction the measured delta is the bucket metric's move from `before` to
    `after` (the two rounds' candidate decision-core diagnoses), and a hit is a delta whose sign
    matches the predicted direction, a near-zero delta (within `1e-9`) is a miss. A bucket or
    metric absent from either side yields a `0.0` delta, no measurable move confirms no prediction.
    Only `manifest.predictions` are graded, the `at_risk` entries are warnings, not claims.

    Args:
        findings: The gated findings whose predictions are being graded.
        before: The decision-core diagnosis of the round the findings were drafted in.
        after: The next round's decision-core diagnosis.

    Returns:
        One `PredictionOutcome` per prediction, in finding-then-prediction order.
    """
    outcomes: list[PredictionOutcome] = []
    for finding in findings:
        for prediction in finding.manifest.predictions:
            measured_before = _predicted_metric(before, prediction)
            measured_after = _predicted_metric(after, prediction)
            delta = measured_after - measured_before if measured_before is not None and measured_after is not None else 0.0
            hit = delta > _ZERO_DELTA if prediction.direction == "up" else delta < -_ZERO_DELTA
            outcomes.append(PredictionOutcome(prediction=prediction, measured_delta=delta, hit=hit))
    return tuple(outcomes)
