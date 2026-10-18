r"""Unit specs for the decision-core / rotating-tail carving (`carve_core` and `draw_tail`).

`carve_core` draws one stratified subset of the train split, drawn once
per tuning run over `(archetype x difficulty)` by the salted-hash rule, folding any stratum
below the min-per-stratum floor into its parent difficulty band and flagging the fold. `draw_tail`
draws a fresh stratified sample of the remaining pool each round (a per-round hash key), disjoint
from the core by construction. Both draws are pure and deterministic given the salt, so these specs
never touch a model, DB, or the wall clock.
"""

from __future__ import annotations

from engram.evals.generation.contracts import AuthoredCase, EvaluationCase, EvaluationDataset, build_dataset
from engram.evals.shared.domain import Archetype, ExpectedQa, QaCaseMeta, QaDifficulty, QaInput, RatedDifficulty
from engram.evals.tuning.service import CoreTail, FoldedStratum, carve_core, draw_tail


def _cases(prefix: str, archetype: Archetype, difficulty: QaDifficulty, count: int) -> list[AuthoredCase]:
    """A group of `count` authored cases sharing one archetype and difficulty, keyed by a unique prefix."""
    return [
        AuthoredCase(
            question=QaInput(question=f"q-{prefix}{i}"),
            expected=ExpectedQa(answer="a", expected_source_ids=(f"{prefix}{i}",)),
            meta=QaCaseMeta(anchor_page_id=f"{prefix}{i}", difficulty=difficulty, archetype=archetype),
        )
        for i in range(count)
    ]


def _dataset(*groups: list[AuthoredCase]) -> EvaluationDataset:
    """A golden dataset over several case groups (`build_dataset` derives stable per-paper names)."""
    return build_dataset([case for group in groups for case in group])


def _names(cases: tuple[EvaluationCase, ...] | list[EvaluationCase]) -> set[str]:
    return {case.name or "" for case in cases}


def _stratum_counts(cases: tuple[EvaluationCase, ...] | list[EvaluationCase]) -> dict[tuple[Archetype | None, RatedDifficulty], int]:
    counts: dict[tuple[Archetype | None, RatedDifficulty], int] = {}
    for case in cases:
        meta = case.metadata
        key: tuple[Archetype | None, RatedDifficulty] = (meta.archetype, meta.difficulty or "unrated") if meta is not None else (None, "medium")
        counts[key] = counts.get(key, 0) + 1
    return counts


# -- carve_core --------------------------------------------------------------


class TestCarveCore:
    def test_draws_uniformly_across_feasible_strata(self):
        # Two equal-sized strata; a core of 6 splits 3/3 (proportional to the equal pools).
        dataset = _dataset(_cases("pin", Archetype.PINPOINT, "easy", 6), _cases("con", Archetype.SYNTHESIS, "hard", 6))

        core_tail = carve_core(dataset, core_size=6, min_per_stratum=1, salt="s")

        assert len(core_tail.core.cases) == 6
        assert _stratum_counts(core_tail.core.cases) == {(Archetype.PINPOINT, "easy"): 3, (Archetype.SYNTHESIS, "hard"): 3}
        assert core_tail.folded == ()

    def test_core_and_pool_partition_the_train_split(self):
        dataset = _dataset(_cases("pin", Archetype.PINPOINT, "easy", 10))

        core_tail = carve_core(dataset, core_size=4, min_per_stratum=1, salt="s")

        core_names = _names(core_tail.core.cases)
        pool_names = _names(core_tail.pool)
        assert len(core_names) == 4
        assert len(pool_names) == 6
        assert core_names.isdisjoint(pool_names)
        assert core_names | pool_names == _names(dataset.cases)

    def test_core_never_exceeds_the_train_split(self):
        dataset = _dataset(_cases("pin", Archetype.PINPOINT, "easy", 3))

        core_tail = carve_core(dataset, core_size=100, min_per_stratum=1, salt="s")

        assert len(core_tail.core.cases) == 3
        assert core_tail.pool == ()

    def test_thin_strata_fold_into_the_parent_band_and_are_flagged(self):
        # The 2-case pinpoint stratum is below the floor of 5, so it folds into the easy band and is
        # recorded; the 10-case synthesis stratum clears the floor and stays its own stratum.
        dataset = _dataset(_cases("pin", Archetype.PINPOINT, "easy", 2), _cases("con", Archetype.SYNTHESIS, "easy", 10))

        core_tail = carve_core(dataset, core_size=6, min_per_stratum=5, salt="s")

        assert FoldedStratum(archetype=Archetype.PINPOINT, difficulty="easy") in core_tail.folded
        assert FoldedStratum(archetype=Archetype.SYNTHESIS, difficulty="easy") not in core_tail.folded
        # The folded cases stay eligible for the core (folded into the parent band, not dropped).
        assert len(core_tail.core.cases) == 6

    def test_is_deterministic_per_salt(self):
        dataset = _dataset(_cases("pin", Archetype.PINPOINT, "easy", 10), _cases("con", Archetype.SYNTHESIS, "hard", 10))

        first = carve_core(dataset, core_size=8, min_per_stratum=1, salt="alpha")
        again = carve_core(dataset, core_size=8, min_per_stratum=1, salt="alpha")

        assert [case.name for case in first.core.cases] == [case.name for case in again.core.cases]

    def test_a_different_salt_draws_a_different_core(self):
        dataset = _dataset(_cases("pin", Archetype.PINPOINT, "easy", 10), _cases("con", Archetype.SYNTHESIS, "hard", 10))

        alpha = carve_core(dataset, core_size=8, min_per_stratum=1, salt="alpha")
        beta = carve_core(dataset, core_size=8, min_per_stratum=1, salt="beta")

        assert _names(alpha.core.cases) != _names(beta.core.cases)


# -- draw_tail ---------------------------------------------------------------


class TestDrawTail:
    def _carved(self) -> CoreTail:
        dataset = _dataset(_cases("pin", Archetype.PINPOINT, "easy", 10), _cases("con", Archetype.SYNTHESIS, "hard", 10))
        return carve_core(dataset, core_size=8, min_per_stratum=1, salt="s")

    def test_tail_is_disjoint_from_the_core_and_drawn_from_the_pool(self):
        core_tail = self._carved()

        tail = draw_tail(core_tail.pool, tail_size=4, salt="s", round_index=0)

        tail_names = _names(tail.cases)
        assert len(tail.cases) == 4
        assert tail_names.isdisjoint(_names(core_tail.core.cases))
        assert tail_names <= _names(core_tail.pool)

    def test_tail_carries_the_retrieval_evaluator(self):
        core_tail = self._carved()

        tail = draw_tail(core_tail.pool, tail_size=4, salt="s", round_index=0)

        # The tail is scored by the loop, so it must carry the golden evaluator like any dataset.
        assert len(tail.evaluators) == 1

    def test_tail_is_fresh_each_round(self):
        core_tail = self._carved()

        first = draw_tail(core_tail.pool, tail_size=4, salt="s", round_index=0)
        second = draw_tail(core_tail.pool, tail_size=4, salt="s", round_index=1)

        assert _names(first.cases) != _names(second.cases)

    def test_is_deterministic_per_salt_and_round(self):
        core_tail = self._carved()

        first = draw_tail(core_tail.pool, tail_size=4, salt="s", round_index=0)
        again = draw_tail(core_tail.pool, tail_size=4, salt="s", round_index=0)

        assert [case.name for case in first.cases] == [case.name for case in again.cases]

    def test_empty_pool_draws_an_empty_tail(self):
        tail = draw_tail((), tail_size=4, salt="s", round_index=0)

        assert tail.cases == []
