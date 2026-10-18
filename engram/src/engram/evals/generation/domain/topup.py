r"""The top-up planner: which strata a new dataset version drafts and which parent cases it retires.

`plan_top_up` is pure: the same parent cases, stamp, inventory and allocation always give the same plan.
It reads what to draft per stratum (archetype, difficulty and anchor page type) from the parent stamp's
coverage map. It decides what to retire per `(archetype, difficulty)` band.
"""

from __future__ import annotations

from collections import defaultdict
from typing import TYPE_CHECKING

from pydantic import BaseModel, ConfigDict

from engram.evals.generation.domain.allocation import CellQuota
from engram.evals.generation.domain.carving import salted_key
from engram.evals.generation.domain.coverage import stratum_label
from engram.evals.generation.domain.datasets import RetirementRecord
from engram.evals.shared.contracts import Archetype, QaDifficulty

if TYPE_CHECKING:
    from collections.abc import Iterable, Mapping, Sequence

    from engram.evals.generation.domain.allocation import QuotaAllocation
    from engram.evals.generation.domain.coverage import Stratum, StratumInventory
    from engram.evals.generation.domain.datasets import DatasetStamp, EvaluationCase

# The stable-case-identity separator: a name is `{seed_key}#{draft_ordinal}`. A seed key holds at most one `#`,
# inside the claim id or section id it names (`<page-id>#<part>`), and the ordinal holds none, so splitting on
# the last `#` recovers the seed key.
_ORDINAL_SEPARATOR = "#"

type _Band = tuple[Archetype, QaDifficulty]


class ParentCaseRef(BaseModel):
    """The part of one parent case the top-up planner reads: its identity, stratum and gold pages.

    `parent_case_refs` builds these from a loaded dataset's cases.

    Attributes:
        case_name: The stable `{seed_key}#{draft_ordinal}` name, the key a retirement record names.
        seed_key: The seed the case was drafted from, the stable key the salted-hash ranking orders by.
        archetype: The case's structural archetype (from its `QaCaseMeta`).
        difficulty: The case's derived difficulty band (from its `QaCaseMeta`).
        gold_ids: The case's gold page ids, checked against the knowledge base for `gold_gone` attribution.
    """

    model_config = ConfigDict(frozen=True)

    case_name: str
    seed_key: str
    archetype: Archetype
    difficulty: QaDifficulty
    gold_ids: tuple[str, ...]


class TopUpPlan(BaseModel):
    """The deterministic plan for one coverage-gap top-up: what to generate and what to retire.

    Attributes:
        parent_version: The version this top-up is derived from, the new version's `parent_version` link.
        generate: The under-covered strata to draft new cases for, each with its gap quota, empty when
            the knowledge base offers no new coverage. One cell per stratum (archetype, difficulty and
            anchor page type), plus extra cells for any band that retirement left below its quota.
        retire: The retirement records of the parent cases dropped (displaced by a smaller quota, stale,
            or with their gold pages gone), ordered by case name.
    """

    model_config = ConfigDict(frozen=True)

    parent_version: str
    generate: tuple[CellQuota, ...]
    retire: tuple[RetirementRecord, ...]


def parent_case_refs(cases: Iterable[EvaluationCase]) -> list[ParentCaseRef]:
    """Extract the top-up planner's `ParentCaseRef`s from a loaded dataset's cases.

    A case with no metadata, no archetype or no name is skipped, since it has no stratum.
    A skipped case is never retired, so a top-up keeps it unchanged.

    Args:
        cases: The parent dataset's cases (each a coverage-stamped `EvaluationCase` carrying a stable name).

    Returns:
        One `ParentCaseRef` per attributable case, in input order.
    """
    refs: list[ParentCaseRef] = []
    for case in cases:
        meta = case.metadata
        if meta is None or meta.archetype is None or meta.difficulty is None or case.name is None:
            continue
        gold = tuple(case.expected_output.cited_source_ids) if case.expected_output is not None else ()
        refs.append(
            ParentCaseRef(
                case_name=case.name, seed_key=_seed_key_of(case.name), archetype=meta.archetype, difficulty=meta.difficulty, gold_ids=gold
            )
        )
    return refs


def plan_top_up(  # noqa: PLR0913 (each pure top-up input is distinct, and bundling them would obscure the contract)
    parent_cases: Sequence[ParentCaseRef],
    stamp: DatasetStamp,
    inventory: StratumInventory,
    allocation: QuotaAllocation,
    *,
    parent_version: str,
    existing_page_ids: frozenset[str] | None = None,
    salt: str | None = None,
    stale_case_names: frozenset[str] = frozenset(),
) -> TopUpPlan:
    """Plan a coverage-gap top-up: the strata to draft and the parent cases to retire.

    A stratum's gap is its fresh quota, capped at the current seed pool, minus what the parent stamp
    already covered. Retirement runs per `(archetype, difficulty)` band in the order `gold_gone`, then
    `stale`, then `displaced`, and each band keeps its first cases in `salted_key(seed_key, salt)` order
    up to its fresh quota. A band that retirement leaves below its quota gets extra cells to draft the
    shortfall. The order is stable per seed key, so only the displaced tail changes.

    Args:
        parent_cases: The parent dataset's case references (from `parent_case_refs`).
        stamp: The parent version's stamp, supplying the coverage summary the gap is read against and
            the default sampling salt the ranking uses.
        inventory: The current knowledge base's per-stratum seed-pool census, bounding each generation gap.
        allocation: The fresh per-stratum quota allocation over the current knowledge base.
        parent_version: The parent version's file name, recorded on the plan as the new version's parent.
        existing_page_ids: The knowledge base's current page ids. When given, a parent case with any gold
            id missing from them retires as `gold_gone`. `None` skips that check.
        salt: A ranking salt in place of the stamp's. `None` uses `stamp.salt`, which changes the fewest
            cases. Another salt changes which cases survive.
        stale_case_names: The parent case names a freshness check found stale. Each retires as `stale`
            before the ranking, and its band drafts a replacement. Empty by default.

    Returns:
        The plan, with the cells to draft and the retirement records.
    """
    ranking_salt = stamp.salt if salt is None else salt
    retire = _retirements(parent_cases, allocation, ranking_salt, existing_page_ids, stale_case_names)
    generate = _gap_cells(stamp, inventory, allocation, parent_cases, retire)
    return TopUpPlan(parent_version=parent_version, generate=tuple(generate), retire=tuple(retire))


def _case_band(case: EvaluationCase) -> tuple[Archetype, QaDifficulty] | None:
    """The `(archetype, difficulty)` band a case belongs to, or None for a case with no archetype or no difficulty."""
    meta = case.metadata
    if meta is None or meta.archetype is None or meta.difficulty is None:
        return None
    return (meta.archetype, meta.difficulty)


def merge_top_up(
    parent_cases: Sequence[EvaluationCase], plan: TopUpPlan, fresh_cases: Sequence[EvaluationCase], allocation: QuotaAllocation
) -> list[EvaluationCase]:
    """Merge the surviving parent cases with the gap-filling newcomers, capped at each band's fresh quota.

    Drops the plan's retired cases, keeps every survivor as drafted, then fills each
    `(archetype, difficulty)` band up to its fresh quota from `fresh_cases`. Survivors keep parent order
    and newcomers keep pipeline order, so the merge is deterministic. A newcomer with a survivor's name is skipped.

    Args:
        parent_cases: The parent version's cases.
        plan: The top-up plan whose `retire` records name the cases to drop.
        fresh_cases: The freshly drafted candidate cases for the gap archetypes.
        allocation: The fresh per-stratum quota allocation (its per-band totals cap each band).

    Returns:
        The merged case list, survivors then gap-filling newcomers, capped per band.
    """
    band_quota: dict[tuple[Archetype, QaDifficulty], int] = {}
    for cell in allocation.cells:
        band = (cell.stratum.archetype, cell.stratum.difficulty)
        band_quota[band] = band_quota.get(band, 0) + cell.quota

    retired_names = {record.case_name for record in plan.retire}
    merged: list[EvaluationCase] = []
    seen_names: set[str] = set()
    filled: dict[tuple[Archetype, QaDifficulty], int] = {}
    for case in parent_cases:
        if case.name in retired_names:
            continue
        merged.append(case)
        if case.name is not None:
            seen_names.add(case.name)
        band = _case_band(case)
        if band is not None:
            filled[band] = filled.get(band, 0) + 1
    for case in fresh_cases:
        band = _case_band(case)
        if band is None or case.name in seen_names or filled.get(band, 0) >= band_quota.get(band, 0):
            continue
        merged.append(case)
        if case.name is not None:
            seen_names.add(case.name)
        filled[band] = filled.get(band, 0) + 1
    return merged


def _gap_cells(
    stamp: DatasetStamp,
    inventory: StratumInventory,
    allocation: QuotaAllocation,
    parent_cases: Sequence[ParentCaseRef],
    retire: Sequence[RetirementRecord],
) -> list[CellQuota]:
    """The cells to draft: each stratum's positive gap over the parent stamp, then the band deficits retirement opened."""
    available = {count.stratum: count.available for count in inventory.counts}
    cells: list[CellQuota] = []
    planned_gap: dict[Stratum, int] = {}
    for cell in allocation.cells:
        covered = stamp.coverage.get(stratum_label(cell.stratum), 0)
        gap = min(cell.quota, available.get(cell.stratum, 0)) - covered
        if gap > 0:
            cells.append(CellQuota(stratum=cell.stratum, quota=gap))
            planned_gap[cell.stratum] = gap
    cells.extend(_deficit_cells(stamp, allocation, available, planned_gap, _retired_by_band(parent_cases, retire)))
    return cells


def _deficit_cells(
    stamp: DatasetStamp,
    allocation: QuotaAllocation,
    available: Mapping[Stratum, int],
    planned_gap: Mapping[Stratum, int],
    retired_by_band: Mapping[_Band, int],
) -> list[CellQuota]:
    """The extra cells that redraft what retirement removed below a band's quota, each bounded by its remaining pool."""
    band_quota: dict[_Band, int] = defaultdict(int)
    covered: dict[_Band, int] = defaultdict(int)
    band_cells: dict[_Band, list[CellQuota]] = defaultdict(list)
    for cell in allocation.cells:
        band: _Band = (cell.stratum.archetype, cell.stratum.difficulty)
        band_quota[band] += cell.quota
        covered[band] += stamp.coverage.get(stratum_label(cell.stratum), 0)
        band_cells[band].append(cell)

    deficit_cells: list[CellQuota] = []
    for band, cells in band_cells.items():
        survivors = max(0, covered[band] - retired_by_band.get(band, 0))
        deficit = max(0, band_quota[band] - survivors) - sum(planned_gap.get(cell.stratum, 0) for cell in cells)
        for cell in cells:
            if deficit <= 0:
                break
            take = min(deficit, available.get(cell.stratum, 0) - planned_gap.get(cell.stratum, 0))
            if take > 0:
                deficit_cells.append(CellQuota(stratum=cell.stratum, quota=take))
                deficit -= take
    return deficit_cells


def _retired_by_band(parent_cases: Sequence[ParentCaseRef], retire: Sequence[RetirementRecord]) -> dict[_Band, int]:
    """The number of retired parent cases per `(archetype, difficulty)` band."""
    band_of: dict[str, _Band] = {ref.case_name: (ref.archetype, ref.difficulty) for ref in parent_cases}
    retired: dict[_Band, int] = defaultdict(int)
    for record in retire:
        band = band_of.get(record.case_name)
        if band is not None:
            retired[band] += 1
    return retired


def _retirements(
    parent_cases: Sequence[ParentCaseRef],
    allocation: QuotaAllocation,
    salt: str,
    existing_page_ids: frozenset[str] | None,
    stale_case_names: frozenset[str],
) -> list[RetirementRecord]:
    """The retirement records for a top-up, `gold_gone` then `stale` then the `displaced` tail of each band, sorted by name."""
    band_quota: dict[_Band, int] = defaultdict(int)
    for cell in allocation.cells:
        band_quota[(cell.stratum.archetype, cell.stratum.difficulty)] += cell.quota

    retire: list[RetirementRecord] = []
    live_by_band: dict[_Band, list[ParentCaseRef]] = defaultdict(list)
    for ref in parent_cases:
        if existing_page_ids is not None and not all(gold in existing_page_ids for gold in ref.gold_ids):
            retire.append(RetirementRecord(case_name=ref.case_name, seed_key=ref.seed_key, reason="gold_gone"))
        elif ref.case_name in stale_case_names:
            retire.append(RetirementRecord(case_name=ref.case_name, seed_key=ref.seed_key, reason="stale"))
        else:
            live_by_band[(ref.archetype, ref.difficulty)].append(ref)

    for band, refs in live_by_band.items():
        ranked = sorted(refs, key=lambda ref: salted_key(ref.seed_key, salt))
        retire.extend(
            RetirementRecord(case_name=ref.case_name, seed_key=ref.seed_key, reason="displaced") for ref in ranked[band_quota[band] :]
        )

    retire.sort(key=lambda record: record.case_name)
    return retire


def _seed_key_of(case_name: str) -> str:
    """Recover the seed key from a stable `{seed_key}#{draft_ordinal}` case name (the `#` suffix stripped)."""
    return case_name.rsplit(_ORDINAL_SEPARATOR, 1)[0]
