"""Unit specs for the recommendation S3-signature detectors (`engram.evals.recommendation.domain.detectors`).

The detectors are pure functions over a `Diagnosis` plus injected knowledge_base-query results (the handler
calls the SQL gateway and feeds the results in). These specs pin each detector's firing rule:
section-split fires only for failing cases whose over-budget sections sit on their gold pages,
grouped by page-type kind, missing-edge assembles the unreachable gold pairs of failing trace and
survey cases with the unlifted links that would connect them, and over-expansion fires when a case
cites far more than its gold with low precision and many tool calls. An empty diagnosis yields no
signals.

The section-split and missing-edge specs source their injected inputs from `FakeKnowledgeBaseGateway`
(`tests/support/fakes/recommendation`) exactly as the handler will, so the fake's shape is exercised here
too. Over-expansion reads the report through a tight `Protocol`, so its specs drive it with tiny
structural stand-ins carrying only the fields it reads, the same fakes-over-`ReportCase` idiom as
`test_diagnosis`.
"""

from __future__ import annotations

from dataclasses import dataclass

import pydantic
import pytest

from engram.evals.rca.contracts import Diagnosis, FailingCase
from engram.evals.recommendation.contracts import CandidateEdge, MinedLink, SectionStat, StructuralDiagnosis
from engram.evals.recommendation.domain.detectors import (
    OverExpansionSignal,
    PairReachability,
    SectionSplitSignal,
    detect_missing_edges,
    detect_over_expansion,
    detect_section_splits,
)
from engram.evals.recommendation.domain.recommendation import CounterfactualResult
from engram.evals.shared.domain import Archetype, QaDifficulty
from engram.testing.fakes.recommendation import FakeKnowledgeBaseGateway

# -- diagnosis builders ------------------------------------------------------


def _failing_case(
    *,
    question: str,
    gold: tuple[str, ...],
    recall: float = 0.0,
    cited: tuple[str, ...] = (),
    archetype: Archetype | None = Archetype.PINPOINT,
    n_tool_calls: float = 1.0,
    difficulty: QaDifficulty = "medium",
) -> FailingCase:
    return FailingCase(
        question=question,
        gold_ids=gold,
        cited_ids=cited,
        recall=recall,
        n_tool_calls=n_tool_calls,
        archetype=archetype,
        difficulty=difficulty,
    )


def _diagnosis(worst_cases: list[FailingCase]) -> Diagnosis:
    return Diagnosis(overall={}, buckets=(), worst_cases=tuple(worst_cases), mean_duration_s=0.0, mean_total_tokens=None)


# -- over-expansion report stand-ins -----------------------------------------


@dataclass(frozen=True)
class _Score:
    value: float


@dataclass(frozen=True)
class _Cited:
    cited_source_ids: tuple[str, ...]


@dataclass(frozen=True)
class _Input:
    question: str


@dataclass(frozen=True)
class _ExpCase:
    inputs: _Input
    output: _Cited
    expected_output: _Cited | None
    scores: dict[str, _Score]


@dataclass(frozen=True)
class _ExpReport:
    cases: list[_ExpCase]


def _exp_case(*, question: str, cited: tuple[str, ...], gold: tuple[str, ...], precision: float, tool_calls: float) -> _ExpCase:
    return _ExpCase(
        inputs=_Input(question=question),
        output=_Cited(cited_source_ids=cited),
        expected_output=_Cited(cited_source_ids=gold),
        scores={"precision@5": _Score(precision), "n_tool_calls": _Score(tool_calls)},
    )


# -- section splits ----------------------------------------------------------


class TestDetectSectionSplits:
    async def test_fires_for_a_failing_case_with_an_over_budget_gold_section(self):
        gateway = FakeKnowledgeBaseGateway()
        gateway.sections_over_budget_result = (SectionStat(page_id="p:1", canon="findings", tokens=900),)
        base = _diagnosis([_failing_case(question="why?", gold=("p:1",), recall=0.0)])

        over_budget = await gateway.sections_over_budget(["p:1"], token_budget=600, collection="research")
        signals = detect_section_splits(base, over_budget, {"p:1": "paper"})

        assert len(signals) == 1
        assert signals[0].page_type == "paper"
        assert signals[0].case_names == ("why?",)
        assert signals[0].offenders == (SectionStat(page_id="p:1", canon="findings", tokens=900),)
        assert gateway.sections_over_budget_calls[0].token_budget == 600

    def test_does_not_fire_for_a_passing_case(self):
        base = _diagnosis([_failing_case(question="ok?", gold=("p:1",), recall=1.0)])

        signals = detect_section_splits(base, (SectionStat(page_id="p:1", canon="findings", tokens=900),), {"p:1": "paper"})

        assert signals == ()

    def test_does_not_fire_for_a_non_gold_page(self):
        base = _diagnosis([_failing_case(question="why?", gold=("p:1",), recall=0.0)])

        signals = detect_section_splits(base, (SectionStat(page_id="p:9", canon="findings", tokens=900),), {"p:9": "paper"})

        assert signals == ()

    def test_skips_an_over_budget_page_of_unknown_kind(self):
        base = _diagnosis([_failing_case(question="why?", gold=("p:1",), recall=0.0)])

        signals = detect_section_splits(base, (SectionStat(page_id="p:1", canon="findings", tokens=900),), {})

        assert signals == ()

    def test_groups_offenders_by_kind_ordered_by_page_type(self):
        base = _diagnosis(
            [_failing_case(question="a?", gold=("p:1",), recall=0.0), _failing_case(question="b?", gold=("n:1",), recall=0.0)]
        )
        over_budget = (SectionStat(page_id="p:1", canon="findings", tokens=900), SectionStat(page_id="n:1", canon="body", tokens=800))

        signals = detect_section_splits(base, over_budget, {"p:1": "paper", "n:1": "note"})

        assert [s.page_type for s in signals] == ["note", "paper"]
        assert signals[0].case_names == ("b?",)
        assert signals[1].case_names == ("a?",)


# -- missing edges -----------------------------------------------------------


class TestDetectMissingEdges:
    async def test_assembles_unreachable_pairs_mined_candidates_and_counterfactual(self):
        gateway = FakeKnowledgeBaseGateway()
        gateway.reachable_pairs_result = (False,)
        gateway.unlifted_links_result = (
            MinedLink(src_page_id="p:1", src_type="paper", raw_target="Foo", dst_page_id="p:2", dst_type="paper", rel="mentions"),
        )
        base = _diagnosis([_failing_case(question="trace?", gold=("p:1", "p:2"), archetype=Archetype.TRACE, recall=0.0)])
        pairs = [("p:1", "p:2")]
        counterfactual = CounterfactualResult(
            n_pairs=1,
            reachable_before=0,
            reachable_after=1,
            max_hops=3,
            candidate_edges=(CandidateEdge(src_id="p:1", dst_id="p:2", rel="mentions"),),
        )

        reachable = await gateway.reachable_pairs(pairs, max_hops=3, collection="research")
        reachability = tuple(
            PairReachability(src_id=src, dst_id=dst, reachable=is_reachable)
            for (src, dst), is_reachable in zip(pairs, reachable, strict=True)
        )
        mined = await gateway.unlifted_links(collection="research")
        signal = detect_missing_edges(base, reachability, mined, counterfactual)

        assert signal is not None
        assert signal.unreachable_pairs == (("p:1", "p:2"),)
        assert signal.case_names == ("trace?",)
        assert signal.mined_candidates == tuple(mined)
        assert signal.counterfactual is counterfactual

    def test_matches_a_mined_candidate_regardless_of_link_direction(self):
        base = _diagnosis([_failing_case(question="trace?", gold=("p:1", "p:2"), archetype=Archetype.SURVEY, recall=0.0)])
        reachability = (PairReachability(src_id="p:1", dst_id="p:2", reachable=False),)
        mined = (MinedLink(src_page_id="p:2", src_type="paper", raw_target="Foo", dst_page_id="p:1", dst_type="paper", rel="mentions"),)

        signal = detect_missing_edges(base, reachability, mined)

        assert signal is not None
        assert signal.mined_candidates == mined

    def test_ignores_dangling_mined_links(self):
        base = _diagnosis([_failing_case(question="trace?", gold=("p:1", "p:2"), archetype=Archetype.TRACE, recall=0.0)])
        reachability = (PairReachability(src_id="p:1", dst_id="p:2", reachable=False),)
        mined = (MinedLink(src_page_id="p:1", src_type="paper", raw_target="Ghost", dst_page_id=None, dst_type=None, rel="mentions"),)

        signal = detect_missing_edges(base, reachability, mined)

        assert signal is not None
        assert signal.mined_candidates == ()

    def test_returns_none_when_no_failing_trace_or_survey_case(self):
        base = _diagnosis([_failing_case(question="pin?", gold=("p:1", "p:2"), archetype=Archetype.PINPOINT, recall=0.0)])
        reachability = (PairReachability(src_id="p:1", dst_id="p:2", reachable=False),)

        assert detect_missing_edges(base, reachability, ()) is None

    def test_returns_none_when_every_pair_is_reachable(self):
        base = _diagnosis([_failing_case(question="trace?", gold=("p:1", "p:2"), archetype=Archetype.TRACE, recall=0.0)])
        reachability = (PairReachability(src_id="p:1", dst_id="p:2", reachable=True),)

        assert detect_missing_edges(base, reachability, ()) is None

    def test_ignores_an_unreachable_pair_not_owned_by_a_failing_case(self):
        base = _diagnosis([_failing_case(question="trace?", gold=("p:1", "p:2"), archetype=Archetype.TRACE, recall=0.0)])
        reachability = (PairReachability(src_id="x:1", dst_id="x:2", reachable=False),)

        assert detect_missing_edges(base, reachability, ()) is None


# -- over-expansion ----------------------------------------------------------


class TestDetectOverExpansion:
    def test_fires_on_low_precision_wide_citation_and_many_tool_calls(self):
        report = _ExpReport(cases=[_exp_case(question="q?", cited=("a", "b", "c", "d"), gold=("a",), precision=0.25, tool_calls=5.0)])

        signal = detect_over_expansion(report)

        assert signal is not None
        assert signal.case_names == ("q?",)
        assert signal.mean_cited == 4.0
        assert signal.mean_gold == 1.0
        assert signal.mean_tool_calls == 5.0

    def test_does_not_fire_when_precision_is_high(self):
        report = _ExpReport(cases=[_exp_case(question="q?", cited=("a", "b", "c", "d"), gold=("a",), precision=0.9, tool_calls=5.0)])

        assert detect_over_expansion(report) is None

    def test_does_not_fire_when_tool_calls_are_low(self):
        report = _ExpReport(cases=[_exp_case(question="q?", cited=("a", "b", "c", "d"), gold=("a",), precision=0.25, tool_calls=1.0)])

        assert detect_over_expansion(report) is None

    def test_does_not_fire_when_citation_is_not_wide(self):
        report = _ExpReport(cases=[_exp_case(question="q?", cited=("a", "b"), gold=("a", "b"), precision=0.25, tool_calls=5.0)])

        assert detect_over_expansion(report) is None

    def test_means_are_over_the_offending_cases_only(self):
        report = _ExpReport(
            cases=[
                _exp_case(question="offender", cited=("a", "b", "c", "d"), gold=("a",), precision=0.25, tool_calls=5.0),
                _exp_case(question="clean", cited=("a",), gold=("a",), precision=1.0, tool_calls=1.0),
            ]
        )

        signal = detect_over_expansion(report)

        assert signal is not None
        assert signal.case_names == ("offender",)
        assert signal.mean_cited == 4.0


# -- empty diagnosis + value-object shape ------------------------------------


class TestEmptyAndShape:
    def test_empty_diagnosis_yields_no_signals(self):
        base = _diagnosis([])

        assert detect_section_splits(base, (), {}) == ()
        assert detect_missing_edges(base, (), (), None) is None
        assert detect_over_expansion(_ExpReport(cases=[])) is None

    def test_structural_diagnosis_is_frozen(self):
        base = _diagnosis([])
        diagnosis = StructuralDiagnosis(base=base, evidence_grade="raw-baseline", section_splits=(), missing_edges=(), over_expansion=None)

        assert diagnosis.base == base
        with pytest.raises(pydantic.ValidationError):
            diagnosis.evidence_grade = "residual-of-tuned"  # type: ignore[misc]  # frozen-VO reassignment is rejected

    def test_signals_are_frozen_value_objects(self):
        signal = SectionSplitSignal(page_type="paper", case_names=("q?",), offenders=(SectionStat(page_id="p:1", canon="c", tokens=1),))

        with pytest.raises(pydantic.ValidationError):
            signal.page_type = "note"  # type: ignore[misc]  # frozen-VO reassignment is rejected

    def test_over_expansion_signal_round_trips(self):
        signal = OverExpansionSignal(case_names=("q?",), mean_cited=4.0, mean_gold=1.0, mean_tool_calls=5.0)

        assert OverExpansionSignal.model_validate(signal.model_dump()) == signal
