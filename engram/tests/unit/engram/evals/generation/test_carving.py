"""Unit specs for the shared salted key and stratified carve that frozen, top-up and tuning share."""

from __future__ import annotations

import hashlib

import pytest

from engram.evals.generation.contracts import (
    AuthoredCase,
    EvaluationCase,
    EvaluationDataset,
    build_dataset,
    carve_frozen,
    carve_strata,
    case_stratum,
    salted_key,
)
from engram.evals.shared.domain import Archetype, ExpectedQa, QaCaseMeta, QaDifficulty, QaInput
from engram.evals.tuning.service import carve_core


@pytest.mark.parametrize(("text", "salt"), [("paper-0", "s1"), ("survey:cites:p9", ""), ("", "abc"), ("a|b", "c")])
def test_salted_key_is_the_md5_of_text_bar_salt(text: str, salt: str) -> None:
    assert salted_key(text, salt) == hashlib.md5(f"{text}|{salt}".encode(), usedforsecurity=False).hexdigest()


def test_salted_key_changes_with_the_salt() -> None:
    assert salted_key("paper-0", "one") != salted_key("paper-0", "two")


def _cases(spec: dict[tuple[Archetype, QaDifficulty], int]) -> list[EvaluationCase]:
    """Build cases from a `{(archetype, difficulty): count}` spec, one distinct paper id each."""
    records: list[AuthoredCase] = []
    for (archetype, difficulty), count in spec.items():
        for index in range(count):
            paper_id = f"{archetype.value}-{difficulty}-{index}"
            records.append(
                AuthoredCase(
                    question=QaInput(question=f"q-{paper_id}"),
                    expected=ExpectedQa(answer="a", expected_source_ids=(paper_id,)),
                    meta=QaCaseMeta(anchor_page_id=paper_id, difficulty=difficulty, archetype=archetype),
                )
            )
    return list(build_dataset(records).cases)


def _dataset(cases: list[EvaluationCase]) -> EvaluationDataset:
    """Wrap cases in a dataset for the core carve."""
    return EvaluationDataset(name="d", cases=cases)


# Two thin strata fold into the unlabelled medium band, which then ties the trace band on its remainder.
_TIED_BANDS: dict[tuple[Archetype, QaDifficulty], int] = {
    (Archetype.TRACE, "medium"): 2,
    (Archetype.PINPOINT, "medium"): 1,
    (Archetype.SYNTHESIS, "medium"): 1,
}


def test_frozen_and_core_carves_pick_the_same_cases_on_a_tied_remainder() -> None:
    cases = _cases(_TIED_BANDS)
    core = carve_core(_dataset(cases), core_size=1, min_per_stratum=2, salt="s")

    frozen = carve_frozen(cases, fraction=0.25, salt="s", min_per_stratum=2)

    assert frozen == tuple(case.name for case in core.core.cases)


def test_carve_strata_reports_the_folded_strata_and_selects_the_total() -> None:
    cases = _cases(_TIED_BANDS)

    selected, folded = carve_strata(cases, total=3, min_per_stratum=2, salt="s")

    assert len(selected) == 3
    assert sorted(folded, key=str) == sorted([(Archetype.PINPOINT, "medium"), (Archetype.SYNTHESIS, "medium")], key=str)


def test_case_stratum_reads_the_archetype_and_difficulty() -> None:
    (case,) = _cases({(Archetype.SURVEY, "hard"): 1})

    assert case_stratum(case) == (Archetype.SURVEY, "hard")
