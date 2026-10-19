"""The per-case results file: a header line, then one line per case, a failed case written as incorrect."""

from __future__ import annotations

import json
import os

from dataclasses import replace
from typing import TYPE_CHECKING

import pytest

from engram.evals.generation.contracts import AuthoredCase, EvaluationDataset, build_dataset, load_dataset, save_dataset
from engram.evals.harness.infrastructure.results import ResultsHeader, dataset_sha256, read_results, results_lines, write_results
from engram.evals.scoring.contracts import SOURCE_SCORING_VERSION
from engram.evals.shared.domain import ExpectedQa, QaCaseMeta, QaInput, RetrievalAnswer

if TYPE_CHECKING:
    from pathlib import Path

    from engram.evals.harness.contracts import EvaluationRunReport

_HEADER = ResultsHeader(
    strategy="search-hybrid",
    sql=True,
    code_mode=True,
    model="test-model",
    thinking="high",
    instructions_sha256="a" * 64,
    max_tool_calls=8,
    pinned_search=True,
    search_date_range=True,
    dataset="sample",
    dataset_sha256="b" * 64,
    collection="notes",
    knowledge_base_fingerprint="hash:notes",
    index_build="7+500-1000 test-embedding",
    started="2026-10-13T09:00:00+00:00",
    source_lookup_error=None,
    source_scoring_version=SOURCE_SCORING_VERSION,
)

_LINE_KEYS = {
    "case",
    "query_type",
    "strict",
    "official",
    "failed",
    "error",
    "answer",
    "cited",
    "sources",
    "recall@4",
    "recall@10",
    "mrr",
    "other_collection",
    "transient",
    "seen_recall",
    "seen_gold",
}


def _case(name: str, *, answer: str, gold: tuple[str, ...], query_type: str | None = None) -> AuthoredCase:
    return AuthoredCase(
        question=QaInput(question=f"question {name}"),
        expected=ExpectedQa(answer=answer, expected_source_ids=gold),
        meta=QaCaseMeta(query_type=query_type),
        case_name=name,
    )


def _dataset() -> EvaluationDataset:
    return build_dataset(
        [
            _case("case-a", answer="yes", gold=("note.alpha", "note.beta"), query_type="comparison"),
            _case("case-b", answer="north", gold=("note.gamma",)),
            _case("case-c", answer="no", gold=(), query_type="unanswerable"),
        ]
    )


_ANSWERS = {
    "question case-a": RetrievalAnswer(answer="Yes.", cited_source_ids=["note.beta#^c2", "note.zeta", "note.alpha"]),
    "question case-c": RetrievalAnswer(answer="no it did not", cited_source_ids=["note.alpha"]),
}


async def _answer(inp: QaInput) -> RetrievalAnswer:
    if inp.question not in _ANSWERS:
        msg = "the model stopped answering"
        raise RuntimeError(msg)
    return _ANSWERS[inp.question]


async def _report() -> EvaluationRunReport:
    return await _dataset().evaluate(_answer, progress=False)


def _read(path: Path) -> list[dict[str, object]]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]


async def test_the_file_opens_with_the_header_and_holds_one_line_per_case(tmp_path: Path) -> None:
    lines = results_lines(await _report(), sources=None, other_collection=frozenset(), case_order=("case-a", "case-b", "case-c"))
    path = tmp_path / "runs" / "results.jsonl"

    write_results(path, _HEADER, lines)

    written = _read(path)
    assert written[0] == {
        "header": {
            "strategy": "search-hybrid",
            "sql": True,
            "code_mode": True,
            "model": "test-model",
            "thinking": "high",
            "instructions_sha256": "a" * 64,
            "max_tool_calls": 8,
            "pinned_search": True,
            "search_date_range": True,
            "dataset": "sample",
            "dataset_sha256": "b" * 64,
            "collection": "notes",
            "knowledge_base_fingerprint": "hash:notes",
            "index_build": "7+500-1000 test-embedding",
            "started": "2026-10-13T09:00:00+00:00",
            "source_lookup_error": None,
            "source_scoring_version": SOURCE_SCORING_VERSION,
        }
    }
    assert [line["case"] for line in written[1:]] == ["case-a", "case-b", "case-c"]
    assert all(set(line) == _LINE_KEYS for line in written[1:])


async def test_an_answered_case_carries_its_answer_scores_and_page_level_retrieval(tmp_path: Path) -> None:
    lines = results_lines(await _report(), sources=None, other_collection=frozenset(), case_order=("case-a", "case-b", "case-c"))
    path = tmp_path / "results.jsonl"

    write_results(path, _HEADER, lines)

    assert _read(path)[1] == {
        "case": "case-a",
        "query_type": "comparison",
        "strict": True,
        "official": False,
        "failed": False,
        "error": None,
        "answer": "Yes.",
        "cited": ["note.beta#^c2", "note.zeta", "note.alpha"],
        "sources": None,
        "recall@4": 1.0,
        "recall@10": 1.0,
        "mrr": 1.0,
        "other_collection": False,
        "transient": False,
        "seen_recall": None,
        "seen_gold": None,
    }


async def test_a_failed_case_is_written_as_incorrect_with_its_error() -> None:
    lines = results_lines(await _report(), sources=None, other_collection=frozenset(), case_order=("case-a", "case-b", "case-c"))

    failed = next(line for line in lines if line.case == "case-b")

    assert failed.failed is True
    assert failed.error is not None
    assert "the model stopped answering" in failed.error
    assert (failed.strict, failed.official) == (False, False)
    assert failed.answer is None
    assert failed.cited == ()
    assert (failed.recall_at_4, failed.recall_at_10, failed.mrr) == (0.0, 0.0, 0.0)


async def test_a_case_with_no_gold_has_no_retrieval_scores() -> None:
    lines = results_lines(await _report(), sources=None, other_collection=frozenset(), case_order=("case-a", "case-b", "case-c"))

    no_gold = next(line for line in lines if line.case == "case-c")

    assert (no_gold.recall_at_4, no_gold.recall_at_10, no_gold.mrr) == (None, None, None)
    assert (no_gold.strict, no_gold.official) == (False, True)


async def test_mapped_sources_are_scored_against_the_gold_ids() -> None:
    sources = {"case-a": ["src-zeta", "src-beta", "src-alpha", "src-eta", "note.alpha"]}

    lines = results_lines(await _report(), sources=sources, other_collection=frozenset(), case_order=("case-a", "case-b", "case-c"))

    answered = next(line for line in lines if line.case == "case-a")
    failed = next(line for line in lines if line.case == "case-b")
    assert answered.sources == ("src-zeta", "src-beta", "src-alpha", "src-eta", "note.alpha")
    assert answered.recall_at_4 == pytest.approx(0.0)
    assert answered.recall_at_10 == pytest.approx(0.5)
    assert answered.mrr == pytest.approx(0.2)
    assert failed.sources == ()


async def test_seen_sources_score_each_case_against_its_gold_ids_a_failed_case_keeping_what_it_saw() -> None:
    seen = {"case-a": frozenset({"note.beta", "note.zeta"}), "case-b": frozenset({"note.gamma"}), "case-c": frozenset({"note.alpha"})}

    lines = results_lines(await _report(), sources={}, seen=seen, other_collection=frozenset(), case_order=_ORDER)

    by_case = {line.case: line for line in lines}
    assert (by_case["case-a"].seen_gold, by_case["case-a"].seen_recall) == (("note.beta",), 0.5)
    assert (by_case["case-a"].recall_at_4, by_case["case-a"].recall_at_10) == (0.0, 0.0)
    assert (by_case["case-b"].failed, by_case["case-b"].seen_gold, by_case["case-b"].seen_recall) == (True, ("note.gamma",), 1.0)
    assert (by_case["case-c"].seen_gold, by_case["case-c"].seen_recall) == (None, None)


async def test_a_mapped_case_whose_tool_results_named_nothing_has_seen_recall_zero() -> None:
    lines = results_lines(await _report(), sources={}, seen={}, other_collection=frozenset(), case_order=_ORDER)

    answered = next(line for line in lines if line.case == "case-a")
    assert (answered.seen_gold, answered.seen_recall) == ((), 0.0)


async def test_a_run_that_mapped_no_sources_has_no_seen_recall() -> None:
    lines = results_lines(await _report(), sources=None, other_collection=frozenset(), case_order=_ORDER)

    assert all((line.seen_gold, line.seen_recall) == (None, None) for line in lines)


async def test_a_failed_source_lookup_leaves_every_source_and_retrieval_score_null() -> None:
    lines = results_lines(
        await _report(),
        sources=None,
        seen={"case-a": frozenset({"note.beta"})},
        other_collection=frozenset(),
        case_order=("case-a", "case-b", "case-c"),
        source_lookup_failed=True,
    )

    assert all(line.sources is None for line in lines)
    assert all((line.recall_at_4, line.recall_at_10, line.mrr) == (None, None, None) for line in lines)
    assert all((line.seen_gold, line.seen_recall) == (None, None) for line in lines)
    assert next(line for line in lines if line.case == "case-a").strict is True


async def test_a_case_named_as_reading_another_collection_is_flagged() -> None:
    lines = results_lines(await _report(), sources=None, other_collection=frozenset({"case-c"}), case_order=("case-a", "case-b", "case-c"))

    assert {line.case: line.other_collection for line in lines} == {"case-a": False, "case-b": False, "case-c": True}


async def test_lines_follow_the_dataset_order_with_failures_in_place() -> None:
    lines = results_lines(await _report(), sources=None, other_collection=frozenset(), case_order=("case-c", "case-b", "case-a"))

    assert [line.case for line in lines] == ["case-c", "case-b", "case-a"]


_ORDER = ("case-a", "case-b", "case-c")


async def test_a_failure_named_transient_is_written_as_transient() -> None:
    lines = results_lines(await _report(), sources=None, other_collection=frozenset(), case_order=_ORDER, transient=frozenset({"case-b"}))

    assert {line.case: line.transient for line in lines} == {"case-a": False, "case-b": True, "case-c": False}


async def test_a_failure_not_named_transient_is_not_transient() -> None:
    lines = results_lines(await _report(), sources=None, other_collection=frozenset(), case_order=_ORDER)

    failed = next(line for line in lines if line.case == "case-b")
    assert (failed.failed, failed.transient) == (True, False)


async def test_the_file_reads_back_as_written(tmp_path: Path) -> None:
    lines = results_lines(
        await _report(),
        sources={"case-a": ["note.alpha"]},
        seen={"case-a": frozenset({"note.alpha", "note.beta"})},
        other_collection=frozenset({"case-c"}),
        case_order=_ORDER,
        transient=frozenset({"case-b"}),
    )
    path = tmp_path / "results.jsonl"
    write_results(path, _HEADER, lines)

    header, read = read_results(path)

    assert header == _HEADER
    assert read == tuple(lines)


def _older_line(case: str, *, error: str | None = None) -> dict[str, object]:
    """A case line as a results file written before transient failures and seen recall were recorded holds it."""
    return {
        "case": case,
        "query_type": None,
        "strict": error is None,
        "official": error is None,
        "failed": error is not None,
        "error": error,
        "answer": None if error is not None else "yes",
        "cited": [] if error is not None else ["note.alpha"],
        "sources": None,
        "recall@4": None,
        "recall@10": None,
        "mrr": None,
        "other_collection": False,
    }


def _older_header(*, pinned_search: bool = False) -> dict[str, object]:
    """A header as a results file written before code mode, the thinking level and the date range were recorded holds it."""
    return {
        "strategy": "full",
        "sql": False,
        "model": "test-model",
        "instructions_sha256": "a" * 64,
        "max_tool_calls": None,
        "pinned_search": pinned_search,
        "dataset": "sample",
        "dataset_sha256": "b" * 64,
        "collection": "notes",
        "knowledge_base_fingerprint": "hash:notes",
        "started": "2026-10-01T09:00:00+00:00",
        "source_lookup_error": None,
    }


def _write_older_file(path: Path) -> None:
    """A results file written before code mode, the thinking level and transient failures were recorded."""
    rows = [
        {"header": _older_header()},
        _older_line("case-a"),
        _older_line("case-b", error="ModelAPIError: Connection error."),
        _older_line("case-c", error="UsageLimitExceeded: The next tool call(s) would exceed the tool_calls_limit of 2 (tool_calls=3)."),
    ]
    path.write_text("".join(f"{json.dumps(row)}\n" for row in rows), encoding="utf-8")


def test_an_older_file_is_refused_as_predating_the_current_format_naming_what_its_header_lacks(tmp_path: Path) -> None:
    path = tmp_path / "older.jsonl"
    _write_older_file(path)

    with pytest.raises(ValueError, match=r"line 1 of .* predates the current results format: its header has no code_mode, index_build, search_date_range, source_scoring_version, thinking\."):
        read_results(path)


async def test_a_case_line_missing_a_field_the_writer_writes_is_refused_as_predating_the_current_format(tmp_path: Path) -> None:
    path = tmp_path / "results.jsonl"
    write_results(path, _HEADER, [])
    with path.open("a", encoding="utf-8") as handle:
        handle.write(f"{json.dumps(_older_line('case-a'))}\n")

    with pytest.raises(ValueError, match=r"line 2 of .* predates the current results format: it has no seen_gold, seen_recall, transient\."):
        read_results(path)


def test_a_header_key_this_reader_does_not_know_is_ignored(tmp_path: Path) -> None:
    path = tmp_path / "results.jsonl"
    write_results(path, _HEADER, [])
    row = json.loads(path.read_text(encoding="utf-8"))
    row["header"]["a_key_a_newer_writer_adds"] = 1
    path.write_text(f"{json.dumps(row)}\n", encoding="utf-8")

    header, lines = read_results(path)

    assert (header, lines) == (_HEADER, ())


def test_a_header_with_no_source_scoring_version_is_refused_as_predating_the_current_format(tmp_path: Path) -> None:
    path = tmp_path / "results.jsonl"
    write_results(path, _HEADER, [])
    row = json.loads(path.read_text(encoding="utf-8"))
    del row["header"]["source_scoring_version"]
    path.write_text(f"{json.dumps(row)}\n", encoding="utf-8")

    with pytest.raises(ValueError, match=r"line 1 of .* predates the current results format: its header has no source_scoring_version\."):
        read_results(path)


def test_an_empty_file_is_refused(tmp_path: Path) -> None:
    path = tmp_path / "results.jsonl"
    path.write_text("\n", encoding="utf-8")

    with pytest.raises(ValueError, match="is empty"):
        read_results(path)


def test_a_line_that_is_not_a_case_line_is_refused_naming_its_number(tmp_path: Path) -> None:
    path = tmp_path / "results.jsonl"
    write_results(path, _HEADER, [])
    with path.open("a", encoding="utf-8") as file:
        file.write('{"case": "case-a"}\n')

    with pytest.raises(ValueError, match="line 2 of"):
        read_results(path)


def test_a_first_line_that_is_not_a_header_is_refused(tmp_path: Path) -> None:
    path = tmp_path / "results.jsonl"
    path.write_text(json.dumps(_older_line("case-a")) + "\n", encoding="utf-8")

    with pytest.raises(ValueError, match="line 1 of"):
        read_results(path)


async def _answer_every_case(inp: QaInput) -> RetrievalAnswer:
    return RetrievalAnswer(answer="north", cited_source_ids=["note.gamma"])


async def _rerun_of_case_b() -> EvaluationRunReport:
    return await build_dataset([_case("case-b", answer="north", gold=("note.gamma",))]).evaluate(_answer_every_case, progress=False)


async def test_kept_lines_take_their_place_in_the_dataset_order_beside_the_lines_run_now() -> None:
    earlier = results_lines(await _report(), sources=None, other_collection=frozenset(), case_order=_ORDER)
    kept = [line for line in earlier if line.case != "case-b"]

    lines = results_lines(await _rerun_of_case_b(), sources=None, other_collection=frozenset(), case_order=_ORDER, kept=kept)

    assert [line.case for line in lines] == ["case-a", "case-b", "case-c"]
    assert (lines[0], lines[2]) == (kept[0], kept[1])
    assert (lines[1].failed, lines[1].strict) == (False, True)


async def test_a_failed_source_lookup_leaves_only_the_lines_run_now_unscored() -> None:
    sources = {"case-a": ["src-alpha"], "case-c": ["src-alpha"]}
    earlier = results_lines(await _report(), sources=sources, other_collection=frozenset(), case_order=_ORDER)
    kept = [line for line in earlier if line.case != "case-b"]

    lines = results_lines(
        await _rerun_of_case_b(), sources=None, other_collection=frozenset(), case_order=_ORDER, kept=kept, source_lookup_failed=True
    )

    assert (lines[0], lines[2]) == (kept[0], kept[1])
    assert lines[0].sources == ("src-alpha",)
    assert (lines[1].sources, lines[1].recall_at_4, lines[1].recall_at_10, lines[1].mrr) == (None, None, None, None)


async def test_a_rewrite_replaces_the_file_whole_so_a_reader_of_the_old_file_keeps_it_whole(tmp_path: Path) -> None:
    lines = results_lines(await _report(), sources=None, other_collection=frozenset(), case_order=_ORDER)
    path = tmp_path / "results.jsonl"
    write_results(path, _HEADER, lines)
    old = path.read_text(encoding="utf-8")
    held = tmp_path / "held.jsonl"
    os.link(path, held)

    write_results(path, _HEADER, lines[:1])

    assert held.read_text(encoding="utf-8") == old
    assert len(path.read_text(encoding="utf-8").splitlines()) == 2


@pytest.mark.parametrize("separator", ["\u2028", "\u2029", "\u0085"])
async def test_a_case_line_whose_answer_holds_a_line_separator_reads_back(tmp_path: Path, separator: str) -> None:
    answered, *rest = results_lines(await _report(), sources=None, other_collection=frozenset(), case_order=_ORDER)
    lines = [replace(answered, answer=f"first{separator}second"), *rest]
    path = tmp_path / "results.jsonl"
    write_results(path, _HEADER, lines)

    _header, read = read_results(path)

    assert read == tuple(lines)


def test_a_repeated_case_is_refused_naming_it(tmp_path: Path) -> None:
    path = tmp_path / "results.jsonl"
    write_results(path, _HEADER, [])
    current = {**_older_line("case-a"), "transient": False, "seen_recall": None, "seen_gold": None}
    with path.open("a", encoding="utf-8") as handle:
        handle.write(f"{json.dumps(current)}\n{json.dumps(current)}\n")

    with pytest.raises(ValueError, match=r"line 3 of .* repeats the case case-a"):
        read_results(path)


def test_the_dataset_fingerprint_survives_a_save_and_load(tmp_path: Path) -> None:
    path = tmp_path / "sample.yaml"
    save_dataset(_dataset(), path)

    assert dataset_sha256(load_dataset(path)) == dataset_sha256(_dataset())


def test_the_dataset_fingerprint_changes_with_an_expected_answer() -> None:
    changed = build_dataset([_case("case-a", answer="no", gold=("note.alpha", "note.beta"), query_type="comparison")])
    original = build_dataset([_case("case-a", answer="yes", gold=("note.alpha", "note.beta"), query_type="comparison")])

    assert dataset_sha256(changed) != dataset_sha256(original)
