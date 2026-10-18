"""The quota allocator: how many cases each stratum gets, from archetype shares split over the feasible strata.

Each archetype's share is `round(weight * total_cases)`, its weight taken from `target_distribution`. The share spreads
evenly over the archetype's feasible cells (a band `coverage.feasible_bands` allows, with a non-empty seed pool),
capped at each cell's pool, and the remainder goes to the cells that still have room. A cell gets at least one case
or is left out. The total never exceeds `total_cases`, and an empty or wholly infeasible inventory gives an empty
allocation. Everything here is deterministic, with no randomness and no clock.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from pydantic import BaseModel, ConfigDict

from engram.evals.generation.domain.coverage import DIFFICULTY_ORDER, Stratum, feasible_bands

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence

    from engram.evals.generation.domain.coverage import StratumInventory
    from engram.evals.shared.contracts import Archetype, DifficultyThresholds

class CellQuota(BaseModel):
    """One allocated coverage cell: a stratum and the number of cases targeted for it.

    The quota is always at least one. A stratum that would get nothing is left out of the allocation.
    """

    model_config = ConfigDict(frozen=True)

    stratum: Stratum
    quota: int


class QuotaAllocation(BaseModel):
    """The full per-stratum quota plan for one generation run: the allocated cells in draw order.

    Cells are ordered by `(archetype, difficulty, anchor page type)`. Build the per-stratum target mapping
    coverage expects with `{cell.stratum: cell.quota for cell in cells}`.
    """

    model_config = ConfigDict(frozen=True)

    cells: tuple[CellQuota, ...]


def _feasible_cell_strata(
    archetype: Archetype, available_by_stratum: Mapping[Stratum, int], thresholds: DifficultyThresholds
) -> list[Stratum]:
    """The archetype's feasible inventory strata with a non-empty seed pool, ordered by difficulty then anchor page type."""
    feasible = feasible_bands(archetype, thresholds)
    return sorted(
        (
            stratum
            for stratum, available in available_by_stratum.items()
            if stratum.archetype == archetype and stratum.difficulty in feasible and available > 0
        ),
        key=lambda stratum: (DIFFICULTY_ORDER[stratum.difficulty], stratum.anchor_page_type),
    )


def _distribute(share: int, caps: Sequence[int]) -> list[int]:
    """Split `share` as evenly as possible over cells capped at `caps`, one unit at a time in the given order."""
    quotas = [0] * len(caps)
    remaining = share
    while remaining > 0:
        progressed = False
        for index, cap in enumerate(caps):
            if remaining == 0:
                break
            if quotas[index] < cap:
                quotas[index] += 1
                remaining -= 1
                progressed = True
        if not progressed:
            break
    return quotas


def allocate(
    distribution: Mapping[Archetype, float], total_cases: int, inventory: StratumInventory, thresholds: DifficultyThresholds
) -> QuotaAllocation:
    """Allocate per-stratum case quotas over a knowledge base's feasible strata, archetype by archetype.

    Each archetype in `distribution` gets `round(weight * total_cases)` cases, spread evenly over its feasible
    cells, capped at each cell's seed pool, with the remainder moved to the cells that still have room.
    Archetypes share one running budget, so weights that add up past one still never push the total past
    `total_cases`. A stratum outside `distribution`, structurally infeasible or with an empty pool gets nothing.

    Args:
        distribution: The target weight per archetype (read as a share of `total_cases`).
        total_cases: The overall case budget the grand total is capped at.
        inventory: The knowledge base's per-stratum seed-pool census.
        thresholds: The difficulty band cutoffs feasibility is derived against.

    Returns:
        The `QuotaAllocation` whose cells, ordered by `(archetype, difficulty, anchor page type)`, carry the
        per-stratum quotas.
    """
    available_by_stratum = {count.stratum: count.available for count in inventory.counts}
    cells: list[CellQuota] = []
    budget_remaining = total_cases
    for archetype in sorted(distribution, key=lambda archetype: archetype.value):
        if budget_remaining <= 0:
            break
        share = min(round(distribution[archetype] * total_cases), budget_remaining)
        if share <= 0:
            continue
        cell_strata = _feasible_cell_strata(archetype, available_by_stratum, thresholds)
        caps = [available_by_stratum[stratum] for stratum in cell_strata]
        for stratum, quota in zip(cell_strata, _distribute(share, caps), strict=True):
            if quota > 0:
                cells.append(CellQuota(stratum=stratum, quota=quota))
                budget_remaining -= quota
    # Stamps and reports rely on this order, never on the append order above.
    cells.sort(key=lambda cell: (cell.stratum.archetype.value, DIFFICULTY_ORDER[cell.stratum.difficulty], cell.stratum.anchor_page_type))
    return QuotaAllocation(cells=tuple(cells))
