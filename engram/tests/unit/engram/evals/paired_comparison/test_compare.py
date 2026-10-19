"""The paired comparison: two evaluation runs of one dataset compared case by case, refused when their variants differ in a held field."""

from __future__ import annotations

import dataclasses
import json
import math

from typing import TYPE_CHECKING

import pytest
import yaml

from polyfactory.factories.pydantic_factory import ModelFactory
from pydantic import ValidationError

from engram.evals.harness.infrastructure.results import ResultsHeader as WrittenHeader
from engram.evals.harness.infrastructure.results import ResultsLine as WrittenLine
from engram.evals.harness.infrastructure.results import write_results
from engram.evals.outcomes.contracts import graph_pair_verdict
from engram.evals.paired_comparison.contracts import (
    INTERVAL_Z,
    PairedComparison,
    ResultsFile,
    ResultsFileUnreadableError,
    ResultsHeader,
    ResultsLine,
    RunsNotComparableError,
    SourceLevelRetrieval,
    difference_interval,
    holm_adjusted,
    mcnemar_exact_p,
    paired_comparison_markdown,
    paired_comparisons,
    read_results,
    source_level_retrieval,
    source_level_retrieval_per_query_type,
    write_paired_comparison,
)
from engram.evals.tuning.contracts import RetrievalStrategy

if TYPE_CHECKING:
    from pathlib import Path


class ResultsHeaderFactory(ModelFactory[ResultsHeader]):
    """A results header with fixed values, so two built headers agree unless a test changes one."""

    strategy = RetrievalStrategy.SEARCH_HYBRID
    sql = False
    code_mode = False
    model = "test-model"
    thinking = None
    instructions_sha256 = "a" * 64
    max_tool_calls = 8
    pinned_search = True
    search_date_range = True
    dataset = "sample"
    dataset_sha256 = "b" * 64
    collection = "notes"
    knowledge_base_fingerprint = "hash:notes"
    index_build = "7+500-1000 test-embedding"
    started = "2026-10-13T09:00:00+00:00"
    source_lookup_error = None


def _line(
    case: str,
    *,
    strict: bool,
    query_type: str | None = "alpha",
    failed: bool = False,
    official: bool = False,
    sources: tuple[str, ...] | None = None,
    retrieval: tuple[float, float, float] | None = None,
    seen: float | None = None,
) -> ResultsLine:
    recall_at_4, recall_at_10, mrr = retrieval if retrieval is not None else (None, None, None)
    return ResultsLine(
        case=case,
        query_type=query_type,
        strict=strict,
        official=official,
        failed=failed,
        sources=sources,
        recall_at_4=recall_at_4,
        recall_at_10=recall_at_10,
        mrr=mrr,
        other_collection=False,
        seen_recall=seen,
    )


def _run(path: str, *lines: ResultsLine, header: ResultsHeader | None = None) -> ResultsFile:
    return ResultsFile(path=path, header=header or ResultsHeaderFactory.build(), lines=lines)


def _only_pair(baseline: ResultsFile, candidate: ResultsFile) -> PairedComparison:
    (comparison,) = paired_comparisons([(baseline, candidate)])
    return comparison


# Pairing


def test_cases_pair_by_case_name_whatever_their_order():
    baseline = _run("a.jsonl", _line("c1", strict=True), _line("c2", strict=False))
    candidate = _run("b.jsonl", _line("c2", strict=True), _line("c1", strict=False))

    scores = _only_pair(baseline, candidate).overall

    assert scores.cases == 2
    assert (scores.baseline_only, scores.candidate_only) == (1, 1)


@pytest.mark.parametrize(("missing_from", "only_in"), [("candidate", "a.jsonl"), ("baseline", "b.jsonl")])
def test_a_case_in_one_file_only_is_refused_naming_it_and_the_file(missing_from: str, only_in: str):
    both = (_line("c1", strict=True), _line("c2", strict=False))
    extra = _line("c3", strict=True)
    baseline = _run("a.jsonl", *both, *(() if missing_from == "baseline" else (extra,)))
    candidate = _run("b.jsonl", *both, *(() if missing_from == "candidate" else (extra,)))

    with pytest.raises(RunsNotComparableError, match=rf"c3.*{only_in} only"):
        paired_comparisons([(baseline, candidate)])


def test_a_results_file_that_repeats_a_case_is_refused():
    with pytest.raises(ValidationError, match="repeats the case c1"):
        _run("a.jsonl", _line("c1", strict=True), _line("c2", strict=True), _line("c1", strict=False))


def test_two_runs_with_no_cases_are_refused():
    with pytest.raises(RunsNotComparableError, match="no cases"):
        paired_comparisons([(_run("a.jsonl"), _run("b.jsonl"))])


# Variant checks


@pytest.mark.parametrize(
    ("field", "candidate_header"),
    [
        ("dataset_sha256", ResultsHeaderFactory.build(dataset_sha256="c" * 64)),
        ("model", ResultsHeaderFactory.build(model="other-model")),
        ("instructions_sha256", ResultsHeaderFactory.build(instructions_sha256="d" * 64)),
        ("max_tool_calls", ResultsHeaderFactory.build(max_tool_calls=None)),
        ("pinned_search", ResultsHeaderFactory.build(pinned_search=False)),
        ("search_date_range", ResultsHeaderFactory.build(search_date_range=False)),
        ("collection", ResultsHeaderFactory.build(collection="other-notes", knowledge_base_fingerprint="hash:other-notes")),
        ("thinking", ResultsHeaderFactory.build(thinking="xhigh")),
    ],
)
def test_a_pair_that_differs_in_a_held_field_is_refused_naming_it(field: str, candidate_header: ResultsHeader):
    lines = (_line("c1", strict=True),)

    with pytest.raises(RunsNotComparableError, match=rf"differ in {field}\b"):
        paired_comparisons([(_run("a.jsonl", *lines), _run("b.jsonl", *lines, header=candidate_header))])


def test_a_held_field_is_named_before_a_case_in_one_run_only():
    candidate_header = ResultsHeaderFactory.build(dataset_sha256="c" * 64)

    with pytest.raises(RunsNotComparableError, match=r"differ in dataset_sha256\b"):
        paired_comparisons(
            [(_run("a.jsonl", _line("c1", strict=True)), _run("b.jsonl", _line("c2", strict=True), header=candidate_header))]
        )


def test_different_strategies_the_verdict_calls_unfair_are_refused_with_its_reason():
    lines = (_line("c1", strict=True),)
    baseline_header = ResultsHeaderFactory.build(strategy=RetrievalStrategy.SEARCH_KEYWORD)
    candidate_header = ResultsHeaderFactory.build(strategy=RetrievalStrategy.FULL)
    reason = graph_pair_verdict(RetrievalStrategy.SEARCH_KEYWORD, RetrievalStrategy.FULL).reason

    with pytest.raises(RunsNotComparableError) as refused:
        paired_comparisons([(_run("a.jsonl", *lines, header=baseline_header), _run("b.jsonl", *lines, header=candidate_header))])

    assert reason in str(refused.value)


def test_different_strategies_with_different_sql_switches_are_refused_naming_sql():
    lines = (_line("c1", strict=True),)
    baseline_header = ResultsHeaderFactory.build(strategy=RetrievalStrategy.SEARCH_HYBRID, sql=False)
    candidate_header = ResultsHeaderFactory.build(strategy=RetrievalStrategy.FULL, sql=True)

    with pytest.raises(RunsNotComparableError, match=r"differ in sql\b"):
        paired_comparisons([(_run("a.jsonl", *lines, header=baseline_header), _run("b.jsonl", *lines, header=candidate_header))])


def test_different_strategies_with_different_code_modes_are_refused_naming_code_mode():
    lines = (_line("c1", strict=True),)
    baseline_header = ResultsHeaderFactory.build(strategy=RetrievalStrategy.SEARCH_HYBRID, code_mode=False)
    candidate_header = ResultsHeaderFactory.build(strategy=RetrievalStrategy.FULL, code_mode=True)

    with pytest.raises(RunsNotComparableError, match=r"differ in code_mode\b"):
        paired_comparisons([(_run("a.jsonl", *lines, header=baseline_header), _run("b.jsonl", *lines, header=candidate_header))])


def test_different_strategies_the_verdict_calls_fair_with_one_sql_switch_are_compared():
    lines = (_line("c1", strict=True),)
    baseline_header = ResultsHeaderFactory.build(strategy=RetrievalStrategy.SEARCH_HYBRID, sql=True)
    candidate_header = ResultsHeaderFactory.build(strategy=RetrievalStrategy.FULL, sql=True)

    comparison = _only_pair(_run("a.jsonl", *lines, header=baseline_header), _run("b.jsonl", *lines, header=candidate_header))

    assert comparison.overall.cases == 1


def test_one_strategy_may_differ_in_sql_and_fingerprint():
    lines = (_line("c1", strict=True),)
    candidate_header = ResultsHeaderFactory.build(sql=True, knowledge_base_fingerprint="other:notes")

    comparison = _only_pair(_run("a.jsonl", *lines), _run("b.jsonl", *lines, header=candidate_header))

    assert comparison.overall.cases == 1


def test_one_strategy_may_differ_in_code_mode():
    lines = (_line("c1", strict=True),)
    baseline_header = ResultsHeaderFactory.build(sql=True, thinking="xhigh")
    candidate_header = ResultsHeaderFactory.build(sql=True, code_mode=True, thinking="xhigh")

    comparison = _only_pair(_run("a.jsonl", *lines, header=baseline_header), _run("b.jsonl", *lines, header=candidate_header))

    assert comparison.overall.cases == 1
    assert (comparison.baseline_header.code_mode, comparison.candidate_header.code_mode) == (False, True)


def test_one_strategy_differing_in_both_sql_and_code_mode_is_refused_naming_both():
    lines = (_line("c1", strict=True),)
    candidate_header = ResultsHeaderFactory.build(sql=True, code_mode=True)

    with pytest.raises(RunsNotComparableError, match=r"differ in both sql \(False and True\) and code_mode \(False and True\)"):
        paired_comparisons([(_run("a.jsonl", *lines), _run("b.jsonl", *lines, header=candidate_header))])


# The statistics


def test_mcnemar_exact_p_is_the_two_sided_binomial_tail():
    by_hand = min(1.0, 2 * sum(math.comb(12, k) for k in range(3)) / 2**12)

    assert mcnemar_exact_p(10, 2) == pytest.approx(by_hand)
    assert round(mcnemar_exact_p(10, 2), 4) == 0.0386
    assert mcnemar_exact_p(2, 10) == mcnemar_exact_p(10, 2)


@pytest.mark.parametrize("discordant", [0, 1, 7])
def test_mcnemar_exact_p_of_equal_discordant_counts_is_one(discordant: int):
    assert mcnemar_exact_p(discordant, discordant) == 1.0


def test_the_interval_is_the_mean_difference_plus_or_minus_z_standard_errors():
    differences = [1, 1, 0, -1]
    mean = 0.25
    sample_sd = math.sqrt(((0.75**2) * 2 + 0.25**2 + 1.25**2) / 3)
    half = INTERVAL_Z * sample_sd / math.sqrt(4)

    low, high = difference_interval(differences) or (math.nan, math.nan)

    assert INTERVAL_Z == 1.96
    assert low == pytest.approx(mean - half)
    assert high == pytest.approx(mean + half)


def test_one_case_has_no_interval():
    assert difference_interval([1]) is None


def test_holm_multiplies_the_ranked_p_values_and_keeps_them_in_order():
    assert holm_adjusted([0.01, 0.04, 0.03]) == pytest.approx((0.03, 0.06, 0.06))


def test_holm_caps_an_adjusted_p_value_at_one():
    assert holm_adjusted([0.6, 0.9]) == pytest.approx((1.0, 1.0))


# Scores


def test_a_failed_case_still_pairs_and_counts_as_incorrect():
    baseline = _run("a.jsonl", _line("c1", strict=False, failed=True), _line("c2", strict=True))
    candidate = _run("b.jsonl", _line("c1", strict=True), _line("c2", strict=True))

    scores = _only_pair(baseline, candidate).overall

    assert scores.cases == 2
    assert scores.baseline_strict == 0.5
    assert scores.candidate_only == 1


def test_a_failed_case_is_incorrect_even_when_its_line_says_strict():
    baseline = _run("a.jsonl", _line("c1", strict=True, official=True, failed=True))
    candidate = _run("b.jsonl", _line("c1", strict=True, official=True))

    scores = _only_pair(baseline, candidate).overall

    assert (scores.baseline_strict, scores.baseline_official) == (0.0, 0.0)
    assert scores.candidate_only == 1


def test_a_pair_scores_both_accuracies_the_difference_the_discordant_cases_and_the_other_knowledge_base_counts():
    baseline = _run(
        "a.jsonl",
        _line("c1", strict=True, official=True),
        _line("c2", strict=False, official=True),
        _line("c3", strict=False),
        _line("c4", strict=True, official=True).model_copy(update={"other_collection": True}),
    )
    candidate = _run(
        "b.jsonl",
        _line("c1", strict=True, official=True),
        _line("c2", strict=True, official=True),
        _line("c3", strict=True, official=True),
        _line("c4", strict=False),
    )

    scores = _only_pair(baseline, candidate).overall

    assert scores.cases == 4
    assert (scores.baseline_strict, scores.candidate_strict) == (0.5, 0.75)
    assert scores.difference == 0.25
    assert (scores.baseline_only, scores.candidate_only) == (1, 2)
    assert scores.p_value == mcnemar_exact_p(1, 2)
    assert (scores.baseline_official, scores.candidate_official) == (0.75, 0.75)
    assert (scores.baseline_other_collection, scores.candidate_other_collection) == (1, 0)
    assert scores.interval == difference_interval([0, 1, 1, -1])


def test_each_side_counts_its_failed_cases_overall_and_per_query_type():
    baseline = _run(
        "a.jsonl",
        _line("c1", strict=False, failed=True),
        _line("c2", strict=False, failed=True, query_type="beta"),
        _line("c3", strict=True, query_type="beta"),
    )
    candidate = _run("b.jsonl", _line("c1", strict=True), _line("c2", strict=False, failed=True, query_type="beta"), _line("c3", strict=True, query_type="beta"))

    comparison = _only_pair(baseline, candidate)

    assert (comparison.overall.baseline_failed, comparison.overall.candidate_failed) == (2, 1)
    alpha, beta = (row.scores for row in comparison.query_types)
    assert (alpha.baseline_failed, alpha.candidate_failed) == (1, 0)
    assert (beta.baseline_failed, beta.candidate_failed) == (1, 1)


def test_source_level_retrieval_is_the_mean_over_the_cases_with_gold_source_ids_a_failed_case_scoring_zero():
    lines = (
        _line("c1", strict=True, sources=("s1", "s2"), retrieval=(1.0, 1.0, 1.0)),
        _line("c2", strict=False, sources=("s3",), retrieval=(0.5, 1.0, 0.5)),
        _line("c3", strict=False, failed=True, sources=(), retrieval=(0.0, 0.0, 0.0)),
        _line("c4", strict=True, sources=("s4",)),
        _line("c5", strict=True, sources=("s5", "s6"), retrieval=(0.5, 1.0, 0.5)),
    )

    retrieval = source_level_retrieval(lines)

    assert retrieval == SourceLevelRetrieval(cases=4, recall_at_4=0.5, recall_at_10=0.75, mrr=0.5)


def test_a_run_that_mapped_no_sources_has_no_source_level_retrieval_even_when_its_lines_score_page_ids():
    lines = (_line("c1", strict=True, retrieval=(1.0, 1.0, 1.0)), _line("c2", strict=True, retrieval=(0.0, 0.0, 0.0)))

    assert source_level_retrieval(lines) is None


def test_a_run_with_no_gold_source_ids_has_no_source_level_retrieval():
    assert source_level_retrieval((_line("c1", strict=True, sources=("s1",)),)) is None


def test_a_pair_carries_each_side_s_source_level_retrieval_overall_and_per_query_type():
    baseline = _run(
        "a.jsonl",
        _line("c1", strict=True, sources=("s1",), retrieval=(1.0, 1.0, 1.0)),
        _line("c2", strict=False, query_type="beta", sources=("s9",), retrieval=(0.0, 0.0, 0.0)),
    )
    candidate = _run(
        "b.jsonl",
        _line("c1", strict=True, sources=("s1",), retrieval=(1.0, 1.0, 1.0)),
        _line("c2", strict=True, query_type="beta", sources=("s2",), retrieval=(0.5, 1.0, 0.5)),
    )

    comparison = _only_pair(baseline, candidate)

    assert comparison.overall.baseline_retrieval == SourceLevelRetrieval(cases=2, recall_at_4=0.5, recall_at_10=0.5, mrr=0.5)
    assert comparison.overall.candidate_retrieval == SourceLevelRetrieval(cases=2, recall_at_4=0.75, recall_at_10=1.0, mrr=0.75)
    alpha, beta = (row.scores for row in comparison.query_types)
    assert alpha.baseline_retrieval == SourceLevelRetrieval(cases=1, recall_at_4=1.0, recall_at_10=1.0, mrr=1.0)
    assert beta.candidate_retrieval == SourceLevelRetrieval(cases=1, recall_at_4=0.5, recall_at_10=1.0, mrr=0.5)


def test_source_level_retrieval_carries_the_mean_seen_recall_a_failed_case_counting_what_it_saw():
    lines = (
        _line("c1", strict=True, sources=("s1",), retrieval=(1.0, 1.0, 1.0), seen=1.0),
        _line("c2", strict=False, sources=("s3",), retrieval=(0.0, 0.0, 0.0), seen=0.5),
        _line("c3", strict=False, failed=True, sources=(), retrieval=(0.0, 0.0, 0.0), seen=0.0),
    )

    retrieval = source_level_retrieval(lines)

    assert retrieval is not None
    assert retrieval.seen_recall == pytest.approx(0.5)


def test_a_run_whose_lines_hold_no_seen_recall_has_none_rather_than_zero():
    retrieval = source_level_retrieval((_line("c1", strict=True, sources=("s1",), retrieval=(1.0, 1.0, 1.0)),))

    assert retrieval is not None
    assert retrieval.seen_recall is None


def test_source_level_retrieval_per_query_type_orders_by_name_with_the_cases_that_have_none_last():
    lines = (
        _line("c1", strict=True, query_type=None, sources=("s1",), retrieval=(1.0, 1.0, 1.0), seen=1.0),
        _line("c2", strict=True, query_type="beta", sources=("s2",), retrieval=(0.0, 0.0, 0.0), seen=0.5),
        _line("c3", strict=True, query_type="alpha", sources=("s3",), retrieval=(0.5, 1.0, 0.5), seen=1.0),
        _line("c4", strict=True, query_type="gamma"),
    )

    per_type = source_level_retrieval_per_query_type(lines)

    assert [(row.query_type, row.retrieval.cases, row.retrieval.seen_recall) for row in per_type] == [
        ("alpha", 1, 1.0),
        ("beta", 1, 0.5),
        (None, 1, 1.0),
    ]


def test_a_pair_reports_both_seen_recall_means_and_their_paired_difference_overall_and_per_query_type():
    baseline = _run(
        "a.jsonl",
        _line("c1", strict=True, sources=("s1",), retrieval=(0.0, 0.0, 0.0), seen=0.0),
        _line("c2", strict=True, query_type="beta", sources=("s2",), retrieval=(0.0, 0.0, 0.0), seen=0.5),
    )
    candidate = _run(
        "b.jsonl",
        _line("c1", strict=True, sources=("s1",), retrieval=(0.0, 0.0, 0.0), seen=1.0),
        _line("c2", strict=True, query_type="beta", sources=("s2",), retrieval=(0.0, 0.0, 0.0), seen=0.5),
    )

    comparison = _only_pair(baseline, candidate)

    overall = comparison.overall
    assert overall.baseline_retrieval is not None
    assert overall.candidate_retrieval is not None
    assert (overall.baseline_retrieval.seen_recall, overall.candidate_retrieval.seen_recall) == (0.25, 0.75)
    assert overall.seen_recall_difference == pytest.approx(0.5)
    alpha, beta = (row.scores for row in comparison.query_types)
    assert (alpha.seen_recall_difference, beta.seen_recall_difference) == (pytest.approx(1.0), pytest.approx(0.0))
    markdown = paired_comparison_markdown([comparison])
    assert "| Query type | Baseline seen recall | Candidate seen recall | Seen recall difference |" in markdown
    assert "| all cases | 0.250 | 0.750 | +0.500 |" in markdown
    assert "| beta | 0.500 | 0.500 | +0.000 |" in markdown


def test_an_older_results_file_has_no_seen_recall_and_the_report_shows_it_missing_never_zero(tmp_path: Path):
    path = tmp_path / "older.jsonl"
    scored = dataclasses.replace(_written_line("c1", strict=True), sources=("s1",), recall_at_4=1.0, recall_at_10=1.0, mrr=1.0)
    write_results(path, _WRITTEN_HEADER, [scored])
    header_row, *case_rows = path.read_text(encoding="utf-8").splitlines()
    older_rows = [json.loads(row) for row in case_rows]
    for row in older_rows:
        del row["seen_recall"], row["seen_gold"]
    path.write_text("\n".join([header_row, *(json.dumps(row) for row in older_rows)]) + "\n", encoding="utf-8")
    older = read_results(path)
    candidate = _run("b.jsonl", _line("c1", strict=True, sources=("s1",), retrieval=(1.0, 1.0, 1.0), seen=0.75), header=older.header)

    comparison = _only_pair(older, candidate)

    assert older.lines[0].seen_recall is None
    assert comparison.overall.baseline_retrieval is not None
    assert comparison.overall.baseline_retrieval.seen_recall is None
    assert comparison.overall.seen_recall_difference is None
    assert "| all cases | missing | 0.750 | missing |" in paired_comparison_markdown([comparison])


def test_a_side_whose_source_lookup_failed_has_no_source_level_retrieval():
    failed = ResultsHeaderFactory.build(source_lookup_error="LookupError: no sources")
    baseline = _run("a.jsonl", _line("c1", strict=True, sources=("s1",), retrieval=(1.0, 1.0, 1.0)))
    candidate = _run("b.jsonl", _line("c1", strict=True, sources=("s1",), retrieval=(1.0, 1.0, 1.0)), header=failed)

    scores = _only_pair(baseline, candidate).overall

    assert scores.baseline_retrieval == SourceLevelRetrieval(cases=1, recall_at_4=1.0, recall_at_10=1.0, mrr=1.0)
    assert scores.candidate_retrieval is None


def test_each_query_type_is_scored_on_its_own_and_cases_with_none_come_last():
    baseline = _run(
        "a.jsonl",
        _line("c1", strict=False, query_type=None),
        _line("c2", strict=False, query_type="beta"),
        _line("c3", strict=True, query_type="alpha"),
        _line("c4", strict=False, query_type="alpha"),
    )
    candidate = _run(
        "b.jsonl",
        _line("c1", strict=True, query_type=None),
        _line("c2", strict=False, query_type="beta"),
        _line("c3", strict=True, query_type="alpha"),
        _line("c4", strict=True, query_type="alpha"),
    )

    comparison = _only_pair(baseline, candidate)

    assert [row.query_type for row in comparison.query_types] == ["alpha", "beta", None]
    alpha, beta, untyped = (row.scores for row in comparison.query_types)
    assert (alpha.cases, alpha.baseline_strict, alpha.candidate_strict) == (2, 0.5, 1.0)
    assert (beta.cases, beta.candidate_only) == (1, 0)
    assert (untyped.cases, untyped.candidate_only) == (1, 1)


def test_holm_adjusts_across_every_pair_of_the_call_overall_and_within_each_query_type():
    strong = ([_line(f"c{index}", strict=False) for index in range(10)], [_line(f"c{index}", strict=True) for index in range(10)])
    weak = ([_line("c0", strict=False), _line("c1", strict=True)], [_line("c0", strict=True), _line("c1", strict=True)])
    pairs = [(_run("a.jsonl", *baseline), _run("b.jsonl", *candidate)) for baseline, candidate in (strong, weak)]

    first, second = paired_comparisons(pairs)

    expected = holm_adjusted([mcnemar_exact_p(0, 10), mcnemar_exact_p(0, 1)])
    assert (first.overall.holm_p_value, second.overall.holm_p_value) == pytest.approx(expected)
    assert (first.query_types[0].scores.holm_p_value, second.query_types[0].scores.holm_p_value) == pytest.approx(expected)


def test_a_query_type_in_one_pair_only_is_adjusted_across_that_pair_alone():
    cases = range(10)
    first_pair = (
        _run("a.jsonl", *(_line(f"c{index}", strict=False) for index in cases)),
        _run("b.jsonl", *(_line(f"c{index}", strict=True) for index in cases)),
    )
    second_pair = (
        _run("c.jsonl", *(_line(f"c{index}", strict=False, query_type="beta") for index in cases)),
        _run("d.jsonl", *(_line(f"c{index}", strict=True, query_type="beta") for index in cases)),
    )

    first, second = paired_comparisons([first_pair, second_pair])

    p_value = mcnemar_exact_p(0, 10)
    assert (first.overall.holm_p_value, second.overall.holm_p_value) == pytest.approx((2 * p_value, 2 * p_value))
    assert (first.query_types[0].scores.holm_p_value, second.query_types[0].scores.holm_p_value) == pytest.approx((p_value, p_value))


# The results file


def _written_line(case: str, *, strict: bool, query_type: str | None = "alpha", failed: bool = False) -> WrittenLine:
    return WrittenLine(
        case=case,
        query_type=query_type,
        strict=strict,
        official=strict,
        failed=failed,
        error="budget spent" if failed else None,
        answer=None if failed else "yes",
        cited=(),
        sources=None,
        recall_at_4=None,
        recall_at_10=None,
        mrr=None,
        other_collection=False,
    )


def test_the_reader_reads_the_sources_and_the_retrieval_scores(tmp_path: Path):
    path = tmp_path / "a.jsonl"
    scored = dataclasses.replace(_written_line("c1", strict=True), sources=("s1", "s2"), recall_at_4=0.5, recall_at_10=1.0, mrr=0.25)
    write_results(path, _WRITTEN_HEADER, [scored, _written_line("c2", strict=False)])

    first, second = read_results(path).lines

    assert (first.sources, first.recall_at_4, first.recall_at_10, first.mrr) == (("s1", "s2"), 0.5, 1.0, 0.25)
    assert (second.sources, second.recall_at_4, second.recall_at_10, second.mrr) == (None, None, None, None)


def test_the_reader_reads_a_citation_that_holds_no_source(tmp_path: Path):
    path = tmp_path / "a.jsonl"
    scored = dataclasses.replace(_written_line("c1", strict=True), sources=(None, "s1", None), recall_at_4=1.0, recall_at_10=1.0, mrr=0.5)
    write_results(path, _WRITTEN_HEADER, [scored])

    (line,) = read_results(path).lines

    assert (line.sources, line.mrr) == ((None, "s1", None), 0.5)


_WRITTEN_HEADER = WrittenHeader(
    strategy="search-hybrid",
    sql=True,
    code_mode=True,
    model="test-model",
    thinking="xhigh",
    instructions_sha256="a" * 64,
    max_tool_calls=8,
    pinned_search=True,
    dataset="sample",
    dataset_sha256="b" * 64,
    collection="notes",
    knowledge_base_fingerprint="hash:notes",
    index_build="7+500-1000 test-embedding",
    started="2026-10-13T09:00:00+00:00",
    source_lookup_error=None,
)


def test_the_reader_reads_what_an_evaluation_run_writes(tmp_path: Path):
    path = tmp_path / "a.jsonl"
    write_results(
        path, _WRITTEN_HEADER, [_written_line("c1", strict=True), _written_line("c2", strict=False, query_type=None, failed=True)]
    )

    results = read_results(path)

    assert results.path == str(path)
    assert results.header == ResultsHeaderFactory.build(sql=True, code_mode=True, thinking="xhigh")
    assert results.lines == (_line("c1", strict=True, official=True), _line("c2", strict=False, query_type=None, failed=True))


def test_a_header_written_before_code_mode_and_thinking_were_recorded_reads_them_as_off_and_unset(tmp_path: Path):
    path = tmp_path / "a.jsonl"
    write_results(path, _WRITTEN_HEADER, [_written_line("c1", strict=True)])
    header_row, *case_rows = path.read_text(encoding="utf-8").splitlines()
    older = json.loads(header_row)
    del older["header"]["code_mode"], older["header"]["thinking"]
    path.write_text("\n".join([json.dumps(older), *case_rows]) + "\n", encoding="utf-8")

    header = read_results(path).header

    assert (header.code_mode, header.thinking) == (False, None)


def _older_run(tmp_path: Path, name: str, *, pinned_search: bool) -> ResultsFile:
    """A results file written before the header recorded whether `search` took a date range, read back."""
    path = tmp_path / name
    write_results(path, dataclasses.replace(_WRITTEN_HEADER, pinned_search=pinned_search), [_written_line("c1", strict=True)])
    header_row, *case_rows = path.read_text(encoding="utf-8").splitlines()
    older = json.loads(header_row)
    del older["header"]["search_date_range"]
    path.write_text("\n".join([json.dumps(older), *case_rows]) + "\n", encoding="utf-8")
    return read_results(path)


def _new_run(tmp_path: Path, name: str, *, pinned_search: bool) -> ResultsFile:
    """A results file a run writes now, whose `search` always took a date range, read back."""
    path = tmp_path / name
    write_results(path, dataclasses.replace(_WRITTEN_HEADER, pinned_search=pinned_search), [_written_line("c1", strict=False)])
    return read_results(path)


@pytest.mark.parametrize("pinned_search", [True, False])
def test_a_header_that_did_not_record_the_date_range_reads_it_as_the_opposite_of_pinned_search(tmp_path: Path, pinned_search: bool):
    assert _older_run(tmp_path, "a.jsonl", pinned_search=pinned_search).header.search_date_range is (not pinned_search)


def test_an_older_pinned_run_and_a_pinned_run_now_are_refused_naming_search_date_range(tmp_path: Path):
    older, newer = _older_run(tmp_path, "a.jsonl", pinned_search=True), _new_run(tmp_path, "b.jsonl", pinned_search=True)

    with pytest.raises(RunsNotComparableError, match=r"differ in search_date_range \(False and True\)"):
        paired_comparisons([(older, newer)])


def test_an_older_unpinned_run_pairs_with_a_run_now(tmp_path: Path):
    older, newer = _older_run(tmp_path, "a.jsonl", pinned_search=False), _new_run(tmp_path, "b.jsonl", pinned_search=False)

    comparison = _only_pair(older, newer)

    assert (comparison.overall.cases, comparison.overall.baseline_only) == (1, 1)


def test_a_header_written_before_the_index_build_was_recorded_reads_it_as_unknown(tmp_path: Path):
    path = tmp_path / "a.jsonl"
    write_results(path, _WRITTEN_HEADER, [_written_line("c1", strict=True)])
    header_row, *case_rows = path.read_text(encoding="utf-8").splitlines()
    older = json.loads(header_row)
    del older["header"]["index_build"]
    path.write_text("\n".join([json.dumps(older), *case_rows]) + "\n", encoding="utf-8")

    results = read_results(path)

    assert results.header.index_build is None
    assert "knowledge base hash:notes, index build unknown." in paired_comparison_markdown(paired_comparisons([(results, results)]))


def test_two_runs_on_different_index_builds_are_compared_like_two_fingerprints_and_the_report_shows_both():
    lines = (_line("c1", strict=True),)
    rechunked = ResultsHeaderFactory.build(index_build="7+300-600 test-embedding")

    comparison = _only_pair(_run("a.jsonl", *lines), _run("b.jsonl", *lines, header=rechunked))

    markdown = paired_comparison_markdown([comparison])
    assert comparison.overall.cases == 1
    assert "Baseline: strategy search-hybrid, SQL off, code mode off, no thinking level, knowledge base hash:notes, index build 7+500-1000 test-embedding." in markdown
    assert "Candidate: strategy search-hybrid, SQL off, code mode off, no thinking level, knowledge base hash:notes, index build 7+300-600 test-embedding." in markdown


def test_the_reader_keeps_a_failed_source_lookup(tmp_path: Path):
    path = tmp_path / "a.jsonl"
    write_results(
        path, dataclasses.replace(_WRITTEN_HEADER, source_lookup_error="LookupError: no sources"), [_written_line("c1", strict=True)]
    )

    assert read_results(path).header.source_lookup_error == "LookupError: no sources"


@pytest.mark.parametrize("separator", ["\u2028", "\u2029", "\u0085"])
def test_a_case_line_whose_answer_holds_a_line_separator_is_read(tmp_path: Path, separator: str):
    path = tmp_path / "a.jsonl"
    answered = dataclasses.replace(_written_line("c1", strict=True), answer=f"first{separator}second")
    write_results(path, _WRITTEN_HEADER, [answered, _written_line("c2", strict=False)])

    assert [line.case for line in read_results(path).lines] == ["c1", "c2"]


def test_a_missing_results_file_is_refused_naming_it(tmp_path: Path):
    with pytest.raises(ResultsFileUnreadableError, match=r"missing\.jsonl"):
        read_results(tmp_path / "missing.jsonl")


def test_an_unknown_strategy_is_refused_naming_line_one(tmp_path: Path):
    path = tmp_path / "a.jsonl"
    write_results(path, dataclasses.replace(_WRITTEN_HEADER, strategy="no-such-strategy"), [_written_line("c1", strict=True)])

    with pytest.raises(ResultsFileUnreadableError, match="line 1"):
        read_results(path)


def test_a_case_line_that_does_not_parse_is_refused_naming_its_line(tmp_path: Path):
    path = tmp_path / "a.jsonl"
    write_results(path, _WRITTEN_HEADER, [_written_line("c1", strict=True)])
    path.write_text(path.read_text(encoding="utf-8") + '{"case": "c2"}\n', encoding="utf-8")

    with pytest.raises(ResultsFileUnreadableError, match="line 3"):
        read_results(path)


def test_a_case_written_twice_is_refused_naming_the_line_and_the_case(tmp_path: Path):
    path = tmp_path / "a.jsonl"
    write_results(path, _WRITTEN_HEADER, [_written_line("c1", strict=True), _written_line("c1", strict=False)])

    with pytest.raises(ResultsFileUnreadableError, match=r"line 3.*c1"):
        read_results(path)


# The report


def _two_pairs() -> list[tuple[ResultsFile, ResultsFile]]:
    baseline = (_line("c1", strict=False), _line("c2", strict=True, query_type=None))
    candidate = (_line("c1", strict=True), _line("c2", strict=True, query_type=None))
    sql_on = ResultsHeaderFactory.build(sql=True)
    return [
        (_run("runs/a.jsonl", *baseline), _run("runs/b.jsonl", *candidate, header=sql_on)),
        (_run("runs/b.jsonl", *candidate, header=sql_on), _run("runs/c.jsonl", *candidate, header=sql_on)),
    ]


def test_the_markdown_has_one_section_per_pair_with_a_row_for_all_cases_and_one_per_query_type():
    markdown = paired_comparison_markdown(paired_comparisons(_two_pairs()))

    assert "## runs/b.jsonl against runs/a.jsonl\n" in markdown
    assert "## runs/c.jsonl against runs/b.jsonl\n" in markdown
    assert "| all cases | 2 | 50.0% | 100.0% | +50.0 (" in markdown
    assert "| alpha | 1 | 0.0% | 100.0% | +100.0 (no interval) |" in markdown
    assert "| no query type | 1 |" in markdown
    assert "question type" not in markdown


def test_the_markdown_counts_each_side_s_failed_cases():
    baseline = _run("a.jsonl", _line("c1", strict=False, failed=True), _line("c2", strict=True, official=True))
    candidate = _run("b.jsonl", _line("c1", strict=True, official=True), _line("c2", strict=True, official=True))

    markdown = paired_comparison_markdown(paired_comparisons([(baseline, candidate)]))

    assert "| Query type | Baseline official | Candidate official | Baseline failed | Candidate failed |" in markdown
    assert "| all cases | 50.0% | 100.0% | 1 | 0 | 0 | 0 |" in markdown


def test_the_markdown_gives_each_side_s_source_level_retrieval_and_none_for_a_side_that_scored_none():
    baseline = _run("a.jsonl", _line("c1", strict=True, sources=("s1",), retrieval=(0.5, 1.0, 0.25)))
    candidate = _run("b.jsonl", _line("c1", strict=True, retrieval=(1.0, 1.0, 1.0)))

    markdown = paired_comparison_markdown(paired_comparisons([(baseline, candidate)]))

    assert (
        "| Query type | Baseline scored | Candidate scored | Baseline recall@4 | Candidate recall@4 "
        "| Baseline recall@10 | Candidate recall@10 | Baseline MRR | Candidate MRR |"
    ) in markdown
    assert "| all cases | 1 | 0 | 0.500 | none | 1.000 | none | 0.250 | none |" in markdown


def test_the_markdown_shows_each_side_s_code_mode_and_thinking_level():
    lines = (_line("c1", strict=True),)
    baseline_header = ResultsHeaderFactory.build(sql=True, thinking="xhigh")
    candidate_header = ResultsHeaderFactory.build(sql=True, code_mode=True, thinking="xhigh")
    unset = ResultsHeaderFactory.build()

    shown = paired_comparison_markdown(
        paired_comparisons([(_run("a.jsonl", *lines, header=baseline_header), _run("b.jsonl", *lines, header=candidate_header))])
    )
    default = paired_comparison_markdown(
        paired_comparisons([(_run("a.jsonl", *lines, header=unset), _run("b.jsonl", *lines, header=unset))])
    )

    assert "Baseline: strategy search-hybrid, SQL on, code mode off, thinking xhigh, knowledge base hash:notes, index build 7+500-1000 test-embedding." in shown
    assert "Candidate: strategy search-hybrid, SQL on, code mode on, thinking xhigh, knowledge base hash:notes, index build 7+500-1000 test-embedding." in shown
    assert "Baseline: strategy search-hybrid, SQL off, code mode off, no thinking level, knowledge base hash:notes, index build 7+500-1000 test-embedding." in default


def test_the_markdown_notes_each_side_whose_source_lookup_failed():
    lines = (_line("c1", strict=True),)
    failed = ResultsHeaderFactory.build(source_lookup_error="LookupError: no sources")

    noted = paired_comparison_markdown(paired_comparisons([(_run("a.jsonl", *lines), _run("b.jsonl", *lines, header=failed))]))
    clean = paired_comparison_markdown(paired_comparisons([(_run("a.jsonl", *lines), _run("b.jsonl", *lines))]))

    assert "\nThe candidate's source lookup failed (LookupError: no sources), so its results file holds no source-level recall.\n" in noted
    assert "The baseline's source lookup failed" not in noted
    assert "source lookup failed" not in clean


def test_the_report_writes_the_markdown_and_a_yaml_twin(tmp_path: Path):
    comparisons = paired_comparisons(_two_pairs())

    markdown, twin = write_paired_comparison(comparisons, out=tmp_path / "reports" / "compare.md")

    assert (markdown, twin) == (tmp_path / "reports" / "compare.md", tmp_path / "reports" / "compare.yaml")
    assert markdown.read_text(encoding="utf-8") == paired_comparison_markdown(comparisons)
    document = yaml.safe_load(twin.read_text(encoding="utf-8"))
    assert [pair["baseline"] for pair in document["pairs"]] == ["runs/a.jsonl", "runs/b.jsonl"]
    assert document["pairs"][0]["baseline_header"]["strategy"] == "search-hybrid"
    assert document["pairs"][0]["overall"]["candidate_only"] == 1


def test_a_report_path_that_is_not_markdown_is_refused(tmp_path: Path):
    with pytest.raises(ValueError, match=r"\.md"):
        write_paired_comparison(paired_comparisons(_two_pairs()), out=tmp_path / "compare.yaml")
