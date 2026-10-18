"""Hand-computed correctness tests for the layered evidence metrics.

These tests are the spec for `engram.evals.outcomes.domain.evaluators`: the one arithmetic core
(`ranked_metrics`) and the three layers built over it (citation, surfaced-source, raw-ranking), each
scored from its own explicit input. The parametrized divergence table pins the exact scenario every
layer exists to tell apart: a well-cited answer whose underlying search ranked poorly, a search that
failed before it could rank anything, and a case with no search call at all.

Real `FileDeliveredEvidenceStore` instances (over `tmp_path`) stand in for the reader everywhere a
search call's stored payload needs reading back, the same adapter a live guarded trial writes
through, so the ranking layer is exercised against the exact bytes a reader would refuse or accept.
"""

from __future__ import annotations

import math

from typing import TYPE_CHECKING, Literal

import pytest

from core.ai.evaluation.contracts import DeliveredEvidenceRef, ExecutionTrace, FileDeliveredEvidenceStore, ToolCallTrace
from core.ai.retrieval.contracts import NextSteps, SearchHit, SearchResult

from engram.evals.outcomes.domain.evaluators import (
    LayeredEvidenceMetrics,
    MetricStatus,
    RankedMetrics,
    SearchCallRanking,
    SearchRankingMetrics,
    SurfacedSourceMetrics,
    _mrr,
    _precision_at_k,
    _recall_at_k,
    bare_unit_id,
    citation_source_metrics,
    ndcg_at_k,
    normalize_refs,
    page_ids,
    page_of,
    ranked_metrics,
    surfaced_source_metrics,
)
from engram.evals.outcomes.service import layered_evidence_metrics, search_ranking_metrics
from engram.evals.shared.serve_bridge import SEARCH

if TYPE_CHECKING:
    from pathlib import Path

_EPSILON = 1e-9
_COLLECTION = "research"


def _hit(unit_id: str) -> SearchHit:
    """A minimal ranked hit carrying only its own bare id."""
    return SearchHit(id=unit_id, hit_level="page", score=1.0)


def _search_result(*unit_ids: str) -> SearchResult:
    """A bounded search answer over `unit_ids`, in rank order."""
    return SearchResult(hits=tuple(_hit(unit_id) for unit_id in unit_ids), next_steps=NextSteps(summary="ok"))


async def _store_search_call(
    store: FileDeliveredEvidenceStore, trial_id: str, ordinal: int, result: SearchResult
) -> ToolCallTrace:
    """An `ok` search call whose evidence is the real stored bytes of `result`."""
    ref = await store.append(trial_id, ordinal, result.model_dump_json(), sources=())
    return ToolCallTrace(tool_name=SEARCH, ordinal=ordinal, status="ok", delivered_evidence=(ref,), response_bytes=ref.payload_bytes, duration_ms=1)


def _failed_search_call(ordinal: int) -> ToolCallTrace:
    """A search call the tool itself failed: no stored evidence, no ranking possible."""
    return ToolCallTrace(tool_name=SEARCH, ordinal=ordinal, status="failed", response_bytes=0, duration_ms=1)


class TestPageOf:
    def test_strips_the_first_anchor_delimiter(self):
        assert page_of("paper.x#^c3") == "paper.x"

    def test_only_strips_the_first_hash(self):
        assert page_of("paper.x#^c3#stray") == "paper.x"

    def test_a_bare_page_is_unchanged(self):
        assert page_of("paper.x") == "paper.x"


class TestPageIds:
    def test_strips_claim_anchor_suffix(self):
        assert page_ids(["p.q#^c1", "p.q", "p.r"]) == ["p.q", "p.r"]

    def test_preserves_first_occurrence_order(self):
        assert page_ids(["b", "a", "c"]) == ["b", "a", "c"]

    def test_dedupes_keeping_first(self):
        assert page_ids(["a", "b", "a", "c", "b"]) == ["a", "b", "c"]

    def test_anchor_and_bare_page_collapse_to_one(self):
        assert page_ids(["paper.x#^c3", "paper.x", "paper.x#^c9"]) == ["paper.x"]

    def test_empty_input_yields_empty_list(self):
        assert page_ids([]) == []


class TestNormalizeRefs:
    def test_page_granularity_collapses_the_anchor(self):
        assert normalize_refs(["paper.x#^c3"], "page") == ["paper.x"]

    def test_unit_granularity_keeps_the_anchor_exact(self):
        assert normalize_refs(["paper.x#^c3"], "unit") == ["paper.x#^c3"]

    def test_unit_granularity_still_dedupes_in_order(self):
        assert normalize_refs(["a", "b", "a"], "unit") == ["a", "b"]


class TestBareUnitId:
    def test_a_bare_ref_resolves_to_itself(self):
        assert bare_unit_id("paper.x", _COLLECTION) == "paper.x"

    def test_a_qualified_ref_resolves_to_its_unit_id(self):
        assert bare_unit_id(f"page:{_COLLECTION}/paper.x", _COLLECTION) == "paper.x"

    def test_a_qualified_claim_ref_keeps_its_anchor(self):
        assert bare_unit_id(f"unit:{_COLLECTION}/paper.x#^c3", _COLLECTION) == "paper.x#^c3"

    def test_an_unparseable_ref_falls_back_to_itself(self):
        # "group:<name>" with no "/" segment is a malformed group ref (BadRefError), and this
        # function never raises for it: the reference is used as-is rather than dropped.
        assert bare_unit_id("group:research", _COLLECTION) == "group:research"


class TestRecallAtK:
    @pytest.mark.parametrize(("k", "expected"), [(1, 1 / 3), (2, 1 / 3), (3, 2 / 3)])
    def test_canonical_example(self, k: int, expected: float):
        assert _recall_at_k(["a", "x", "b"], {"a", "b", "c"}, k) == pytest.approx(expected, abs=_EPSILON)

    def test_empty_want_is_zero_not_division_error(self):
        assert _recall_at_k(["a", "b"], set(), 5) == 0.0

    def test_empty_got_is_zero(self):
        assert _recall_at_k([], {"a", "b"}, 5) == 0.0


class TestPrecisionAtK:
    @pytest.mark.parametrize(("k", "expected"), [(1, 1.0), (2, 0.5), (3, 2 / 3)])
    def test_canonical_example(self, k: int, expected: float):
        assert _precision_at_k(["a", "x", "b"], {"a", "b", "c"}, k) == pytest.approx(expected, abs=_EPSILON)

    def test_empty_got_is_zero_not_division_error(self):
        assert _precision_at_k([], {"a", "b"}, 5) == 0.0


class TestMrr:
    def test_first_item_relevant_gives_one(self):
        assert _mrr(["a", "x", "b"], {"a", "b", "c"}) == pytest.approx(1.0, abs=_EPSILON)

    def test_second_item_relevant_gives_one_half(self):
        assert _mrr(["x", "a", "b"], {"a", "b", "c"}) == pytest.approx(0.5, abs=_EPSILON)

    def test_no_relevant_item_gives_zero(self):
        assert _mrr(["x", "y", "z"], {"a", "b", "c"}) == 0.0


class TestRankedMetrics:
    def test_bundles_recall_precision_and_reciprocal_rank_per_k(self):
        result = ranked_metrics(["a", "x", "b"], {"a", "b", "c"}, ks=(1, 3))

        assert result.recall[1] == pytest.approx(1 / 3, abs=_EPSILON)
        assert result.recall[3] == pytest.approx(2 / 3, abs=_EPSILON)
        assert result.precision[1] == pytest.approx(1.0, abs=_EPSILON)
        assert result.precision[3] == pytest.approx(2 / 3, abs=_EPSILON)
        assert result.reciprocal_rank == pytest.approx(1.0, abs=_EPSILON)


class TestCitationSourceMetrics:
    def test_is_always_measured(self):
        result = citation_source_metrics(["a"], ["a"], ks=(1,))
        assert result.status == "measured"

    def test_empty_citations_still_measure_to_zero(self):
        result = citation_source_metrics([], ["a", "b"], ks=(1,))
        assert result.status == "measured"
        assert result.metrics.precision[1] == 0.0

    def test_page_granularity_collapses_a_claim_anchor_to_its_page(self):
        result = citation_source_metrics(["paper.x#^c3"], ["paper.x"], ks=(1,), granularity="page")
        assert result.metrics.recall[1] == pytest.approx(1.0, abs=_EPSILON)

    def test_unit_granularity_keeps_the_claim_anchor_exact(self):
        result = citation_source_metrics(["paper.x#^c3"], ["paper.x"], ks=(1,), granularity="unit")
        assert result.metrics.recall[1] == pytest.approx(0.0, abs=_EPSILON)


class TestSurfacedSourceMetrics:
    def test_no_tool_calls_at_all_is_not_applicable(self):
        result = surfaced_source_metrics((), ["a"], collection=_COLLECTION, ks=(1,))
        assert result.status == "not_applicable"
        assert result.metrics is None

    def test_every_call_failing_or_refused_is_unknown(self):
        calls = (
            ToolCallTrace(tool_name=SEARCH, ordinal=0, status="failed", response_bytes=0, duration_ms=1),
            ToolCallTrace(tool_name="fetch", ordinal=1, status="refused", response_bytes=10, duration_ms=1),
        )
        result = surfaced_source_metrics(calls, ["a"], collection=_COLLECTION, ks=(1,))
        assert result.status == "unknown"
        assert result.metrics is None

    def test_an_ok_call_is_measured_from_its_debased_returned_refs(self):
        calls = (
            ToolCallTrace(
                tool_name="fetch",
                ordinal=0,
                status="ok",
                returned_unit_refs=(f"page:{_COLLECTION}/page.gold",),
                response_bytes=10,
                duration_ms=1,
            ),
        )
        result = surfaced_source_metrics(calls, ["page.gold"], collection=_COLLECTION, ks=(1,))
        assert result.status == "measured"
        assert result.metrics is not None
        assert result.metrics.recall[1] == pytest.approx(1.0, abs=_EPSILON)

    def test_first_seen_union_spans_every_ok_call(self):
        calls = (
            ToolCallTrace(
                tool_name="fetch", ordinal=0, status="ok", returned_unit_refs=(f"page:{_COLLECTION}/page.a",), response_bytes=10, duration_ms=1
            ),
            ToolCallTrace(
                tool_name="fetch", ordinal=1, status="ok", returned_unit_refs=(f"page:{_COLLECTION}/page.b",), response_bytes=10, duration_ms=1
            ),
        )
        result = surfaced_source_metrics(calls, ["page.a", "page.b"], collection=_COLLECTION, ks=(2,))
        assert result.status == "measured"
        assert result.metrics is not None
        assert result.metrics.recall[2] == pytest.approx(1.0, abs=_EPSILON)


class TestSearchRankingMetrics:
    async def test_no_search_call_is_not_applicable_at_the_trial_level(self, tmp_path: Path):
        store = FileDeliveredEvidenceStore(tmp_path)
        result = await search_ranking_metrics((), store, ["page.gold"], ks=(1,))
        assert result.calls == ()

    async def test_page_granularity_uses_the_hits_own_page(self, tmp_path: Path):
        store = FileDeliveredEvidenceStore(tmp_path)
        call = await _store_search_call(store, "trial", 0, _search_result("paper.x#^c3"))

        page_scored = await search_ranking_metrics((call,), store, ["paper.x"], ks=(1,), granularity="page")
        unit_scored = await search_ranking_metrics((call,), store, ["paper.x"], ks=(1,), granularity="unit")

        page_metrics, unit_metrics = page_scored.calls[0].metrics, unit_scored.calls[0].metrics
        assert page_metrics is not None
        assert unit_metrics is not None
        assert page_metrics.recall[1] == pytest.approx(1.0, abs=_EPSILON)
        assert unit_metrics.recall[1] == pytest.approx(0.0, abs=_EPSILON)

    async def test_multiple_search_calls_keep_separate_rankings(self, tmp_path: Path):
        store = FileDeliveredEvidenceStore(tmp_path)
        first = await _store_search_call(store, "trial", 0, _search_result("page.gold"))
        second = await _store_search_call(store, "trial", 1, _search_result("page.other"))

        result = await search_ranking_metrics((first, second), store, ["page.gold"], ks=(1,))

        assert [call.ordinal for call in result.calls] == [0, 1]
        first_metrics, second_metrics = result.calls[0].metrics, result.calls[1].metrics
        assert result.calls[0].status == "measured"
        assert first_metrics is not None
        assert first_metrics.precision[1] == pytest.approx(1.0, abs=_EPSILON)
        assert result.calls[1].status == "measured"
        assert second_metrics is not None
        assert second_metrics.precision[1] == pytest.approx(0.0, abs=_EPSILON)

    async def test_a_refused_call_with_stored_evidence_is_unknown_not_a_crash(self, tmp_path: Path):
        # A refused call stores the refusal text as its evidence (not a SearchResult), which the
        # ranking layer must never try to parse: status alone decides, before any read.
        store = FileDeliveredEvidenceStore(tmp_path)
        ref = await store.append("trial", 0, "not available in this evaluation", sources=())
        call = ToolCallTrace(tool_name=SEARCH, ordinal=0, status="refused", delivered_evidence=(ref,), response_bytes=ref.payload_bytes, duration_ms=1)

        result = await search_ranking_metrics((call,), store, ["page.gold"], ks=(1,))

        assert result.calls[0].status == "unknown"
        assert result.calls[0].metrics is None

    async def test_a_reader_refusal_reads_as_unknown_not_a_crash(self, tmp_path: Path):
        store = FileDeliveredEvidenceStore(tmp_path)
        stored = await store.append("trial", 0, _search_result("page.gold").model_dump_json(), sources=())
        # A reference naming bytes that were never actually stored under this hash: the store
        # refuses to read it back rather than returning something else.
        tampered = DeliveredEvidenceRef(
            evidence_id=stored.evidence_id, ordinal=stored.ordinal, payload_sha256="0" * 64, payload_bytes=stored.payload_bytes
        )
        call = ToolCallTrace(tool_name=SEARCH, ordinal=0, status="ok", delivered_evidence=(tampered,), response_bytes=stored.payload_bytes, duration_ms=1)

        result = await search_ranking_metrics((call,), store, ["page.gold"], ks=(1,))

        assert result.calls[0].status == "unknown"
        assert result.calls[0].metrics is None


def _measured_call(ordinal: int, *, recall: float, precision: float, mrr: float) -> SearchCallRanking:
    """One measured call carrying a hand-picked `RankedMetrics` at `k=1`, for rollup arithmetic."""
    return SearchCallRanking(
        ordinal=ordinal, status="measured", metrics=RankedMetrics(recall={1: recall}, precision={1: precision}, reciprocal_rank=mrr)
    )


def _unknown_call(ordinal: int) -> SearchCallRanking:
    """One call whose ranking could not be measured."""
    return SearchCallRanking(ordinal=ordinal, status="unknown")


class TestSearchRankingRollup:
    """`SearchRankingMetrics.rollup`: the mean over the trial's measured calls."""

    def test_no_search_call_is_not_applicable(self):
        rollup = SearchRankingMetrics(calls=()).rollup

        assert rollup.status == "not_applicable"
        assert rollup.metrics is None

    def test_every_call_unknown_is_unknown(self):
        rollup = SearchRankingMetrics(calls=(_unknown_call(0), _unknown_call(1))).rollup

        assert rollup.status == "unknown"
        assert rollup.metrics is None

    def test_two_measured_calls_average(self):
        calls = (_measured_call(0, recall=1.0, precision=1.0, mrr=1.0), _measured_call(1, recall=0.0, precision=0.0, mrr=0.5))

        rollup = SearchRankingMetrics(calls=calls).rollup

        assert rollup.status == "measured"
        assert rollup.metrics is not None
        assert rollup.metrics.recall[1] == pytest.approx(0.5, abs=_EPSILON)
        assert rollup.metrics.precision[1] == pytest.approx(0.5, abs=_EPSILON)
        assert rollup.metrics.reciprocal_rank == pytest.approx(0.75, abs=_EPSILON)

    def test_one_measured_and_one_unknown_averages_only_the_measured_call(self):
        calls = (_measured_call(0, recall=1.0, precision=1.0, mrr=1.0), _unknown_call(1))

        rollup = SearchRankingMetrics(calls=calls).rollup

        assert rollup.status == "measured"
        assert rollup.metrics is not None
        assert rollup.metrics.recall[1] == pytest.approx(1.0, abs=_EPSILON)
        assert rollup.metrics.precision[1] == pytest.approx(1.0, abs=_EPSILON)
        assert rollup.metrics.reciprocal_rank == pytest.approx(1.0, abs=_EPSILON)


# -- the divergence table: citation, surfaced and ranking answer differently for the same trial ---


_ROW_SEARCH_ORDER_IRRELEVANT = "search_order_irrelevant_answer_cites_expected"
_ROW_SEARCH_FAILED = "search_failed_before_results"
_ROW_NO_SEARCH_CALL = "no_search_call"


@pytest.mark.parametrize(
    ("row_id", "search_call", "expected_citation_precision_at_1", "expected_ranking_status", "expected_ranking_precision_at_1", "expected_ranking_mrr"),
    [
        pytest.param(_ROW_SEARCH_ORDER_IRRELEVANT, "ok", 1.0, "measured", 0.0, 0.5, id=_ROW_SEARCH_ORDER_IRRELEVANT),
        pytest.param(_ROW_SEARCH_FAILED, "failed", 1.0, "unknown", None, None, id=_ROW_SEARCH_FAILED),
        pytest.param(_ROW_NO_SEARCH_CALL, "none", 1.0, "not_applicable", None, None, id=_ROW_NO_SEARCH_CALL),
    ],
)
async def test_citation_and_ranking_diverge_over_one_trial(
    row_id: str,
    search_call: Literal["ok", "failed", "none"],
    expected_citation_precision_at_1: float,
    expected_ranking_status: MetricStatus,
    expected_ranking_precision_at_1: float | None,
    expected_ranking_mrr: float | None,
    tmp_path: Path,
):
    del row_id  # carried only for the parametrize id
    store = FileDeliveredEvidenceStore(tmp_path)
    expected = ("page.gold",)
    cited = ("page.gold",)

    if search_call == "ok":
        # The gold page ranks second: a search-quality metric should read this as mediocre even
        # though the agent's final citation is exactly right.
        tool_calls = (await _store_search_call(store, "trial", 0, _search_result("page.other", "page.gold")),)
    elif search_call == "failed":
        tool_calls = (_failed_search_call(0),)
    else:
        tool_calls = ()

    citation = citation_source_metrics(cited, expected, ks=(1,))
    ranking = await search_ranking_metrics(tool_calls, store, expected, ks=(1,))

    assert citation.status == "measured"
    assert citation.metrics.precision[1] == pytest.approx(expected_citation_precision_at_1, abs=_EPSILON)

    if expected_ranking_status == "not_applicable":
        assert ranking.calls == ()
        return

    assert len(ranking.calls) == 1
    call = ranking.calls[0]
    assert call.status == expected_ranking_status
    if expected_ranking_status == "measured":
        assert call.metrics is not None
        assert call.metrics.precision[1] == pytest.approx(expected_ranking_precision_at_1, abs=_EPSILON)
        assert call.metrics.reciprocal_rank == pytest.approx(expected_ranking_mrr, abs=_EPSILON)
    else:
        assert call.metrics is None


class TestLayeredEvidenceMetrics:
    async def test_ties_the_three_layers_together_for_one_trial(self, tmp_path: Path):
        store = FileDeliveredEvidenceStore(tmp_path)
        search_call = await _store_search_call(store, "trial", 0, _search_result("page.other", "page.gold"))
        surfaced_call = ToolCallTrace(
            tool_name="fetch", ordinal=1, status="ok", returned_unit_refs=(f"page:{_COLLECTION}/page.gold",), response_bytes=10, duration_ms=1
        )
        trace = ExecutionTrace(tool_calls=(search_call, surfaced_call))

        result = await layered_evidence_metrics(
            cited=("page.gold",), expected=("page.gold",), trace=trace, reader=store, collection=_COLLECTION, ks=(1,)
        )

        assert isinstance(result, LayeredEvidenceMetrics)
        assert result.citation.status == "measured"
        assert result.citation.metrics.precision[1] == pytest.approx(1.0, abs=_EPSILON)
        assert isinstance(result.surfaced, SurfacedSourceMetrics)
        assert result.surfaced.status == "measured"
        surfaced_metrics = result.surfaced.metrics
        assert surfaced_metrics is not None
        assert surfaced_metrics.recall[1] == pytest.approx(1.0, abs=_EPSILON)
        assert len(result.ranking.calls) == 1
        ranking_metrics = result.ranking.calls[0].metrics
        assert ranking_metrics is not None
        assert ranking_metrics.precision[1] == pytest.approx(0.0, abs=_EPSILON)


class TestNdcgAtK:
    def test_the_ideal_order_scores_one(self):
        assert ndcg_at_k(["a", "b", "c"], {"a": 2, "b": 1}, 10) == pytest.approx(1.0)

    def test_a_relevant_document_at_rank_two_scores_below_one(self):
        assert ndcg_at_k(["x", "a"], {"a": 1}, 10) == pytest.approx(1 / math.log2(3))

    def test_graded_relevance_weighs_the_higher_grade(self):
        assert ndcg_at_k(["b", "a"], {"a": 2, "b": 1}, 10) < ndcg_at_k(["a", "b"], {"a": 2, "b": 1}, 10)

    def test_a_relevant_document_past_k_does_not_count(self):
        assert ndcg_at_k(["x", "y", "a"], {"a": 1}, 2) == 0.0

    def test_the_gain_is_the_grade_itself_as_trec_eval_counts_it(self):
        ideal = 2 + 1 / math.log2(3)

        assert ndcg_at_k(["a"], {"a": 2, "b": 1}, 10) == pytest.approx(2 / ideal)

    def test_no_relevant_document_scores_zero(self):
        assert ndcg_at_k(["a"], {}, 10) == 0.0
        assert ndcg_at_k(["a"], {"a": 0}, 10) == 0.0
