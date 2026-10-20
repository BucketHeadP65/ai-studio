r"""Unit specs for the tuning error-signal reader (`engram.evals.rca.domain.diagnosis`).

`diagnose` turns one pydantic-evals `EvaluationRunReport` into a structured `Diagnosis` the
tuning proposer reasons about: the per-run `overall` average (== `average_metrics`),
per-`(archetype, difficulty)` `BucketMetrics`, the lowest-recall `worst_cases` carrying gold vs
cited ids, and a best-effort cost summary (mean duration, mean tokens).

Fakes over the heavy `ReportCase` constructor: like `test_runner.py`, the specs drive `diagnose`
with tiny structural stand-ins carrying exactly the fields it reads (`inputs.question`,
`metadata.{archetype,difficulty}`, `{output,expected_output}.cited_source_ids`, `scores`,
`metrics`, `total_duration`). The function's parameter is a Protocol both the real `EvaluationRunReport`
and these stand-ins satisfy.
"""

from __future__ import annotations

import dataclasses
import math

from dataclasses import dataclass

import pydantic
import pytest

from pydantic_evals.evaluators import EvaluationResult
from pydantic_evals.evaluators.spec import EvaluatorSpec
from pydantic_evals.reporting import EvaluationReport, ReportCase

from engram.evals.harness.contracts import EvaluationRunReport, average_metrics
from engram.evals.rca.contracts import Diagnosis, FailingCase, diagnose
from engram.evals.rca.domain.diagnosis import BucketMetrics, classify_case, route_surface
from engram.evals.shared.domain import Archetype, QaCaseMeta, QaDifficulty, QaInput, RetrievalAnswer

# -- structural stand-ins for one report's cases -----------------------------


@dataclass(frozen=True)
class _Score:
    """A stand-in for one `EvaluationResult` — `diagnose` reads only `.value`."""

    value: float


@dataclass(frozen=True)
class _Case:
    """A stand-in for one `ReportCase` — the slice `diagnose` folds over plus the case name the port requires."""

    name: str
    inputs: QaInput
    metadata: QaCaseMeta | None
    expected_output: RetrievalAnswer | None
    output: RetrievalAnswer
    scores: dict[str, _Score]
    metrics: dict[str, float | int]
    total_duration: float


@dataclass(frozen=True)
class _Report:
    """A stand-in for an `EvaluationReport` — `diagnose` reads only `.cases`."""

    cases: list[_Case]


def _meta(*, archetype: Archetype | None, difficulty: QaDifficulty) -> QaCaseMeta:
    return QaCaseMeta(anchor_page_id="paper.x", difficulty=difficulty, archetype=archetype)


def _case(
    *,
    name: str = "paper.x-0",
    question: str = "q?",
    archetype: Archetype | None = Archetype.PINPOINT,
    difficulty: QaDifficulty = "easy",
    gold: tuple[str, ...] = ("paper.a",),
    cited: tuple[str, ...] = ("paper.a",),
    scores: dict[str, float] | None = None,
    metrics: dict[str, float | int] | None = None,
    duration: float = 0.1,
    expected: bool = True,
) -> _Case:
    """One structural case with sensible defaults; override the axis a spec exercises."""
    resolved_scores = {"recall@5": 1.0, "n_tool_calls": 1.0} if scores is None else scores
    return _Case(
        name=name,
        inputs=QaInput(question=question),
        metadata=_meta(archetype=archetype, difficulty=difficulty),
        expected_output=RetrievalAnswer(answer="ref", cited_source_ids=list(gold)) if expected else None,
        output=RetrievalAnswer(answer="got", cited_source_ids=list(cited)),
        scores={name: _Score(value) for name, value in resolved_scores.items()},
        metrics={} if metrics is None else metrics,
        total_duration=duration,
    )


# -- a real pydantic-evals report (proves the production type satisfies the port) --

_SPEC = EvaluatorSpec(name="RetrievalMetrics", arguments=None)


def _real_report(*case_scores: dict[str, float]) -> EvaluationRunReport:
    """A real `EvaluationReport` whose cases carry the given metric maps.

    Used where a spec must drive both `diagnose` and the concrete-typed `average_metrics` with the
    same value, so the real `ReportCase` (not a stand-in) flows through, proving the production
    type structurally satisfies the reader's Protocol port.
    """
    cases = [
        ReportCase(
            name=f"c{index}",
            inputs=QaInput(question=f"q{index}?"),
            metadata=QaCaseMeta(anchor_page_id="paper.x", difficulty="easy", archetype=Archetype.PINPOINT),
            expected_output=RetrievalAnswer(answer="ref", cited_source_ids=["paper.a"]),
            output=RetrievalAnswer(answer="got", cited_source_ids=["paper.a"]),
            metrics={},
            attributes={},
            scores={name: EvaluationResult(name=name, value=value, reason=None, source=_SPEC) for name, value in scores.items()},
            labels={},
            assertions={},
            task_duration=0.1,
            total_duration=0.2,
        )
        for index, scores in enumerate(case_scores)
    ]
    return EvaluationReport(name="r", cases=cases)


# -- overall -----------------------------------------------------------------


class TestOverall:
    def test_overall_equals_average_metrics(self):
        # A real report so `diagnose` (via its Protocol port) and the concrete-typed
        # `average_metrics` both accept it — the equivalence is type-checked, not `# type: ignore`d.
        report = _real_report({"recall@5": 1.0, "mrr": 0.5, "n_tool_calls": 2.0}, {"recall@5": 0.0, "mrr": 1.0, "n_tool_calls": 4.0})

        result = diagnose(report)

        assert result.overall == average_metrics(report)
        assert result.overall == {"mrr": 0.75, "n_tool_calls": 3.0, "recall@5": 0.5}

    def test_overall_keys_are_sorted(self):
        report = _Report(cases=[_case(scores={"recall@5": 1.0, "mrr": 1.0, "precision@1": 1.0})])

        result = diagnose(report)

        assert list(result.overall) == ["mrr", "precision@1", "recall@5"]


# -- buckets -----------------------------------------------------------------


class TestBuckets:
    def test_groups_by_archetype_and_difficulty_with_metric_means(self):
        report = _Report(
            cases=[
                _case(archetype=Archetype.PINPOINT, difficulty="easy", scores={"recall@5": 1.0, "n_tool_calls": 2.0}),
                _case(archetype=Archetype.PINPOINT, difficulty="easy", scores={"recall@5": 0.0, "n_tool_calls": 4.0}),
                _case(archetype=Archetype.SYNTHESIS, difficulty="hard", scores={"recall@5": 0.5, "n_tool_calls": 6.0}),
            ]
        )

        result = diagnose(report)

        assert len(result.buckets) == 2
        by_key = {(bucket.archetype, bucket.difficulty): bucket for bucket in result.buckets}
        pinpoint = by_key[(Archetype.PINPOINT, "easy")]
        assert pinpoint.n_cases == 2
        assert pinpoint.metrics == {"recall@5": 0.5, "n_tool_calls": 3.0}
        synthesis = by_key[(Archetype.SYNTHESIS, "hard")]
        assert synthesis.n_cases == 1
        assert synthesis.metrics == {"recall@5": 0.5, "n_tool_calls": 6.0}

    def test_buckets_are_stably_ordered_by_difficulty_then_archetype(self):
        report = _Report(
            cases=[
                _case(archetype=Archetype.SURVEY, difficulty="hard"),
                _case(archetype=Archetype.PINPOINT, difficulty="easy"),
                _case(archetype=Archetype.SYNTHESIS, difficulty="easy"),
            ]
        )

        result = diagnose(report)

        keys = [(bucket.difficulty, bucket.archetype) for bucket in result.buckets]
        assert keys == [("easy", Archetype.PINPOINT), ("easy", Archetype.SYNTHESIS), ("hard", Archetype.SURVEY)]

    def test_a_none_archetype_is_its_own_bucket(self):
        report = _Report(
            cases=[
                _case(archetype=None, difficulty="medium", scores={"recall@5": 0.2}),
                _case(archetype=Archetype.PINPOINT, difficulty="medium", scores={"recall@5": 0.8}),
            ]
        )

        result = diagnose(report)

        archetypes = {bucket.archetype for bucket in result.buckets}
        assert None in archetypes
        assert Archetype.PINPOINT in archetypes


# -- worst_cases -------------------------------------------------------------


class TestWorstCases:
    def test_picks_the_lowest_recall_cases_ascending(self):
        report = _Report(
            cases=[
                _case(question="hi-recall", scores={"recall@5": 1.0, "n_tool_calls": 1.0}),
                _case(question="lo-recall", scores={"recall@5": 0.0, "n_tool_calls": 5.0}),
                _case(question="mid-recall", scores={"recall@5": 0.5, "n_tool_calls": 3.0}),
            ]
        )

        result = diagnose(report, worst_n=2)

        assert [case.question for case in result.worst_cases] == ["lo-recall", "mid-recall"]
        assert result.worst_cases[0].recall == 0.0

    def test_ties_keep_source_order(self):
        report = _Report(
            cases=[
                _case(question="first-zero", scores={"recall@5": 0.0}),
                _case(question="high", scores={"recall@5": 0.9}),
                _case(question="second-zero", scores={"recall@5": 0.0}),
            ]
        )

        result = diagnose(report, worst_n=2)

        assert [case.question for case in result.worst_cases] == ["first-zero", "second-zero"]

    def test_failing_case_carries_gold_cited_recall_and_tool_calls(self):
        report = _Report(
            cases=[
                _case(
                    question="why?",
                    archetype=Archetype.TRACE,
                    difficulty="hard",
                    gold=("paper.a", "paper.b"),
                    cited=("paper.c",),
                    scores={"recall@5": 0.0, "n_tool_calls": 7.0},
                )
            ]
        )

        worst = diagnose(report).worst_cases[0]

        assert worst.question == "why?"
        assert worst.gold_ids == ("paper.a", "paper.b")
        assert worst.cited_ids == ("paper.c",)
        assert worst.recall == 0.0
        assert worst.n_tool_calls == 7.0
        assert worst.archetype == Archetype.TRACE
        assert worst.difficulty == "hard"

    def test_worst_n_caps_the_count(self):
        report = _Report(cases=[_case(scores={"recall@5": float(i) / 10}) for i in range(10)])

        result = diagnose(report, worst_n=3)

        assert len(result.worst_cases) == 3

    def test_a_missing_expected_output_yields_empty_gold_ids(self):
        report = _Report(cases=[_case(expected=False, cited=("paper.c",), scores={"recall@5": 0.0})])

        worst = diagnose(report).worst_cases[0]

        assert worst.gold_ids == ()
        assert worst.cited_ids == ("paper.c",)


# -- cost --------------------------------------------------------------------


class TestCost:
    def test_mean_duration_over_cases(self):
        report = _Report(cases=[_case(duration=0.2), _case(duration=0.4)])

        result = diagnose(report)

        assert result.mean_duration_s == pytest.approx(0.3)

    def test_mean_total_tokens_sums_token_metrics_per_case(self):
        report = _Report(
            cases=[
                _case(metrics={"input_tokens": 100, "output_tokens": 50, "requests": 1}),
                _case(metrics={"input_tokens": 200, "output_tokens": 50, "requests": 1}),
            ]
        )

        result = diagnose(report)

        # per-case totals are 150 and 250; the mean is 200.
        assert result.mean_total_tokens == 200.0

    def test_no_token_metrics_yields_none(self):
        report = _Report(cases=[_case(metrics={"requests": 1, "cost": 0.0}), _case(metrics={})])

        result = diagnose(report)

        assert result.mean_total_tokens is None


# -- empty report ------------------------------------------------------------


class TestEmptyReport:
    def test_empty_report_is_a_well_formed_empty_diagnosis(self):
        result = diagnose(_Report(cases=[]))

        assert isinstance(result, Diagnosis)
        assert result.overall == {}
        assert result.buckets == ()
        assert result.worst_cases == ()
        assert result.mean_duration_s == 0.0
        assert result.mean_total_tokens is None

    def test_empty_means_are_real_zeros_not_nan(self):
        result = diagnose(_Report(cases=[]))

        assert not math.isnan(result.mean_duration_s)


# -- value-object shape ------------------------------------------------------


class TestValueObjects:
    def test_value_objects_are_frozen(self):
        bucket = BucketMetrics(archetype=Archetype.PINPOINT, difficulty="easy", n_cases=1, metrics={"recall@5": 1.0})
        failing = FailingCase(
            question="q",
            gold_ids=("paper.a",),
            cited_ids=("paper.b",),
            recall=0.0,
            n_tool_calls=2.0,
            archetype=Archetype.PINPOINT,
            difficulty="easy",
        )
        diagnosis = Diagnosis(
            overall={"recall@5": 1.0}, buckets=(bucket,), worst_cases=(failing,), mean_duration_s=0.1, mean_total_tokens=42.0
        )

        with pytest.raises(pydantic.ValidationError):
            diagnosis.mean_duration_s = 0.9  # type: ignore[misc]  # frozen-VO reassignment is rejected


# -- error-type taxonomy -----------------------------------------------------


class TestClassifyCase:
    @pytest.mark.parametrize(
        ("gold", "cited", "recall", "precision", "n_tool_calls", "expected"),
        [
            pytest.param(("a", "b"), ("c",), 0.0, 0.0, 3.0, "gold_missed", id="gold_missed-none-of-gold-cited"),
            pytest.param(("a",), ("z1", "z2", "z3", "z4", "z5", "a"), 1.0, 0.8, 3.0, "gold_buried", id="gold_buried-first-gold-rank-6"),
            pytest.param(("a",), ("z1", "z2", "z3", "z4", "a"), 1.0, 0.8, 3.0, "none", id="rank-5-is-not-buried"),
            pytest.param(("a",), ("a", "b", "c", "d", "e"), 1.0, 0.2, 9.0, "over_fetch", id="over_fetch-by-calls-and-precision"),
            pytest.param(("a",), ("a",), 1.0, 1.0, 9.0, "over_fetch", id="over_fetch-by-calls-only"),
            pytest.param(("a",), ("a", "b", "c", "d", "e"), 1.0, 0.2, 3.0, "over_fetch", id="over_fetch-by-precision-only"),
            pytest.param(("a",), ("a",), 1.0, 1.0, 8.0, "none", id="8-calls-is-not-over_fetch"),
            pytest.param(("a",), ("a",), 1.0, 1 / 3, 3.0, "none", id="precision-at-threshold-is-not-over_fetch"),
            pytest.param(("a",), (), 0.0, 0.0, 1.0, "abandoned", id="abandoned-one-call-no-citations"),
            pytest.param(("a",), (), 0.0, 0.0, 0.0, "abandoned", id="abandoned-zero-calls-no-citations"),
            pytest.param(("a",), (), 0.0, 0.0, 5.0, "gold_missed", id="no-citations-many-calls-is-missed-not-abandoned"),
            pytest.param(("a",), ("a",), 1.0, 1.0, 3.0, "none", id="none-clean-hit"),
            pytest.param((), ("x",), 0.0, 0.0, 3.0, "none", id="no-gold-expected-is-none"),
        ],
    )
    def test_truth_table(self, gold, cited, recall, precision, n_tool_calls, expected):
        result = classify_case(gold=gold, cited=cited, recall=recall, precision=precision, n_tool_calls=n_tool_calls)

        assert result == expected

    def test_abandoned_takes_precedence_over_gold_missed(self):
        # An early give-up with gold expected is abandoned (an S1 query-policy failure), not a
        # gold_missed retrieval failure — the trajectory-order precedence.
        assert classify_case(gold=("a",), cited=(), recall=0.0, precision=0.0, n_tool_calls=1.0) == "abandoned"


class TestRouteSurface:
    @pytest.mark.parametrize(
        ("error_type", "expected"),
        [("gold_missed", "S2"), ("gold_buried", "S2"), ("over_fetch", "S2"), ("abandoned", "S1"), ("none", "S2")],
    )
    def test_routes_error_type_to_primary_surface(self, error_type, expected):
        assert route_surface(error_type) == expected


class TestErrorTaxonomyInDiagnose:
    def test_bucket_error_counts_tally_every_type(self):
        report = _Report(
            cases=[
                _case(
                    archetype=Archetype.PINPOINT,
                    difficulty="easy",
                    gold=("a",),
                    cited=("a",),
                    scores={"recall@5": 1.0, "precision@5": 1.0, "n_tool_calls": 1.0},
                ),
                _case(
                    archetype=Archetype.PINPOINT,
                    difficulty="easy",
                    gold=("a",),
                    cited=("x",),
                    scores={"recall@5": 0.0, "precision@5": 0.0, "n_tool_calls": 3.0},
                ),
            ]
        )

        bucket = diagnose(report).buckets[0]

        assert bucket.error_counts == {"gold_missed": 1, "gold_buried": 0, "over_fetch": 0, "abandoned": 0, "none": 1}

    def test_error_totals_aggregate_across_buckets(self):
        report = _Report(
            cases=[
                _case(
                    archetype=Archetype.PINPOINT,
                    difficulty="easy",
                    gold=("a",),
                    cited=("a",),
                    scores={"recall@5": 1.0, "precision@5": 1.0, "n_tool_calls": 1.0},
                ),  # none
                _case(
                    archetype=Archetype.PINPOINT,
                    difficulty="easy",
                    gold=("a",),
                    cited=("x",),
                    scores={"recall@5": 0.0, "precision@5": 0.0, "n_tool_calls": 3.0},
                ),  # gold_missed
                _case(
                    archetype=Archetype.SYNTHESIS,
                    difficulty="hard",
                    gold=("a",),
                    cited=(),
                    scores={"recall@5": 0.0, "precision@5": 0.0, "n_tool_calls": 1.0},
                ),  # abandoned
            ]
        )

        result = diagnose(report)

        assert result.error_totals == {"gold_missed": 1, "gold_buried": 0, "over_fetch": 0, "abandoned": 1, "none": 1}

    def test_failing_case_carries_error_type_surface_and_gold_rank(self):
        report = _Report(
            cases=[
                _case(
                    question="buried",
                    gold=("a",),
                    cited=("z1", "z2", "z3", "z4", "z5", "a"),
                    scores={"recall@5": 1.0, "precision@5": 0.8, "n_tool_calls": 2.0},
                )
            ]
        )

        worst = diagnose(report).worst_cases[0]

        assert worst.error_type == "gold_buried"
        assert worst.surface == "S2"
        assert worst.gold_rank == 6

    def test_failing_case_gold_rank_is_none_when_no_gold_cited(self):
        report = _Report(
            cases=[
                _case(question="missed", gold=("a", "b"), cited=("c",), scores={"recall@5": 0.0, "precision@5": 0.0, "n_tool_calls": 3.0})
            ]
        )

        worst = diagnose(report).worst_cases[0]

        assert worst.error_type == "gold_missed"
        assert worst.gold_rank is None
        assert worst.surface == "S2"

    def test_abandoned_case_routes_to_s1(self):
        report = _Report(
            cases=[_case(question="gaveup", gold=("a",), cited=(), scores={"recall@5": 0.0, "precision@5": 0.0, "n_tool_calls": 1.0})]
        )

        worst = diagnose(report).worst_cases[0]

        assert worst.error_type == "abandoned"
        assert worst.surface == "S1"


class TestErrorTaxonomyEmptyReport:
    def test_empty_report_is_zeroed_and_existing_fields_unchanged(self):
        result = diagnose(_Report(cases=[]))

        # Existing empty-report behaviour is untouched (the taxonomy is strictly additive).
        assert result.overall == {}
        assert result.buckets == ()
        assert result.worst_cases == ()
        assert result.mean_duration_s == 0.0
        assert result.mean_total_tokens is None
        # The new field is the complete taxonomy at zero, not an empty mapping.
        assert result.error_totals == {"gold_missed": 0, "gold_buried": 0, "over_fetch": 0, "abandoned": 0, "none": 0}


class TestAdditiveDefaults:
    def test_models_construct_without_the_new_fields(self):
        # Proves the additive contract that keeps existing (out-of-scope) construction sites green.
        bucket = BucketMetrics(archetype=Archetype.PINPOINT, difficulty="easy", n_cases=1, metrics={"recall@5": 1.0})
        assert bucket.error_counts == {}

        failing = FailingCase(
            question="q", gold_ids=("a",), cited_ids=("b",), recall=0.0, n_tool_calls=2.0, archetype=Archetype.PINPOINT, difficulty="easy"
        )
        assert failing.error_type == "none"
        assert failing.surface == "S2"
        assert failing.gold_rank is None

        diagnosis = Diagnosis(overall={}, buckets=(), worst_cases=(), mean_duration_s=0.0, mean_total_tokens=None)
        assert diagnosis.error_totals == {}


class TestLayeredMetricKeysPassThroughUnaffected:
    """Pins that a case's namespaced layer keys never confuse the headline pick.

    A case's scores may now also carry `citation_` / `surfaced_` / `ranking_`-namespaced keys
    (`engram.evals.outcomes.domain.evaluators`) alongside the unprefixed `recall@k` / `precision@k` / `mrr`.
    `primary_at_k_key` matches on the bare `recall@` / `precision@` prefix, so a namespaced key
    (which never starts with that bare prefix) cannot be picked as the headline.
    """

    def test_the_headline_pick_reads_the_unprefixed_key(self):
        case = _case(
            gold=("paper.a",),
            cited=("paper.a",),
            scores={
                "recall@5": 1.0,
                "precision@5": 1.0,
                "mrr": 1.0,
                "citation_recall@5": 1.0,
                "surfaced_recall@5": 0.5,
                "ranking_precision@5": 0.0,
                "ranking_mrr": 0.5,
                "n_tool_calls": 1.0,
            },
        )

        diagnosis = diagnose(_Report(cases=[case]))

        assert diagnosis.worst_cases[0].recall == 1.0
        assert diagnosis.error_totals["none"] == 1

    def test_namespaced_keys_still_ride_the_general_overall_average(self):
        # BucketMetrics.metrics and Diagnosis.overall generalize over whatever a case's scores
        # carry, so the new keys are averaged in beside the unprefixed ones without any reader change.
        case = _case(scores={"recall@5": 1.0, "citation_recall@5": 1.0, "surfaced_recall@5": 0.5, "n_tool_calls": 1.0})

        diagnosis = diagnose(_Report(cases=[case]))

        assert diagnosis.overall["citation_recall@5"] == 1.0
        assert diagnosis.overall["surfaced_recall@5"] == 0.5


class TestHumanJudgedCases:
    def test_a_case_with_no_difficulty_is_bucketed_as_unrated(self):
        case = dataclasses.replace(_case(), metadata=QaCaseMeta(judgments={"paper.a": 1}))

        result = diagnose(_Report(cases=[case]))

        assert [(bucket.archetype, bucket.difficulty) for bucket in result.buckets] == [(None, "unrated")]
