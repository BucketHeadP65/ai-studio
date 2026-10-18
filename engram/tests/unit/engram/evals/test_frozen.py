r"""Unit specs for the frozen-split carve and its versioned sidecar.

`frozen.py` mechanizes the eval loop's held-out yardstick: `carve_frozen` draws a stratified
salted-hash subset of an immutable dataset version's case names (the same `(archetype x difficulty)`
salted-hash idiom `tuning.carve_core` uses), and `save_frozen_split` / `load_frozen_split` persist
it as an append-only `<name>.frozen.v{M}.json` chain behind a `<name>.frozen.current` pointer, mirroring
the version-chain idioms of `datasets.py`. Pure and file-IO-only, so every spec runs Docker-free over a
`tmp_path`, the only IO being the sidecar round-trip.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from engram.evals.generation.contracts import (
    AuthoredCase,
    EvaluationCase,
    FrozenSplit,
    build_dataset,
    carve_frozen,
    frozen_stratum_counts,
    load_frozen_split,
    next_frozen_version,
    save_frozen_split,
)
from engram.evals.generation.domain.frozen import frozen_version_path
from engram.evals.shared.domain import Archetype, ExpectedQa, QaCaseMeta, QaDifficulty, QaInput

if TYPE_CHECKING:
    from pathlib import Path


def _cases(spec: dict[tuple[Archetype, QaDifficulty], int]) -> list[EvaluationCase]:
    """Build golden cases from a `{(archetype, difficulty): count}` spec, one distinct paper id each."""
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


def _split(name: str = "research", *, version: int = 3, salt: str = "s", parent: int | None = None) -> FrozenSplit:
    """A frozen split fixture with two case names, for the sidecar round-trip specs."""
    return FrozenSplit(
        dataset_name=name,
        dataset_version=version,
        case_names=("pinpoint-easy-0", "survey-hard-1"),
        salt=salt,
        frozen_fraction=0.2,
        frozen_at="2026-07-05T00:00:00+00:00",
        staleness_at_freeze=0.0,
        parent_frozen_version=parent,
    )


class TestCarveFrozen:
    def test_is_deterministic_for_a_fixed_cases_and_salt(self):
        cases = _cases({(Archetype.PINPOINT, "easy"): 6, (Archetype.SURVEY, "hard"): 6})

        first = carve_frozen(cases, fraction=0.5, salt="alpha", min_per_stratum=2)
        second = carve_frozen(cases, fraction=0.5, salt="alpha", min_per_stratum=2)

        assert first == second

    def test_a_different_salt_draws_a_different_subset(self):
        cases = _cases({(Archetype.PINPOINT, "easy"): 8, (Archetype.SURVEY, "hard"): 8})

        alpha = carve_frozen(cases, fraction=0.5, salt="alpha", min_per_stratum=2)
        beta = carve_frozen(cases, fraction=0.5, salt="beta", min_per_stratum=2)

        assert set(alpha) != set(beta)  # the salted-hash order reorders each band, so the leading draw shifts

    def test_draws_the_fraction_as_a_subset_without_duplicates(self):
        cases = _cases({(Archetype.PINPOINT, "easy"): 6, (Archetype.SURVEY, "hard"): 4})

        frozen = carve_frozen(cases, fraction=0.3, salt="s", min_per_stratum=2)

        names = {case.name for case in cases}
        assert len(frozen) == 3  # round(0.3 * 10)
        assert len(set(frozen)) == len(frozen)  # no duplicate drawn
        assert set(frozen) <= names  # every drawn name is a real case

    def test_is_stratified_across_bands(self):
        cases = _cases({(Archetype.PINPOINT, "easy"): 4, (Archetype.SURVEY, "hard"): 4})

        frozen = carve_frozen(cases, fraction=0.5, salt="s", min_per_stratum=2)

        counts = frozen_stratum_counts(cases, frozen)
        assert counts == {"pinpoint/easy": 2, "survey/hard": 2}  # 4 drawn, evenly across the two equal strata

    def test_min_per_stratum_fold_keeps_a_thin_stratum_eligible(self):
        # A lone survey/hard case is below the floor, so it folds into the (None, hard) band rather than
        # being dropped: freezing the whole version returns every name including the thin one.
        cases = _cases({(Archetype.PINPOINT, "easy"): 3, (Archetype.SURVEY, "hard"): 1})

        frozen = carve_frozen(cases, fraction=1.0, salt="s", min_per_stratum=2)

        assert set(frozen) == {case.name for case in cases}

    def test_empty_cases_carve_to_nothing(self):
        assert carve_frozen([], fraction=0.2, salt="s", min_per_stratum=2) == ()


class TestFrozenStratumCounts:
    def test_counts_only_the_named_subset_by_raw_stratum(self):
        cases = _cases({(Archetype.PINPOINT, "easy"): 3, (Archetype.TRACE, "medium"): 2})
        by_archetype = {case.metadata.archetype: case.name for case in cases if case.metadata is not None and case.name is not None}

        counts = frozen_stratum_counts(cases, [by_archetype[Archetype.PINPOINT], by_archetype[Archetype.TRACE]])

        assert counts == {"pinpoint/easy": 1, "trace/medium": 1}


class TestFrozenSidecarRoundTrip:
    def test_save_writes_the_versioned_json_and_moves_the_pointer(self, tmp_path: Path):
        path = save_frozen_split(_split(), tmp_path)

        assert path == frozen_version_path(tmp_path, "research", 1)
        assert path.exists()
        pointer = tmp_path / "research.frozen.current"
        assert pointer.read_text(encoding="utf-8").strip() == "research.frozen.v1.json"

    def test_load_resolves_the_pointer_and_re_validates(self, tmp_path: Path):
        saved = _split()
        save_frozen_split(saved, tmp_path)

        loaded = load_frozen_split(tmp_path, "research")

        assert loaded == saved

    def test_load_is_none_when_never_frozen(self, tmp_path: Path):
        assert load_frozen_split(tmp_path, "research") is None

    def test_load_is_none_when_the_pointer_dangles(self, tmp_path: Path):
        (tmp_path / "research.frozen.current").write_text("research.frozen.v9.json", encoding="utf-8")

        assert load_frozen_split(tmp_path, "research") is None

    def test_freezing_again_writes_the_next_version_and_leaves_the_earlier_one_untouched(self, tmp_path: Path):
        first = _split(salt="first")
        v1 = save_frozen_split(first, tmp_path)

        assert next_frozen_version(tmp_path, "research") == 2  # v1 written, v2 is next
        second = _split(salt="second", parent=1)
        v2 = save_frozen_split(second, tmp_path)

        assert v2 == frozen_version_path(tmp_path, "research", 2)
        assert v1.exists()  # append-only: the earlier version is never deleted
        assert FrozenSplit.model_validate_json(v1.read_text(encoding="utf-8")) == first  # nor mutated
        pointer = tmp_path / "research.frozen.current"
        assert pointer.read_text(encoding="utf-8").strip() == "research.frozen.v2.json"  # pointer moved to v2
        assert load_frozen_split(tmp_path, "research") == second

    def test_next_frozen_version_is_one_on_a_clean_directory(self, tmp_path: Path):
        assert next_frozen_version(tmp_path, "research") == 1

    def test_load_rejects_a_tampered_pointer_body(self, tmp_path: Path):
        # The pointer is written as a bare basename; a hand-edited traversal fails at the boundary
        # instead of redirecting the read outside the datasets dir.
        (tmp_path / "research.frozen.current").write_text("../evil.json", encoding="utf-8")

        with pytest.raises(ValueError, match="bare file name"):
            load_frozen_split(tmp_path, "research")

    def test_load_rejects_a_non_slug_chain_name(self, tmp_path: Path):
        with pytest.raises(ValueError, match="plain name"):
            load_frozen_split(tmp_path, "../evil")

    def test_save_rejects_a_non_slug_dataset_name(self, tmp_path: Path):
        with pytest.raises(ValueError, match="plain name"):
            save_frozen_split(_split(name="../evil"), tmp_path)
