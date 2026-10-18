"""Unit tests for the pure top-k overlap calc behind the dialect parity smoke.

Cover only the network-free, DB-free :func:`top_k_overlap` — the Jaccard-style overlap of two
top-k id lists. The CLI `main()` that drives it against two live backends is not exercised here.
"""

from __future__ import annotations

import pytest

from scripts.dialect_parity_smoke import DEFAULT_THRESHOLD, top_k_overlap


def test_identical_top_k_is_full_overlap():
    ids = ["a", "b", "c", "d"]
    assert top_k_overlap(ids, list(ids), k=4) == 1.0


def test_disjoint_top_k_is_zero_overlap():
    assert top_k_overlap(["a", "b", "c"], ["x", "y", "z"], k=3) == 0.0


def test_partial_overlap_is_intersection_over_union():
    # {a,b,c} vs {b,c,d}: |∩| = 2 ({b,c}), |∪| = 4 ({a,b,c,d}) -> 0.5
    assert top_k_overlap(["a", "b", "c"], ["b", "c", "d"], k=3) == 0.5


def test_k_truncates_each_list_before_comparing():
    # Beyond the first 2 the lists diverge; truncating to k=2 leaves them identical.
    a = ["a", "b", "z1", "z2"]
    b = ["a", "b", "y1", "y2"]
    assert top_k_overlap(a, b, k=2) == 1.0
    # Without truncation: ∩ = {a,b} (2), ∪ = {a,b,z1,z2,y1,y2} (6) -> 1/3.
    assert top_k_overlap(a, b, k=4) == pytest.approx(1 / 3)


def test_k_larger_than_lists_uses_what_is_present():
    assert top_k_overlap(["a", "b"], ["a", "b"], k=10) == 1.0


def test_two_empty_lists_are_full_overlap():
    # No divergence between two empty result sets — vacuously identical.
    assert top_k_overlap([], [], k=5) == 1.0


def test_one_empty_one_populated_is_zero_overlap():
    assert top_k_overlap([], ["a", "b"], k=5) == 0.0


def test_duplicate_ids_within_a_list_collapse_to_the_set():
    # A backend that repeats an id in its top-k must not inflate the union/intersection.
    assert top_k_overlap(["a", "a", "b"], ["a", "b"], k=3) == 1.0


def test_zero_k_compares_nothing_and_is_full_overlap():
    assert top_k_overlap(["a", "b"], ["c", "d"], k=0) == 1.0


def test_negative_k_is_rejected():
    with pytest.raises(ValueError, match="k"):
        top_k_overlap(["a"], ["a"], k=-1)


def test_default_threshold_is_a_fraction():
    assert 0.0 < DEFAULT_THRESHOLD <= 1.0
