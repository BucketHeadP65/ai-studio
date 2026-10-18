"""The generator check: rank the search setups by nDCG@10 in each dataset, compare the rankings by Kendall's tau-b, decide."""

from __future__ import annotations

import math

from pathlib import Path
from typing import TYPE_CHECKING

import pytest
import yaml

from core.ai.retrieval.contracts import SqlResult

from engram.evals.commands import CheckGenerator
from engram.evals.generation.contracts import AuthoredCase, build_dataset, save_dataset
from engram.evals.generator_check.contracts import (
    RANK_DECIMALS,
    GeneratorCheck,
    SetupRanking,
    SetupsTiedError,
    check_generator,
    generator_check,
    kendall_tau_b,
    setup_ranks,
    write_generator_check,
)
from engram.evals.handlers import CheckGeneratorHandler
from engram.evals.scoring.contracts import SEARCH_SETUPS
from engram.evals.shared.contracts import ExpectedQa, QaCaseMeta, QaInput

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence

    from engram.retrieval.domain import Search

_SETUPS = tuple(SEARCH_SETUPS)
_DESCENDING = dict(zip(_SETUPS, (0.7, 0.6, 0.5, 0.4, 0.3, 0.2, 0.1), strict=True))


def test_setups_are_ranked_highest_ndcg_first():
    assert setup_ranks({"a": 0.2, "b": 0.9, "c": 0.5}) == {"a": 3, "b": 1, "c": 2}


def test_setups_with_the_same_ndcg_share_a_rank_and_the_next_rank_skips():
    assert setup_ranks({"a": 0.5, "b": 0.5, "c": 0.2, "d": 0.9}) == {"a": 2, "b": 2, "c": 4, "d": 1}


def test_tau_b_of_identical_orders_is_one():
    assert kendall_tau_b([0.1, 0.4, 0.3, 0.9], [0.2, 0.8, 0.5, 0.95]) == 1.0


def test_tau_b_of_identical_orders_with_the_same_ties_is_one():
    assert kendall_tau_b([0.5, 0.5, 0.2, 0.9], [0.3, 0.3, 0.1, 0.4]) == 1.0


def test_tau_b_of_reversed_orders_is_minus_one():
    values = list(range(7))

    assert kendall_tau_b(values, values[::-1]) == -1.0


def test_tau_b_of_one_swapped_pair_among_four():
    assert kendall_tau_b([1, 2, 3, 4], [1, 2, 4, 3]) == pytest.approx(2 / 3)


def test_tau_b_with_ties_in_both_orders_matches_the_known_example():
    # Two concordant pairs, six discordant, two pairs tied in the first order and one in the second.
    assert kendall_tau_b([12, 2, 1, 12, 2], [1, 4, 7, 1, 0]) == pytest.approx(-4 / math.sqrt(72))
    assert kendall_tau_b([12, 2, 1, 12, 2], [1, 4, 7, 1, 0]) == pytest.approx(-0.4714045)


def test_tau_b_is_refused_when_one_order_ties_every_value():
    with pytest.raises(ValueError, match="every value is the same"):
        kendall_tau_b([0.3, 0.3, 0.3], [0.1, 0.2, 0.3])


def test_tau_b_is_refused_for_orders_of_different_lengths():
    with pytest.raises(ValueError, match="same length"):
        kendall_tau_b([0.1, 0.2], [0.1, 0.2, 0.3])


def test_the_check_passes_when_both_datasets_rank_the_setups_in_the_same_order():
    generated = {setup: value / 2 for setup, value in _DESCENDING.items()}

    check = generator_check(_DESCENDING, generated)

    assert check.passed is True
    assert check.tau_b == 1.0
    assert (check.agreeing_pairs, check.pairs) == (21, 21)
    assert check.rank_decimals == RANK_DECIMALS == 4
    assert [row.setup for row in check.setups] == list(_SETUPS)
    assert check.setups[0] == SetupRanking(setup="hybrid", human_ndcg=0.7, human_rank=1, generated_ndcg=0.35, generated_rank=1)
    assert [row.generated_rank for row in check.setups] == [1, 2, 3, 4, 5, 6, 7]


def test_the_check_passes_when_both_datasets_tie_the_same_setups():
    human = {**_DESCENDING, "keyword": 0.7}
    generated = {**_DESCENDING, "hybrid": 0.9, "keyword": 0.9}

    check = generator_check(human, generated)

    assert check.passed is True
    assert check.tau_b == 1.0
    assert check.agreeing_pairs == 21
    assert [row.human_rank for row in check.setups[:3]] == [1, 1, 3]


def test_setups_that_differ_only_past_the_fourth_decimal_share_a_rank():
    human = {**_DESCENDING, "keyword": 0.70004}
    generated = {**_DESCENDING, "keyword": 0.7}

    check = generator_check(human, generated)

    assert [row.human_rank for row in check.setups[:3]] == [1, 1, 3]
    assert check.setups[1].human_ndcg == 0.70004
    assert check.passed is True
    assert check.tau_b == 1.0


def test_setups_that_differ_at_the_fourth_decimal_keep_their_order():
    human = {**_DESCENDING, "keyword": 0.7001}

    check = generator_check(human, _DESCENDING)

    assert [row.human_rank for row in check.setups[:2]] == [2, 1]
    assert check.passed is False


def test_one_swapped_pair_fails_the_check():
    generated = {**_DESCENDING, "hybrid": 0.6, "keyword": 0.7}

    check = generator_check(_DESCENDING, generated)

    assert check.passed is False
    assert check.agreeing_pairs == 20
    assert check.tau_b == pytest.approx(19 / 21)


def test_a_tie_in_one_dataset_only_fails_the_check():
    generated = {**_DESCENDING, "keyword": 0.7}

    check = generator_check(_DESCENDING, generated)

    assert check.passed is False
    assert check.agreeing_pairs == 20
    assert check.tau_b < 1.0


def test_reversed_rankings_fail_with_no_agreeing_pair():
    generated = dict(zip(_SETUPS, reversed(list(_DESCENDING.values())), strict=True))

    check = generator_check(_DESCENDING, generated)

    assert check.passed is False
    assert check.agreeing_pairs == 0
    assert check.tau_b == -1.0


@pytest.mark.parametrize(("constant", "named"), [("human", "human-judged"), ("generated", "generated")])
def test_a_dataset_that_scores_every_setup_zero_is_refused_by_name_with_the_value_and_the_usual_causes(constant: str, named: str):
    flat = dict.fromkeys(_SETUPS, 0.0)
    human, generated = (flat, _DESCENDING) if constant == "human" else (_DESCENDING, flat)

    with pytest.raises(SetupsTiedError) as refusal:
        generator_check(human, generated)

    assert str(refusal.value) == (
        f"the {named} dataset gives every search setup the same nDCG@10, 0.0000, so it ranks no setup above another. "
        "An nDCG@10 of 0 usually means the wrong collection, or gold ids that map to no source document"
    )


def test_a_dataset_that_scores_every_setup_the_same_above_zero_is_refused_with_the_value_alone():
    with pytest.raises(SetupsTiedError) as refusal:
        generator_check(_DESCENDING, dict.fromkeys(_SETUPS, 0.31234))

    assert (
        str(refusal.value) == "the generated dataset gives every search setup the same nDCG@10, 0.3123, so it ranks no setup above another"
    )


def test_two_datasets_scored_under_different_setups_are_refused():
    with pytest.raises(ValueError, match="different search setups"):
        generator_check({"hybrid": 0.5, "keyword": 0.4}, {"hybrid": 0.5, "hybrid-raw": 0.4})


class _FakeScorer:
    """Answers the nDCG@10 scripted for each dataset name and setup, and records every call in order."""

    def __init__(self, ndcg: Mapping[str, Mapping[str, float]]) -> None:
        self.ndcg = ndcg
        self.calls: list[tuple[str, str, str]] = []

    async def __call__(self, dataset: Path, *, collection: str, setup: str) -> float:
        self.calls.append((dataset.name, collection, setup))
        return self.ndcg[dataset.name][setup]


def _files(tmp_path: Path, *names: str) -> list[Path]:
    paths = [tmp_path / name for name in names]
    for path in paths:
        path.write_text("cases: []\n", encoding="utf-8")
    return paths


async def test_the_service_scores_both_datasets_under_every_setup_in_order_and_compares_them(tmp_path: Path):
    generated = {**_DESCENDING, "hybrid": 0.6, "keyword": 0.7}
    scorer = _FakeScorer({"human.yaml": _DESCENDING, "generated.yaml": generated})
    human_path, generated_path = _files(tmp_path, "human.yaml", "generated.yaml")

    check = await check_generator(human_path, generated_path, collection="kb", scorer=scorer)

    assert scorer.calls == [(name, "kb", setup) for setup in _SETUPS for name in ("human.yaml", "generated.yaml")]
    assert check == generator_check(_DESCENDING, generated)
    assert [(row.human_ndcg, row.generated_ndcg) for row in check.setups[:2]] == [(0.7, 0.6), (0.6, 0.7)]
    assert check.passed is False


@pytest.mark.parametrize(
    ("human", "generated", "shared"),
    [
        ("human/scifact.yaml", "generated/scifact.yaml", "scifact.score.yaml"),
        ("scifact.yaml", "scifact.keyword.yaml", "scifact.keyword.score.yaml"),
        ("scifact.hybrid-raw.yaml", "scifact.yaml", "scifact.hybrid-raw.score.yaml"),
    ],
)
async def test_the_service_refuses_two_datasets_that_would_write_the_same_score_report_before_scoring(
    human: str, generated: str, shared: str
):
    scorer = _FakeScorer({})

    with pytest.raises(ValueError, match=f"would both write the score report '{shared}'"):
        await check_generator(Path(human), Path(generated), collection="kb", scorer=scorer)

    assert scorer.calls == []


async def test_the_service_checks_two_datasets_whose_names_differ_only_past_a_setup_free_suffix(tmp_path: Path):
    scorer = _FakeScorer({"scifact.yaml": _DESCENDING, "scifact.v2.yaml": _DESCENDING})
    human_path, generated_path = _files(tmp_path, "scifact.yaml", "scifact.v2.yaml")

    check = await check_generator(human_path, generated_path, collection="kb", scorer=scorer)

    assert check.passed is True


@pytest.mark.parametrize(("missing", "named"), [("human.yaml", "human-judged"), ("generated.yaml", "generated")])
async def test_the_service_refuses_a_missing_dataset_file_before_scoring_either(tmp_path: Path, missing: str, named: str):
    _files(tmp_path, *({"human.yaml", "generated.yaml"} - {missing}))
    scorer = _FakeScorer({})

    with pytest.raises(FileNotFoundError) as refusal:
        await check_generator(tmp_path / "human.yaml", tmp_path / "generated.yaml", collection="kb", scorer=scorer)

    assert str(refusal.value) == f"the {named} dataset {tmp_path / missing} does not exist"
    assert scorer.calls == []


def test_the_report_is_named_after_the_generated_dataset_and_carries_the_check(tmp_path: Path):
    check = generator_check(_DESCENDING, {**_DESCENDING, "hybrid": 0.6, "keyword": 0.7})

    yaml_path, markdown_path = write_generator_check(
        check, human=Path("human.yaml"), generated=Path("gen.v2.yaml"), collection="kb", out_dir=tmp_path / "scores"
    )

    assert yaml_path == tmp_path / "scores" / "gen.v2.generator-check.yaml"
    assert markdown_path == tmp_path / "scores" / "gen.v2.generator-check.md"
    document = yaml.safe_load(yaml_path.read_text(encoding="utf-8"))
    assert (document["human"], document["generated"], document["collection"]) == ("human.yaml", "gen.v2.yaml", "kb")
    assert GeneratorCheck.model_validate(document) == check
    assert document["rank_decimals"] == 4
    summary = markdown_path.read_text(encoding="utf-8")
    assert "# Generator check: gen.v2.yaml against human.yaml on kb" in summary
    assert "The check failed" in summary
    assert "| hybrid | 0.7000 | 1 | 0.6000 | 2 |" in summary
    assert f"Kendall's tau-b: {19 / 21:.4f}." in summary
    assert "Setup pairs both datasets order the same way: 20 of 21." in summary


def test_a_passing_report_says_so(tmp_path: Path):
    _, markdown_path = write_generator_check(
        generator_check(_DESCENDING, _DESCENDING), human=Path("h.yaml"), generated=Path("g.yaml"), collection="kb", out_dir=tmp_path
    )

    assert "The check passed" in markdown_path.read_text(encoding="utf-8")


_GOLD_RANK = {name: rank for rank, name in enumerate(_SETUPS, start=1)}
"""Where the scripted search ranks the gold document under each setup, so each setup scores a different nDCG@10."""


class _FakeSetupSearch:
    """Ranks the gold document `d1` at the rank scripted for the search's setup, behind filler documents."""

    def __init__(self) -> None:
        self.queries: list[Search] = []

    async def __call__(self, query: Search) -> SqlResult:
        self.queries.append(query)
        setup = next(name for name, knobs in SEARCH_SETUPS.items() if (knobs.hybrid, knobs.level) == (query.hybrid, query.level))
        ranked = [f"filler{index}" for index in range(1, _GOLD_RANK[setup])] + ["d1"]
        return SqlResult(rows=tuple({"id": f"{source}:0", "hit_level": "raw", "page_id": None} for source in ranked))


class _NoPages:
    """A knowledge base with no pages, so every id is read as a source document."""

    async def page_ids(self, collection: str, ids: Sequence[str]) -> frozenset[str]:
        del collection, ids
        return frozenset()

    async def source_ids(self, collection: str, page_ids: Sequence[str]) -> Mapping[str, str]:
        del collection, page_ids
        return {}


def _one_case(path: Path, judgments: dict[str, int], gold: str = "d1") -> Path:
    case = AuthoredCase(
        question=QaInput(question="Q1?"),
        expected=ExpectedQa(answer="", expected_source_ids=(gold,)),
        meta=QaCaseMeta(judgments=judgments),
        case_name="q1",
    )
    save_dataset(build_dataset([case]), path)
    return path


async def test_the_handler_writes_every_score_report_and_the_check_and_answers_the_verdict(tmp_path: Path):
    human = _one_case(tmp_path / "human.yaml", {"d1": 2})
    generated = _one_case(tmp_path / "generated.yaml", {})
    search = _FakeSetupSearch()
    out = tmp_path / "scores"

    report = await CheckGeneratorHandler(search, _NoPages()).handle(
        CheckGenerator(human=str(human), generated=str(generated), collection="kb", out=str(out))
    )

    assert (report.human, report.generated, report.collection) == ("human.yaml", "generated.yaml", "kb")
    assert report.check.passed is True
    assert [row.human_rank for row in report.check.setups] == [1, 2, 3, 4, 5, 6, 7]
    assert report.check.setups[1].human_ndcg == pytest.approx(1 / math.log2(3))
    assert len(search.queries) == 14
    assert report.out == str(out / "generated.generator-check.yaml")
    assert report.markdown == str(out / "generated.generator-check.md")
    score_reports = {path.name for path in out.glob("*.score.yaml")}
    assert score_reports == {
        f"{stem}.score.yaml" if setup == "hybrid" else f"{stem}.{setup}.score.yaml" for stem in ("human", "generated") for setup in _SETUPS
    }


async def test_a_dataset_that_scores_every_setup_the_same_is_refused_after_the_score_reports_naming_their_folder(tmp_path: Path):
    human = _one_case(tmp_path / "human.yaml", {"d1": 1})
    generated = _one_case(tmp_path / "generated.yaml", {}, gold="d9")
    out = tmp_path / "scores"

    with pytest.raises(SetupsTiedError) as refusal:
        await CheckGeneratorHandler(_FakeSetupSearch(), _NoPages()).handle(
            CheckGenerator(human=str(human), generated=str(generated), collection="kb", out=str(out))
        )

    assert str(refusal.value).startswith("the generated dataset gives every search setup the same nDCG@10, 0.0000")
    assert str(refusal.value).endswith(f". The score report of every setup is already in {out}")
    assert len(list(out.glob("*.score.yaml"))) == 14
    assert list(out.glob("*.generator-check.*")) == []
