"""Scoring search alone against relevance judgments, per document, as nDCG@10, recall@k and MRR."""

from __future__ import annotations

import math

from pathlib import Path
from typing import TYPE_CHECKING

import pytest
import yaml

from asyncpg.exceptions import LockNotAvailableError, PostgresSyntaxError, QueryCanceledError
from core.ai.retrieval.contracts import SqlResult
from pydantic import ValidationError

from engram.evals.commands import ScoreDataset
from engram.evals.generation.contracts import AuthoredCase, build_dataset, save_dataset
from engram.evals.handlers import ScoreDatasetHandler
from engram.evals.scoring.contracts import (
    CASE_RETRIES,
    FIRST_RETRY_WAIT_SECONDS,
    SEARCH_SETUPS,
    HitRef,
    ranked_sources,
    score_case,
    score_dataset,
    write_score_report,
)
from engram.evals.scoring.domain import source_gold
from engram.evals.shared.contracts import ExpectedQa, QaCaseMeta, QaInput

if TYPE_CHECKING:
    from collections.abc import Collection, Mapping, Sequence

    from engram.retrieval.domain import Search


def _raw(unit_id: str) -> HitRef:
    return HitRef(id=unit_id, hit_level="raw", page_id=None)


def _card(page_id: str) -> HitRef:
    return HitRef(id=page_id, hit_level="card", page_id=page_id)


def _section(unit_id: str) -> HitRef:
    return HitRef(id=unit_id, hit_level="section", page_id=unit_id.split("#", 1)[0])


def test_two_chunks_of_one_document_count_once_at_the_first_rank_and_the_repeat_holds_its_own_rank():
    assert ranked_sources([_raw("d1:3"), _raw("d1:7"), _raw("d2:0")], {}) == (["d1", None, "d2"], 0)


def test_a_page_hit_maps_through_its_page_s_source():
    hits = [_card("dg.paper-x"), _section("dg.paper-x#methods"), _raw("d9:1")]

    assert ranked_sources(hits, {"dg.paper-x": "d4"}) == (["d4", None, "d9"], 0)


def test_a_hit_with_no_source_holds_its_rank_and_is_counted():
    assert ranked_sources([_card("dg.orphan"), _raw("d1:0")], {}) == ([None, "d1"], 1)


def test_entity_hits_above_the_gold_source_take_the_first_ranks():
    hits = [_card("dg.entity-a"), _card("dg.entity-b"), _raw("d1:0")]
    ranked, unmapped = ranked_sources(hits, {})

    scores = score_case(ranked=ranked, gold=("d1",), judgments={})

    assert unmapped == 2
    assert scores.recall == {1: 0.0, 5: 1.0, 10: 1.0}
    assert scores.reciprocal_rank == pytest.approx(1 / 3)
    assert scores.ndcg_at_10 == pytest.approx(1 / math.log2(4))
    assert scores.ranked == (None, None, "d1")


def test_a_case_with_judgments_scores_ndcg_from_the_grades():
    scores = score_case(ranked=["b", "a"], gold=("a", "b"), judgments={"a": 2, "b": 1})

    ideal = 2 + 1 / math.log2(3)
    assert scores.ndcg_at_10 == pytest.approx((1 + 2 / math.log2(3)) / ideal)
    assert scores.recall == {1: 0.5, 5: 1.0, 10: 1.0}
    assert scores.reciprocal_rank == 1.0


def test_a_case_with_no_judgments_scores_every_gold_id_as_grade_one():
    scores = score_case(ranked=["x", "b"], gold=("b",), judgments={})

    assert scores.ndcg_at_10 == pytest.approx(1 / math.log2(3))
    assert scores.reciprocal_rank == 0.5


class _FakeSearch:
    """Answers every search with the rows scripted for its question, and records the questions."""

    def __init__(self, rows_by_question: Mapping[str, Sequence[Mapping[str, object]]]) -> None:
        self.rows_by_question = rows_by_question
        self.queries: list[Search] = []

    async def __call__(self, query: Search) -> SqlResult:
        self.queries.append(query)
        return SqlResult(rows=tuple(dict(row) for row in self.rows_by_question[query.query]))


class _FakeSources:
    """A knowledge base whose pages are the keys of `sources` and `pages_without_source`."""

    def __init__(self, sources: Mapping[str, str], pages_without_source: Collection[str] = ()) -> None:
        self.sources = sources
        self.pages_without_source = pages_without_source
        self.asked: list[tuple[str, tuple[str, ...]]] = []
        self.pages_asked: list[tuple[str, tuple[str, ...]]] = []

    async def source_ids(self, collection: str, page_ids: Sequence[str]) -> Mapping[str, str]:
        self.asked.append((collection, tuple(page_ids)))
        return {page_id: self.sources[page_id] for page_id in page_ids if page_id in self.sources}

    async def page_ids(self, collection: str, ids: Sequence[str]) -> frozenset[str]:
        self.pages_asked.append((collection, tuple(ids)))
        return frozenset(found for found in ids if found in self.sources or found in self.pages_without_source)


def _dataset(tmp_path: Path) -> Path:
    cases = [
        AuthoredCase(
            question=QaInput(question="Q1?"),
            expected=ExpectedQa(answer="", expected_source_ids=("d1",)),
            meta=QaCaseMeta(judgments={"d1": 1}),
            case_name="q1",
        ),
        AuthoredCase(
            question=QaInput(question="Q2?"),
            expected=ExpectedQa(answer="", expected_source_ids=("d2",)),
            meta=QaCaseMeta(judgments={"d2": 1}),
            case_name="q2",
        ),
    ]
    path = tmp_path / "set.yaml"
    save_dataset(build_dataset(cases), path)
    return path


async def test_a_dataset_is_scored_case_by_case_and_as_means(tmp_path: Path):
    search = _FakeSearch(
        {
            "Q1?": [{"id": "d1:0", "hit_level": "raw", "page_id": None}],
            "Q2?": [{"id": "dg.x", "hit_level": "card", "page_id": "dg.x"}, {"id": "d2:4", "hit_level": "raw", "page_id": None}],
        }
    )
    sources = _FakeSources({})

    score = await score_dataset(_dataset(tmp_path), collection="scifact", search=search, sources=sources)

    assert [case.name for case in score.cases] == ["q1", "q2"]
    assert score.means["recall@1"] == 0.5, "q2's first hit names no source, so its gold sits at rank two"
    assert score.means["mrr"] == 0.75
    assert score.unmapped_hits == 1
    assert {query.k for query in search.queries} == {50}
    assert all(query.collection == "scifact" for query in search.queries)
    assert sources.asked == [("scifact", ("dg.x",))], "a case with no page hit asks for no page source"


async def test_the_handler_writes_the_report_and_answers_the_means(tmp_path: Path):
    search = _FakeSearch({"Q1?": [{"id": "d1:0", "hit_level": "raw", "page_id": None}], "Q2?": []})
    handler = ScoreDatasetHandler(search, _FakeSources({}))

    report = await handler.handle(ScoreDataset(dataset=str(_dataset(tmp_path)), collection="scifact", out=str(tmp_path / "scores")))

    assert report.cases == 2
    assert report.means["recall@1"] == 0.5
    assert (tmp_path / "scores" / "set.score.yaml").is_file()
    summary = (tmp_path / "scores" / "set.score.md").read_text(encoding="utf-8")
    assert "Recall past 10 is not reported" in summary
    assert "q2" in summary, "a case that ranked no gold document is named"


def _case(name: str, gold: tuple[str, ...], judgments: dict[str, int] | None = None) -> AuthoredCase:
    return AuthoredCase(
        question=QaInput(question=f"{name}?"),
        expected=ExpectedQa(answer="", expected_source_ids=gold),
        meta=QaCaseMeta(judgments=judgments or {}),
        case_name=name,
    )


def _saved(tmp_path: Path, *cases: AuthoredCase) -> Path:
    path = tmp_path / "generated.yaml"
    save_dataset(build_dataset(cases), path)
    return path


def _chunk_of(*source_ids: str) -> list[dict[str, object]]:
    return [{"id": f"{source_id}:0", "hit_level": "raw", "page_id": None} for source_id in source_ids]


async def test_a_gold_page_scores_against_the_source_its_page_names(tmp_path: Path):
    dataset = _saved(tmp_path, _case("q1", ("dg.four",)))
    search = _FakeSearch({"q1?": _chunk_of("d4")})

    score = await score_dataset(dataset, collection="kb", search=search, sources=_FakeSources({"dg.four": "d4"}))

    assert score.cases[0].recall == {1: 1.0, 5: 1.0, 10: 1.0}
    assert score.means["mrr"] == 1.0
    assert score.means["ndcg@10"] == 1.0


async def test_two_gold_pages_of_one_source_count_as_one_gold_id(tmp_path: Path):
    dataset = _saved(tmp_path, _case("q1", ("dg.a", "dg.b")))
    search = _FakeSearch({"q1?": _chunk_of("d4")})

    score = await score_dataset(dataset, collection="kb", search=search, sources=_FakeSources({"dg.a": "d4", "dg.b": "d4"}))

    assert score.means["recall@1"] == 1.0
    assert score.unmappable_gold == 0
    gold = source_gold(("dg.a", "dg.b"), {}, pages={"dg.a", "dg.b"}, page_sources={"dg.a": "d4", "dg.b": "d4"})
    assert gold.ids == ("d4",)


async def test_a_gold_id_that_names_no_page_is_kept_as_a_source_id(tmp_path: Path):
    dataset = _saved(tmp_path, _case("q1", ("d1",), {"d1": 1, "d2": 0}))
    search = _FakeSearch({"q1?": _chunk_of("d2", "d1")})

    score = await score_dataset(dataset, collection="kb", search=search, sources=_FakeSources({"dg.x": "d9"}))

    assert score.means["recall@1"] == 0.0
    assert score.means["recall@5"] == 1.0
    assert score.means["mrr"] == 0.5
    assert score.means["ndcg@10"] == pytest.approx(1 / math.log2(3))


async def test_a_gold_page_with_no_source_is_counted_and_a_case_left_with_no_gold_is_left_out(tmp_path: Path):
    dataset = _saved(tmp_path, _case("q1", ("dg.linked", "dg.unlinked")), _case("q2", ("dg.lonely",)))
    search = _FakeSearch({"q1?": _chunk_of("d4"), "q2?": []})
    sources = _FakeSources({"dg.linked": "d4"}, pages_without_source=("dg.unlinked", "dg.lonely"))

    score = await score_dataset(dataset, collection="kb", search=search, sources=sources)

    assert score.unmappable_gold == 2
    assert score.cases_without_gold == 1
    assert [case.name for case in score.cases] == ["q1"]
    assert score.means["recall@1"] == 1.0, "the left out case does not pull the means down"
    assert [query.query for query in search.queries] == ["q1?"], "a case with no gold left runs no search"
    assert sources.pages_asked == [("kb", ("dg.linked", "dg.unlinked", "dg.lonely"))], "one page lookup for the whole dataset"
    _, markdown = write_score_report(score, dataset=dataset, collection="kb", out_dir=tmp_path / "scores")
    summary = markdown.read_text(encoding="utf-8")
    assert "Gold ids that name a page with no source document: 2." in summary
    assert "Cases left out because no gold id names a source document: 1." in summary


async def test_the_handler_answers_the_gold_that_named_no_source(tmp_path: Path):
    dataset = _saved(tmp_path, _case("q1", ("dg.linked", "dg.unlinked")), _case("q2", ("dg.lonely",)))
    search = _FakeSearch({"q1?": _chunk_of("d4"), "q2?": []})
    sources = _FakeSources({"dg.linked": "d4"}, pages_without_source=("dg.unlinked", "dg.lonely"))

    report = await ScoreDatasetHandler(search, sources).handle(ScoreDataset(dataset=str(dataset), collection="kb", out=str(tmp_path / "scores")))

    assert report.cases == 1
    assert report.unmappable_gold == 2
    assert report.cases_without_gold == 1


async def test_a_gold_page_carries_its_grade_and_two_pages_of_one_source_keep_the_higher(tmp_path: Path):
    dataset = _saved(tmp_path, _case("q1", ("dg.a", "dg.b", "d9"), {"dg.a": 2, "dg.b": 1, "d9": 1, "dg.c": 0}))
    search = _FakeSearch({"q1?": _chunk_of("d9", "d4")})
    sources = _FakeSources({"dg.a": "d4", "dg.b": "d4", "dg.c": "d5"})

    score = await score_dataset(dataset, collection="kb", search=search, sources=sources)

    ideal = 2 + 1 / math.log2(3)
    assert score.means["ndcg@10"] == pytest.approx((1 + 2 / math.log2(3)) / ideal)
    assert score.means["recall@5"] == 1.0


_SETUP_NAMES = "hybrid, keyword, hybrid-raw, keyword-raw, hybrid-card, hybrid-section, hybrid-claim"


def test_the_seven_search_setups_are_named_in_order():
    assert ", ".join(SEARCH_SETUPS) == _SETUP_NAMES


@pytest.mark.parametrize(
    ("setup", "hybrid", "level"),
    [
        ("hybrid", True, None),
        ("keyword", False, None),
        ("hybrid-raw", True, "raw"),
        ("keyword-raw", False, "raw"),
        ("hybrid-card", True, "card"),
        ("hybrid-section", True, "section"),
        ("hybrid-claim", True, "claim"),
    ],
)
async def test_each_search_setup_runs_every_search_with_its_two_knobs(tmp_path: Path, setup: str, hybrid: bool, level: str | None):
    search = _FakeSearch({"Q1?": [], "Q2?": []})

    score = await score_dataset(_dataset(tmp_path), collection="scifact", search=search, sources=_FakeSources({}), setup_name=setup)

    assert [(query.hybrid, query.level) for query in search.queries] == [(hybrid, level), (hybrid, level)]
    assert {(query.k, query.collection) for query in search.queries} == {(50, "scifact")}
    assert score.setup == setup


async def test_a_dataset_scored_with_no_setup_runs_hybrid_over_every_level(tmp_path: Path):
    search = _FakeSearch({"Q1?": [], "Q2?": []})

    score = await score_dataset(_dataset(tmp_path), collection="scifact", search=search, sources=_FakeSources({}))

    assert {(query.hybrid, query.level) for query in search.queries} == {(True, None)}
    assert score.setup == "hybrid"


async def test_an_unknown_setup_is_refused_naming_every_setup_before_any_search(tmp_path: Path):
    search = _FakeSearch({})

    with pytest.raises(ValueError, match="unknown search setup 'dense'") as refusal:
        await score_dataset(_dataset(tmp_path), collection="scifact", search=search, sources=_FakeSources({}), setup_name="dense")

    assert f"choose one of {_SETUP_NAMES}" in str(refusal.value)
    assert search.queries == []


def test_a_score_command_naming_no_setup_is_refused_naming_every_setup():
    with pytest.raises(ValidationError, match="unknown search setup 'dense'") as refusal:
        ScoreDataset(dataset="set.yaml", collection="scifact", out="scores", setup="dense")

    assert f"choose one of {_SETUP_NAMES}" in str(refusal.value)


def test_a_score_command_with_no_setup_names_hybrid():
    assert ScoreDataset(dataset="set.yaml", collection="scifact", out="scores").setup == "hybrid"


@pytest.mark.parametrize(
    ("setup", "stem"), [("hybrid", "set.score"), ("keyword", "set.keyword.score"), ("hybrid-claim", "set.hybrid-claim.score")]
)
async def test_the_report_is_named_after_its_setup_except_hybrid_and_carries_it(tmp_path: Path, setup: str, stem: str):
    search = _FakeSearch({"Q1?": [{"id": "d1:0", "hit_level": "raw", "page_id": None}], "Q2?": []})
    command = ScoreDataset(dataset=str(_dataset(tmp_path)), collection="scifact", out=str(tmp_path / "scores"), setup=setup)

    report = await ScoreDatasetHandler(search, _FakeSources({})).handle(command)

    assert report.setup == setup
    assert report.out == str(tmp_path / "scores" / f"{stem}.yaml")
    assert report.markdown == str(tmp_path / "scores" / f"{stem}.md")
    assert sorted(path.name for path in (tmp_path / "scores").iterdir()) == [f"{stem}.md", f"{stem}.yaml"]
    assert yaml.safe_load(Path(report.out).read_text(encoding="utf-8"))["setup"] == setup
    assert f"under the {setup} search setup" in Path(report.markdown).read_text(encoding="utf-8")


class _FakeLeveledSearch:
    """Answers every search with the scripted rows at the search's level, or every row when it names no level."""

    def __init__(self, rows: Sequence[Mapping[str, object]]) -> None:
        self.rows = rows
        self.queries: list[Search] = []

    async def __call__(self, query: Search) -> SqlResult:
        self.queries.append(query)
        return SqlResult(rows=tuple(dict(row) for row in self.rows if query.level in {None, row["hit_level"]}))


async def test_a_setup_whose_level_has_no_hits_scores_zero_and_names_every_case_while_hybrid_does_not(tmp_path: Path):
    dataset = _dataset(tmp_path)
    search = _FakeLeveledSearch(_chunk_of("d1", "d2"))

    hybrid = await score_dataset(dataset, collection="scifact", search=search, sources=_FakeSources({}), setup_name="hybrid")
    claim = await score_dataset(dataset, collection="scifact", search=search, sources=_FakeSources({}), setup_name="hybrid-claim")

    assert hybrid.means["mrr"] == 0.75
    assert set(claim.means.values()) == {0.0}
    _, markdown = write_score_report(claim, dataset=dataset, collection="scifact", out_dir=tmp_path / "scores")
    assert "Cases that ranked no gold document (2): q1, q2" in markdown.read_text(encoding="utf-8")


async def test_the_cases_whose_search_returned_no_hits_are_counted_and_printed_only_when_there_are_any(tmp_path: Path):
    dataset = _dataset(tmp_path)
    search = _FakeLeveledSearch(_chunk_of("d1", "d2"))

    hybrid = await score_dataset(dataset, collection="scifact", search=search, sources=_FakeSources({}), setup_name="hybrid")
    claim = await score_dataset(dataset, collection="scifact", search=search, sources=_FakeSources({}), setup_name="hybrid-claim")

    assert [case.hits for case in hybrid.cases] == [2, 2]
    assert [case.hits for case in claim.cases] == [0, 0]
    assert (hybrid.cases_without_hits, claim.cases_without_hits) == (0, 2)
    claim_yaml, claim_markdown = write_score_report(claim, dataset=dataset, collection="scifact", out_dir=tmp_path / "claim")
    _, hybrid_markdown = write_score_report(hybrid, dataset=dataset, collection="scifact", out_dir=tmp_path / "hybrid")
    assert yaml.safe_load(claim_yaml.read_text(encoding="utf-8"))["cases_without_hits"] == 2
    assert "Cases whose search returned no hits at all: 2 of 2." in claim_markdown.read_text(encoding="utf-8")
    assert "returned no hits" not in hybrid_markdown.read_text(encoding="utf-8")


class _FailingSearch(_FakeSearch):
    """Fails each question's first searches with the scripted error, then answers it with its scripted rows."""

    def __init__(self, rows_by_question: Mapping[str, Sequence[Mapping[str, object]]], *, error: Exception, failures: int) -> None:
        super().__init__(rows_by_question)
        self.error = error
        self.failures = failures

    async def __call__(self, query: Search) -> SqlResult:
        failed = sum(asked.query == query.query for asked in self.queries)
        self.queries.append(query)
        if failed < self.failures:
            raise self.error
        return SqlResult(rows=tuple(dict(row) for row in self.rows_by_question[query.query]))


class _RecordedSleep:
    """Records every wait instead of waiting."""

    def __init__(self) -> None:
        self.waits: list[float] = []

    async def __call__(self, seconds: float) -> None:
        self.waits.append(seconds)


_TRANSIENT_SEARCH_FAILURES = [
    QueryCanceledError("canceling statement due to statement timeout"),
    LockNotAvailableError("canceling statement due to lock timeout"),
    ConnectionResetError(54, "Connection reset by peer"),
]


@pytest.mark.parametrize("error", _TRANSIENT_SEARCH_FAILURES, ids=["statement timeout", "lock timeout", "database connection failure"])
async def test_a_case_whose_search_fails_transiently_runs_again_after_a_growing_wait_and_is_counted(tmp_path: Path, error: Exception):
    rows = {"Q1?": [{"id": "d1:0", "hit_level": "raw", "page_id": None}], "Q2?": [{"id": "d2:0", "hit_level": "raw", "page_id": None}]}
    search = _FailingSearch(rows, error=error, failures=2)
    sleep = _RecordedSleep()

    score = await score_dataset(_dataset(tmp_path), collection="scifact", search=search, sources=_FakeSources({}), sleep=sleep)

    assert score.means["recall@1"] == 1.0
    assert [case.retries for case in score.cases] == [2, 2]
    assert score.retries == 4
    assert sorted(sleep.waits) == [FIRST_RETRY_WAIT_SECONDS] * 2 + [FIRST_RETRY_WAIT_SECONDS * 2] * 2
    yaml_path, markdown = write_score_report(score, dataset=tmp_path / "set.yaml", collection="scifact", out_dir=tmp_path / "scores")
    assert yaml.safe_load(yaml_path.read_text(encoding="utf-8"))["retries"] == 4
    assert "Retries after a transient failure: 4." in markdown.read_text(encoding="utf-8")


async def test_a_case_still_failing_transiently_after_its_last_retry_fails_the_score(tmp_path: Path):
    error = QueryCanceledError("canceling statement due to statement timeout")
    search = _FailingSearch({"q1?": []}, error=error, failures=CASE_RETRIES + 1)
    sleep = _RecordedSleep()

    with pytest.raises(QueryCanceledError):
        await score_dataset(_saved(tmp_path, _case("q1", ("d1",))), collection="kb", search=search, sources=_FakeSources({}), sleep=sleep)

    assert len(search.queries) == CASE_RETRIES + 1
    assert sleep.waits == [FIRST_RETRY_WAIT_SECONDS * 2**retry for retry in range(CASE_RETRIES)]


async def test_a_search_failure_that_is_not_transient_fails_the_score_at_once(tmp_path: Path):
    search = _FailingSearch({"q1?": []}, error=PostgresSyntaxError('syntax error at or near "SELEC"'), failures=1)
    sleep = _RecordedSleep()

    with pytest.raises(PostgresSyntaxError):
        await score_dataset(_saved(tmp_path, _case("q1", ("d1",))), collection="kb", search=search, sources=_FakeSources({}), sleep=sleep)

    assert len(search.queries) == 1
    assert sleep.waits == []


async def test_the_handler_answers_the_retries(tmp_path: Path):
    rows = {"Q1?": [{"id": "d1:0", "hit_level": "raw", "page_id": None}], "Q2?": []}
    search = _FailingSearch(rows, error=ConnectionResetError(54, "Connection reset by peer"), failures=1)
    handler = ScoreDatasetHandler(search, _FakeSources({}), sleep=_RecordedSleep())

    report = await handler.handle(ScoreDataset(dataset=str(_dataset(tmp_path)), collection="scifact", out=str(tmp_path / "scores")))

    assert report.retries == 2
