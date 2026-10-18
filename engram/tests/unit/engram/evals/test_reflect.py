"""Unit specs for the reflection seam (`src/engram/evals/rca/domain/reflection.py` and `infrastructure/reflector.py`).

The reflection seam is the LLM-assisted half of diagnosis: an injected model drafts ranked findings
over the deterministic `Diagnosis`, and a pure gate re-validates every draft before anything
downstream reasons over it. These specs cover the two pieces:

* `gate_reflection`, the pure gate, drops findings that reference a bucket or metric absent from the
  diagnosis (an invented reference the LLM could have hallucinated) and passes the rest through in
  rank order, while allowing the LLM-only `S3` surface the deterministic router never emits;
* `PydanticAiReflector`, the port's pydantic-ai implementation, returns the draft a seeded
  network-free `TestModel` emits (no network, the `custom_output_args` pattern).

The reflection prompt's prose is never asserted, only that the seam plumbs a draft through and the
gate enforces groundedness.
"""

from __future__ import annotations

from pydantic_ai.models.test import TestModel

from engram.evals.rca.contracts import BucketPrediction, Diagnosis, DraftReflection, ReflectiveFinding, gate_reflection
from engram.evals.rca.domain.diagnosis import BucketMetrics
from engram.evals.rca.domain.reflection import ChangeManifest
from engram.evals.rca.infrastructure.reflector import PydanticAiReflector
from engram.evals.shared.domain import Archetype, QaDifficulty


def _diagnosis() -> Diagnosis:
    """A diagnosis with two known buckets and two known metrics, the gate's groundedness universe."""
    return Diagnosis(
        overall={"recall@5": 0.5, "precision@5": 0.4},
        buckets=(
            BucketMetrics(archetype=Archetype.TRACE, difficulty="hard", n_cases=3, metrics={"recall@5": 0.3}),
            BucketMetrics(archetype=None, difficulty="medium", n_cases=2, metrics={"recall@5": 0.6}),
        ),
        worst_cases=(),
        mean_duration_s=1.0,
        mean_total_tokens=None,
    )


def _finding(
    archetype: Archetype | None, difficulty: QaDifficulty, metric: str, *, at_risk: tuple[BucketPrediction, ...] = (), narrative: str = "n"
) -> ReflectiveFinding:
    """A finding whose sole prediction targets `(archetype, difficulty)`'s `metric` (rising)."""
    return ReflectiveFinding(
        surface="S2",
        error_type="gold_buried",
        narrative=narrative,
        manifest=ChangeManifest(
            predictions=(BucketPrediction(archetype=archetype, difficulty=difficulty, metric=metric, direction="up"),), at_risk=at_risk
        ),
    )


class TestGateReflection:
    """`gate_reflection` drops ungrounded findings and passes grounded ones through in rank order."""

    def test_drops_a_finding_referencing_an_unknown_bucket(self):
        draft = DraftReflection(findings=(_finding(Archetype.SURVEY, "easy", "recall@5"),))

        assert gate_reflection(draft, _diagnosis()) == ()

    def test_drops_a_finding_referencing_an_unknown_metric(self):
        draft = DraftReflection(findings=(_finding(Archetype.TRACE, "hard", "mrr@5"),))

        assert gate_reflection(draft, _diagnosis()) == ()

    def test_drops_a_finding_whose_at_risk_bucket_is_unknown(self):
        stray = BucketPrediction(archetype=Archetype.SURVEY, difficulty="easy", metric="recall@5", direction="down")
        draft = DraftReflection(findings=(_finding(Archetype.TRACE, "hard", "recall@5", at_risk=(stray,)),))

        assert gate_reflection(draft, _diagnosis()) == ()

    def test_keeps_valid_findings_in_rank_order(self):
        first = _finding(Archetype.TRACE, "hard", "recall@5", narrative="first")
        second = _finding(None, "medium", "precision@5", narrative="second")
        draft = DraftReflection(findings=(first, second))

        assert gate_reflection(draft, _diagnosis()) == (first, second)

    def test_drops_only_the_ungrounded_finding_preserving_order(self):
        keep_first = _finding(Archetype.TRACE, "hard", "recall@5", narrative="a")
        drop = _finding(Archetype.SURVEY, "easy", "recall@5", narrative="bad")
        keep_last = _finding(None, "medium", "recall@5", narrative="c")
        draft = DraftReflection(findings=(keep_first, drop, keep_last))

        assert gate_reflection(draft, _diagnosis()) == (keep_first, keep_last)

    def test_empty_draft_gates_to_empty(self):
        assert gate_reflection(DraftReflection(findings=()), _diagnosis()) == ()

    def test_finding_with_an_empty_manifest_passes_and_s3_surface_is_allowed(self):
        # No references to ground, so the finding is vacuously grounded; S3 is the LLM-only surface
        # the deterministic router never emits, and the gate allows it.
        finding = ReflectiveFinding(surface="S3", error_type="none", narrative="n", manifest=ChangeManifest(predictions=(), at_risk=()))
        draft = DraftReflection(findings=(finding,))

        assert gate_reflection(draft, _diagnosis()) == (finding,)


class TestPydanticAiReflector:
    """`PydanticAiReflector` returns the draft a seeded network-free `TestModel` emits (no network)."""

    async def test_reflect_returns_the_seeded_draft(self):
        seed = DraftReflection(findings=(_finding(Archetype.TRACE, "hard", "recall@5"),))
        reflector = PydanticAiReflector(TestModel(custom_output_args=seed.model_dump()))

        draft = await reflector.reflect(_diagnosis())

        assert draft == seed
