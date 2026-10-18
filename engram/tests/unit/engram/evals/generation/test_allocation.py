"""Unit tests for the pure hierarchical quota allocator.

These lock down `allocate`: the archetype level keeps `target_distribution`'s share semantics
(`round(weight * total_cases)`, matching `stratify`), while within an archetype the share spreads uniformly across feasible cells, capped at each cell's pool, with the
undersubscribed remainder redistributed to cells that still have headroom. Sub-1 quotas are never
emitted, an empty inventory yields an empty allocation, and the whole draw is deterministic.
"""

from engram.evals.generation.contracts import QuotaAllocation, Stratum, StratumCount, StratumInventory, allocate
from engram.evals.generation.domain.allocation import CellQuota
from engram.evals.shared.domain import DEFAULT_DIFFICULTY_THRESHOLDS, Archetype, QaDifficulty


def _stratum(archetype: Archetype, difficulty: QaDifficulty, anchor_page_type: str = "page") -> Stratum:
    return Stratum(archetype=archetype, difficulty=difficulty, anchor_page_type=anchor_page_type)


def _inventory(*counts: StratumCount) -> StratumInventory:
    return StratumInventory(collection="research", counts=counts)


def _by_archetype(allocation: QuotaAllocation) -> dict[Archetype, int]:
    totals: dict[Archetype, int] = {}
    for cell in allocation.cells:
        totals[cell.stratum.archetype] = totals.get(cell.stratum.archetype, 0) + cell.quota
    return totals


def _quota_of(allocation: QuotaAllocation, stratum: Stratum) -> int:
    return next((cell.quota for cell in allocation.cells if cell.stratum == stratum), 0)


# --- archetype level: share == round(weight * total_cases), unchanged vs target_distribution ----


def test_archetype_shares_match_target_distribution():
    # Deep pools per archetype, so nothing is pool-limited: each archetype gets its full share.
    pinpoint = _stratum(Archetype.PINPOINT, "easy")
    survey = _stratum(Archetype.SURVEY, "hard", "hub")
    inventory = _inventory(StratumCount(stratum=pinpoint, available=100), StratumCount(stratum=survey, available=100))
    allocation = allocate({Archetype.PINPOINT: 0.5, Archetype.SURVEY: 0.5}, 20, inventory, DEFAULT_DIFFICULTY_THRESHOLDS)

    assert _by_archetype(allocation) == {Archetype.PINPOINT: 10, Archetype.SURVEY: 10}


def test_archetype_absent_from_distribution_gets_no_quota():
    pinpoint = _stratum(Archetype.PINPOINT, "easy")
    survey = _stratum(Archetype.SURVEY, "hard", "hub")
    inventory = _inventory(StratumCount(stratum=pinpoint, available=100), StratumCount(stratum=survey, available=100))
    # Only pinpoint is weighted; survey is present in the inventory but carries no share.
    allocation = allocate({Archetype.PINPOINT: 1.0}, 10, inventory, DEFAULT_DIFFICULTY_THRESHOLDS)

    assert _by_archetype(allocation) == {Archetype.PINPOINT: 10}
    assert survey not in {cell.stratum for cell in allocation.cells}


# --- within an archetype: uniform across feasible cells -----------------------------------------


def test_uniform_across_feasible_cells():
    # Two feasible pinpoint cells (distinct anchor kinds) share the archetype's budget evenly.
    concept = _stratum(Archetype.PINPOINT, "easy", "concept")
    method = _stratum(Archetype.PINPOINT, "easy", "method")
    inventory = _inventory(StratumCount(stratum=concept, available=100), StratumCount(stratum=method, available=100))

    allocation = allocate({Archetype.PINPOINT: 1.0}, 10, inventory, DEFAULT_DIFFICULTY_THRESHOLDS)

    assert _quota_of(allocation, concept) == 5
    assert _quota_of(allocation, method) == 5


def test_odd_share_splits_as_evenly_as_possible():
    concept = _stratum(Archetype.PINPOINT, "easy", "concept")
    method = _stratum(Archetype.PINPOINT, "easy", "method")
    inventory = _inventory(StratumCount(stratum=concept, available=100), StratumCount(stratum=method, available=100))
    # Share of 5 over two cells: 3 + 2, the extra unit going to the earlier-sorted anchor kind.
    allocation = allocate({Archetype.PINPOINT: 0.5}, 10, inventory, DEFAULT_DIFFICULTY_THRESHOLDS)

    assert {_quota_of(allocation, concept), _quota_of(allocation, method)} == {3, 2}
    assert _quota_of(allocation, concept) == 3


def test_infeasible_band_strata_excluded():
    # Pinpoint can only produce "easy"; a "hard" pinpoint stratum is infeasible and never allocated.
    easy = _stratum(Archetype.PINPOINT, "easy")
    hard = _stratum(Archetype.PINPOINT, "hard")
    inventory = _inventory(StratumCount(stratum=easy, available=100), StratumCount(stratum=hard, available=100))

    allocation = allocate({Archetype.PINPOINT: 1.0}, 10, inventory, DEFAULT_DIFFICULTY_THRESHOLDS)

    assert _quota_of(allocation, easy) == 10
    assert hard not in {cell.stratum for cell in allocation.cells}


def test_zero_pool_strata_excluded():
    empty = _stratum(Archetype.PINPOINT, "easy", "empty")
    stocked = _stratum(Archetype.PINPOINT, "easy", "stocked")
    inventory = _inventory(StratumCount(stratum=empty, available=0), StratumCount(stratum=stocked, available=100))

    allocation = allocate({Archetype.PINPOINT: 1.0}, 10, inventory, DEFAULT_DIFFICULTY_THRESHOLDS)

    assert _quota_of(allocation, stocked) == 10
    assert empty not in {cell.stratum for cell in allocation.cells}


# --- pool caps + remainder redistribution -------------------------------------------------------


def test_pool_cap_and_remainder_redistribution():
    # The thin cell caps at its pool; the remainder it cannot absorb flows to the cell with headroom.
    thin = _stratum(Archetype.PINPOINT, "easy", "thin")
    deep = _stratum(Archetype.PINPOINT, "easy", "deep")
    inventory = _inventory(StratumCount(stratum=thin, available=2), StratumCount(stratum=deep, available=100))

    allocation = allocate({Archetype.PINPOINT: 1.0}, 10, inventory, DEFAULT_DIFFICULTY_THRESHOLDS)

    assert _quota_of(allocation, thin) == 2
    assert _quota_of(allocation, deep) == 8
    assert _by_archetype(allocation) == {Archetype.PINPOINT: 10}


def test_no_cell_quota_exceeds_its_pool():
    thin = _stratum(Archetype.PINPOINT, "easy", "thin")
    inventory = _inventory(StratumCount(stratum=thin, available=3))
    # Budget of 10 but only 3 in the pool: the archetype under-fills, capped at what exists.
    allocation = allocate({Archetype.PINPOINT: 1.0}, 10, inventory, DEFAULT_DIFFICULTY_THRESHOLDS)

    assert _quota_of(allocation, thin) == 3
    for cell in allocation.cells:
        available = next(count.available for count in inventory.counts if count.stratum == cell.stratum)
        assert cell.quota <= available


# --- sub-1 quotas never emitted -----------------------------------------------------------------


def test_sub_one_quotas_never_emitted():
    a = _stratum(Archetype.PINPOINT, "easy", "a")
    b = _stratum(Archetype.PINPOINT, "easy", "b")
    c = _stratum(Archetype.PINPOINT, "easy", "c")
    inventory = _inventory(
        StratumCount(stratum=a, available=100), StratumCount(stratum=b, available=100), StratumCount(stratum=c, available=100)
    )
    # A share of 2 across three feasible cells: two cells get 1, the third is omitted (never 0).
    allocation = allocate({Archetype.PINPOINT: 1.0}, 2, inventory, DEFAULT_DIFFICULTY_THRESHOLDS)

    assert len(allocation.cells) == 2
    assert all(cell.quota >= 1 for cell in allocation.cells)
    assert _by_archetype(allocation) == {Archetype.PINPOINT: 2}


# --- empty inventory ----------------------------------------------------------------------------


def test_empty_inventory_yields_empty_allocation():
    allocation = allocate({Archetype.PINPOINT: 1.0}, 10, _inventory(), DEFAULT_DIFFICULTY_THRESHOLDS)
    assert allocation == QuotaAllocation(cells=())
    assert allocation.cells == ()


def test_all_infeasible_inventory_yields_empty_allocation():
    hard = _stratum(Archetype.PINPOINT, "hard")  # pinpoint can never be hard
    allocation = allocate(
        {Archetype.PINPOINT: 1.0}, 10, _inventory(StratumCount(stratum=hard, available=50)), DEFAULT_DIFFICULTY_THRESHOLDS
    )
    assert allocation.cells == ()


# --- total never exceeds total_cases ------------------------------------------------------------


def test_total_never_exceeds_total_cases_when_weights_oversubscribe():
    pinpoint = _stratum(Archetype.PINPOINT, "easy")
    survey = _stratum(Archetype.SURVEY, "hard", "hub")
    inventory = _inventory(StratumCount(stratum=pinpoint, available=100), StratumCount(stratum=survey, available=100))
    # Weights sum to 1.2; naive shares would be 6 + 6 = 12 > 10, but the global budget caps the total.
    allocation = allocate({Archetype.PINPOINT: 0.6, Archetype.SURVEY: 0.6}, 10, inventory, DEFAULT_DIFFICULTY_THRESHOLDS)

    total = sum(cell.quota for cell in allocation.cells)
    assert total == 10
    assert total <= 10


# --- determinism --------------------------------------------------------------------------------


def test_determinism_same_inputs_identical_output_tuple():
    a = _stratum(Archetype.SYNTHESIS, "medium", "page")
    b = _stratum(Archetype.SYNTHESIS, "hard", "page")
    inventory = _inventory(StratumCount(stratum=a, available=50), StratumCount(stratum=b, available=50))
    distribution = {Archetype.SYNTHESIS: 1.0}

    first = allocate(distribution, 10, inventory, DEFAULT_DIFFICULTY_THRESHOLDS)
    second = allocate(distribution, 10, inventory, DEFAULT_DIFFICULTY_THRESHOLDS)

    assert first == second
    assert first.cells == second.cells


def test_output_order_is_independent_of_input_dict_order():
    pinpoint = _stratum(Archetype.PINPOINT, "easy")
    survey = _stratum(Archetype.SURVEY, "hard", "hub")
    inventory = _inventory(StratumCount(stratum=pinpoint, available=100), StratumCount(stratum=survey, available=100))
    forward = allocate({Archetype.PINPOINT: 0.5, Archetype.SURVEY: 0.5}, 20, inventory, DEFAULT_DIFFICULTY_THRESHOLDS)
    reversed_dist = allocate({Archetype.SURVEY: 0.5, Archetype.PINPOINT: 0.5}, 20, inventory, DEFAULT_DIFFICULTY_THRESHOLDS)

    assert forward.cells == reversed_dist.cells


def test_cells_are_sorted_by_archetype_then_difficulty_then_kind():
    survey = _stratum(Archetype.SURVEY, "hard", "hub")
    pinpoint = _stratum(Archetype.PINPOINT, "easy")
    inventory = _inventory(StratumCount(stratum=survey, available=100), StratumCount(stratum=pinpoint, available=100))
    allocation = allocate({Archetype.PINPOINT: 0.5, Archetype.SURVEY: 0.5}, 20, inventory, DEFAULT_DIFFICULTY_THRESHOLDS)

    ordered = [(cell.stratum.archetype.value, cell.stratum.difficulty, cell.stratum.anchor_page_type) for cell in allocation.cells]
    assert ordered == sorted(ordered)


# --- frozen value objects -----------------------------------------------------------------------


def test_cell_quota_and_allocation_are_frozen():
    cell = CellQuota(stratum=_stratum(Archetype.PINPOINT, "easy"), quota=3)
    allocation = QuotaAllocation(cells=(cell,))
    for frozen, field, value in ((cell, "quota", 9), (allocation, "cells", ())):
        try:
            setattr(frozen, field, value)
        except (ValueError, TypeError):
            continue
        msg = f"{type(frozen).__name__}.{field} should be frozen"
        raise AssertionError(msg)
