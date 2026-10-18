"""`import_judgments`: a neutral relevance judgments file becomes an evals dataset whose gold ids are source ids."""

from __future__ import annotations

import hashlib
import json

from typing import TYPE_CHECKING

import pytest

from engram.evals.generation.contracts import load_dataset
from engram.evals.judgments.contracts import JudgmentsFileError, import_judgments

if TYPE_CHECKING:
    from pathlib import Path


def _jsonl(tmp_path: Path, lines: list[dict[str, object]]) -> Path:
    path = tmp_path / "judgments.test.jsonl"
    path.write_text("".join(json.dumps(line) + "\n" for line in lines), encoding="utf-8")
    return path


def test_a_query_becomes_a_case_whose_gold_ids_are_its_relevant_documents(tmp_path: Path) -> None:
    source = _jsonl(tmp_path, [{"query_id": "q1", "question": "Q?", "judgments": {"d1": 1, "d2": 0, "d3": 2}}])

    report = import_judgments(source, tmp_path / "scifact.yaml")

    case = load_dataset(tmp_path / "scifact.yaml").cases[0]
    assert case.name == "q1"
    assert case.inputs.question == "Q?"
    assert case.expected_output is not None
    assert case.expected_output.cited_source_ids == ["d3", "d1"], "highest grade first, grade 0 left out"
    assert case.metadata is not None
    assert case.metadata.judgments == {"d1": 1, "d2": 0, "d3": 2}
    assert case.metadata.difficulty is None
    assert (report.cases, report.skipped) == (1, 0)


def test_a_query_with_no_relevant_document_is_skipped_and_counted(tmp_path: Path) -> None:
    source = _jsonl(
        tmp_path,
        [{"query_id": "q1", "question": "Q?", "judgments": {"d1": 0}}, {"query_id": "q2", "question": "R?", "judgments": {"d2": 1}}],
    )

    report = import_judgments(source, tmp_path / "out.yaml")

    assert (report.cases, report.skipped) == (1, 1)
    assert [case.name for case in load_dataset(tmp_path / "out.yaml").cases] == ["q2"]


def test_a_line_that_is_not_a_judged_query_is_refused_by_number(tmp_path: Path) -> None:
    source = tmp_path / "bad.jsonl"
    source.write_text('{"query_id": "q1", "question": "Q?", "judgments": {"d1": 1}}\n{"oops": true}\n', encoding="utf-8")

    with pytest.raises(JudgmentsFileError, match="line 2"):
        import_judgments(source, tmp_path / "out.yaml")

    assert not (tmp_path / "out.yaml").exists()


def test_a_line_with_an_answer_and_a_query_type_carries_both_onto_its_case(tmp_path: Path) -> None:
    line = {"query_id": "q1", "question": "Q?", "judgments": {"d1": 1}, "answer": "Yes", "query_type": "kind-a"}

    import_judgments(_jsonl(tmp_path, [line]), tmp_path / "out.yaml")

    case = load_dataset(tmp_path / "out.yaml").cases[0]
    assert case.expected_output is not None
    assert case.expected_output.answer == "Yes"
    assert case.expected_output.cited_source_ids == ["d1"]
    assert case.metadata is not None
    assert case.metadata.query_type == "kind-a"


def test_a_line_without_an_answer_or_a_query_type_loads_as_before(tmp_path: Path) -> None:
    import_judgments(_jsonl(tmp_path, [{"query_id": "q1", "question": "Q?", "judgments": {"d1": 1}}]), tmp_path / "out.yaml")

    case = load_dataset(tmp_path / "out.yaml").cases[0]
    assert case.expected_output is not None
    assert case.expected_output.answer == ""
    assert case.metadata is not None
    assert case.metadata.query_type is None


def test_a_query_with_an_answer_and_no_relevant_document_becomes_a_case_with_no_gold_ids(tmp_path: Path) -> None:
    lines: list[dict[str, object]] = [
        {"query_id": "q1", "question": "Q?", "judgments": {}, "answer": "no answer", "query_type": "kind-b"},
        {"query_id": "q2", "question": "R?", "judgments": {"d1": 0}, "answer": "no answer"},
        {"query_id": "q3", "question": "S?", "judgments": {}},
        {"query_id": "q4", "question": "T?", "judgments": {"d2": 0}, "answer": ""},
    ]

    report = import_judgments(_jsonl(tmp_path, lines), tmp_path / "out.yaml")

    cases = load_dataset(tmp_path / "out.yaml").cases
    assert [case.name for case in cases] == ["q1", "q2"], "a query with no relevant document and no answer is still left out"
    assert [case.expected_output.cited_source_ids for case in cases if case.expected_output is not None] == [[], []]
    assert [case.expected_output.answer for case in cases if case.expected_output is not None] == ["no answer"] * 2
    assert (report.cases, report.skipped) == (2, 2)


def _typed_lines(counts: dict[str, int]) -> list[dict[str, object]]:
    lines: list[dict[str, object]] = []
    for query_type, count in counts.items():
        lines += [
            {"query_id": f"{query_type}-{number}", "question": "Q?", "judgments": {"d1": 1}, "answer": "Yes", "query_type": query_type}
            for number in range(count)
        ]
    return lines


def _first_by_salted_hash(names: list[str], salt: str, count: int) -> set[str]:
    return set(sorted(names, key=lambda name: hashlib.sha256((salt + name).encode()).hexdigest())[:count])


def _names(path: Path) -> list[str]:
    return [str(case.name) for case in load_dataset(path).cases]


def test_a_per_type_sample_keeps_the_cases_whose_salted_hash_comes_first_in_each_query_type(tmp_path: Path) -> None:
    source = _jsonl(tmp_path, _typed_lines({"alpha": 4, "beta": 4}))

    report = import_judgments(source, tmp_path / "out.yaml", per_type=2, salt="s")

    alphas, betas = [f"alpha-{n}" for n in range(4)], [f"beta-{n}" for n in range(4)]
    kept = _first_by_salted_hash(alphas, "s", 2) | _first_by_salted_hash(betas, "s", 2)
    assert _names(tmp_path / "out.yaml") == [name for name in [*alphas, *betas] if name in kept], "the kept cases stay in file order"
    assert (report.cases, report.skipped) == (4, 0)
    assert report.cases_per_type == {"alpha": 2, "beta": 2}


def test_the_same_salt_keeps_the_same_cases_every_time(tmp_path: Path) -> None:
    source = _jsonl(tmp_path, _typed_lines({"alpha": 4, "beta": 4}))

    import_judgments(source, tmp_path / "first.yaml", per_type=2, salt="s")
    import_judgments(source, tmp_path / "second.yaml", per_type=2, salt="s")

    assert _names(tmp_path / "first.yaml") == _names(tmp_path / "second.yaml")


def test_a_query_type_with_fewer_cases_than_the_sample_size_keeps_all_of_them(tmp_path: Path) -> None:
    source = _jsonl(tmp_path, _typed_lines({"alpha": 4, "beta": 1}))

    report = import_judgments(source, tmp_path / "out.yaml", per_type=2, salt="s")

    assert report.cases_per_type == {"alpha": 2, "beta": 1}
    assert "beta-0" in _names(tmp_path / "out.yaml")


def test_cases_with_no_query_type_are_sampled_as_one_group_and_left_out_of_the_counts(tmp_path: Path) -> None:
    lines = [{"query_id": f"q{number}", "question": "Q?", "judgments": {"d1": 1}} for number in range(5)]

    report = import_judgments(_jsonl(tmp_path, [*lines, *_typed_lines({"alpha": 3})]), tmp_path / "out.yaml", per_type=2, salt="s")

    untyped = {name for name in _names(tmp_path / "out.yaml") if name.startswith("q")}
    assert untyped == _first_by_salted_hash([f"q{n}" for n in range(5)], "s", 2)
    assert (report.cases, report.cases_per_type) == (4, {"alpha": 2})


def test_without_a_sample_size_every_case_is_kept_and_counted_by_query_type(tmp_path: Path) -> None:
    report = import_judgments(_jsonl(tmp_path, _typed_lines({"alpha": 3, "beta": 1})), tmp_path / "out.yaml")

    assert (report.cases, report.cases_per_type) == (4, {"alpha": 3, "beta": 1})


@pytest.mark.parametrize("per_type", [0, -1])
def test_a_sample_size_below_one_is_refused_before_anything_is_written(tmp_path: Path, per_type: int) -> None:
    source = _jsonl(tmp_path, _typed_lines({"alpha": 3}))

    with pytest.raises(ValueError, match="at least 1"):
        import_judgments(source, tmp_path / "out.yaml", per_type=per_type, salt="s")

    assert not (tmp_path / "out.yaml").exists()
