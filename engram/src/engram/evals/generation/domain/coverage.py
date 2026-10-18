"""The coverage domain: structural strata, the bands each archetype can produce, and the coverage debt score.

A dataset's coverage is measured from the knowledge base's structure alone, never by a model. Its unit is a
stratum, an archetype crossed with a derived difficulty band and an anchor page type. `feasible_bands` finds the
bands an archetype can produce by running `derive_difficulty` over its seeder's shapes. `coverage_debt` is one
trendable score, zero at target and rising with every gap. `build_coverage_report` sorts the strata into a report.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, assert_never

from pydantic import BaseModel, ConfigDict

from engram.evals.generation.domain.seed_shapes import MIN_SURVEY_FANOUT, MIN_SYNTHESIS_FANOUT, TRACE_HOPS
from engram.evals.shared.contracts import Archetype, QaDifficulty, derive_difficulty

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence

    from engram.evals.generation.domain.allocation import QuotaAllocation
    from engram.evals.generation.domain.datasets import EvaluationDataset
    from engram.evals.shared.contracts import DifficultyThresholds

# Keep the shape constants above imported from `seed_shapes`: the seeders and the census SQL read the same values.

# The difficulty order report tuples are emitted in.
DIFFICULTY_ORDER: dict[QaDifficulty, int] = {"easy": 0, "medium": 1, "hard": 2}


class Stratum(BaseModel):
    """One coverage cell: an archetype crossed with a derived difficulty band and an anchor page type.

    `anchor_page_type` is the page type of the seed's anchor: the claim's page for pinpoint, the hub for
    survey, the path start for trace, the fanning page for synthesis and the section's page for passage. Any
    page type the knowledge base's schema declares is valid.
    """

    model_config = ConfigDict(frozen=True)

    archetype: Archetype
    difficulty: QaDifficulty
    anchor_page_type: str

    def __hash__(self) -> int:
        # Pyright needs this explicit hash to accept a stratum as a mapping key or set member.
        return hash((self.archetype, self.difficulty, self.anchor_page_type))


class StratumCount(BaseModel):
    """A stratum with its knowledge base seed-pool size and the dataset cases covering it.

    `available` is what the knowledge base can structurally offer for the stratum. `covered` is how many
    dataset cases actually fall in it, set only by an audit and zero in the inventory census.
    """

    model_config = ConfigDict(frozen=True)

    stratum: Stratum
    available: int
    covered: int = 0


class StratumInventory(BaseModel):
    """The per-stratum seed-pool census for one knowledge base: what it offers, stratum by stratum."""

    model_config = ConfigDict(frozen=True)

    collection: str
    counts: tuple[StratumCount, ...]


def _reachable_shapes(archetype: Archetype, thresholds: DifficultyThresholds) -> tuple[tuple[int, int, int], ...]:
    """The `(n_sources, n_hops, n_sections)` shapes the archetype's seeder can produce, up to the largest threshold plus one."""
    ceiling = (
        max(
            thresholds.single_source,
            thresholds.deep_path_hops,
            thresholds.wide_fanout_sources,
            thresholds.cross_section_sources,
            thresholds.cross_section_sections,
        )
        + 1
    )
    match archetype:
        case Archetype.PINPOINT | Archetype.PASSAGE:
            # Always one source, zero hops, one section.
            return ((1, 0, 1),)
        case Archetype.TRACE:
            # A fixed src -> mid -> dst chain: `TRACE_HOPS` hops over `TRACE_HOPS + 1` distinct nodes.
            return ((TRACE_HOPS + 1, TRACE_HOPS, 1),)
        case Archetype.SURVEY:
            # Hub plus `>= MIN_SURVEY_FANOUT` sources, with sections tracking the gold-source count.
            lo = 1 + MIN_SURVEY_FANOUT
            return tuple((n, 0, n) for n in range(lo, max(lo, ceiling) + 1))
        case Archetype.SYNTHESIS:
            # `>= MIN_SYNTHESIS_FANOUT` reached pages over an independent source-page section count.
            lo = MIN_SYNTHESIS_FANOUT
            return tuple((n, 0, sections) for n in range(lo, max(lo, ceiling) + 1) for sections in range(1, ceiling + 1))
        case _:
            assert_never(archetype)


def feasible_bands(archetype: Archetype, thresholds: DifficultyThresholds) -> frozenset[QaDifficulty]:
    """The difficulty bands an archetype can structurally produce.

    It runs `derive_difficulty` over every shape the archetype's seeder can produce, so the bands always
    agree with the difficulty labels cases carry.

    Args:
        archetype: The structural archetype to characterise.
        thresholds: The tunable difficulty band cutoffs.

    Returns:
        The set of `easy` / `medium` / `hard` bands the archetype can reach.
    """
    return frozenset(
        derive_difficulty(n_sources, n_hops, n_sections, thresholds)
        for n_sources, n_hops, n_sections in _reachable_shapes(archetype, thresholds)
    )


def coverage_debt(counts: Sequence[StratumCount], target: Mapping[Stratum, int]) -> float:
    """The single trendable coverage debt score of a dataset against a per-stratum target.

    Debt is the summed shortfall from target divided by the total target, plus the share of targeted strata
    at zero. Over-representation is not debt. The score is zero exactly when every targeted stratum meets its
    target, and widening any gap can only raise it.

    Args:
        counts: The per-stratum covered counts. A stratum absent here counts as covered zero.
        target: The desired case count per stratum, usually `{cell.stratum: cell.quota}` from an
            allocation. A target of zero or less asks for nothing.

    Returns:
        The coverage debt score, zero at target and growing with under-coverage.
    """
    targeted = {stratum: quota for stratum, quota in target.items() if quota > 0}
    if not targeted:
        return 0.0
    covered_by = {count.stratum: count.covered for count in counts}
    total_target = sum(targeted.values())
    shortfall = sum(max(quota - covered_by.get(stratum, 0), 0) for stratum, quota in targeted.items())
    at_zero = sum(1 for stratum in targeted if covered_by.get(stratum, 0) == 0)
    return shortfall / total_target + at_zero / len(targeted)


class CoverageReport(BaseModel):
    """A dataset's coverage audit against a knowledge base's stratum inventory.

    `infeasible` is the feasibility manifest: the strata whose band the archetype cannot structurally
    produce, so their zero is never read as a gap. Among the feasible strata, `gaps` are under target and
    `over_represented` are over it. `thin` flags feasible strata whose seed pool is non-empty but below the
    `min_pool_warn` floor. `debt` is the `coverage_debt` score. For the inventory census alone
    `dataset_version` is `None` and `version_chain` is empty.
    """

    model_config = ConfigDict(frozen=True)

    collection: str
    inventory: StratumInventory
    gaps: tuple[Stratum, ...]
    over_represented: tuple[Stratum, ...]
    infeasible: tuple[Stratum, ...]
    debt: float
    dataset_version: str | None = None
    version_chain: tuple[str, ...] = ()
    thin: tuple[Stratum, ...] = ()


class AuditedVersion(BaseModel):
    """The audited dataset's identity for a coverage report: its version label plus the walked chain."""

    model_config = ConfigDict(frozen=True)

    dataset_version: str
    version_chain: tuple[str, ...] = ()


def build_coverage_report(
    *,
    inventory: StratumInventory,
    target: Mapping[Stratum, int],
    thresholds: DifficultyThresholds,
    audit: AuditedVersion | None = None,
    min_pool: int = 0,
) -> CoverageReport:
    """Sort the tracked strata into gaps, over-representation and infeasible, flag thin pools and score the debt.

    A stratum whose band is outside `feasible_bands` for its archetype is infeasible whatever its target,
    so a structural zero is never counted as a gap. A feasible stratum is a gap when its target exceeds its
    covered count, over-represented when its covered count exceeds its target, and thin when its seed pool
    is non-empty but below `min_pool`. The tracked strata are the inventory's plus the target's keys, in
    `(archetype, difficulty, anchor page type)` order.

    Args:
        inventory: The per-stratum census, whose `covered` counts drive the audit. The report takes its
            knowledge base from it.
        target: The desired case count per stratum, from a `QuotaAllocation`.
        thresholds: The difficulty band cutoffs feasibility is derived against.
        audit: The audited dataset's version label and walked chain, or `None` for the inventory census alone.
        min_pool: The seed-pool floor below which a non-empty feasible stratum is flagged thin, `0` to turn
            the flag off.

    Returns:
        The frozen `CoverageReport` with the strata sorted, the debt score and the version details.
    """
    covered_by = {count.stratum: count.covered for count in inventory.counts}
    available_by = {count.stratum: count.available for count in inventory.counts}
    tracked = sorted(
        {*covered_by, *target}, key=lambda stratum: (stratum.archetype.value, DIFFICULTY_ORDER[stratum.difficulty], stratum.anchor_page_type)
    )
    gaps: list[Stratum] = []
    over_represented: list[Stratum] = []
    infeasible: list[Stratum] = []
    thin: list[Stratum] = []
    for stratum in tracked:
        if stratum.difficulty not in feasible_bands(stratum.archetype, thresholds):
            infeasible.append(stratum)
            continue
        if 0 < available_by.get(stratum, 0) < min_pool:
            thin.append(stratum)
        covered = covered_by.get(stratum, 0)
        want = target.get(stratum, 0)
        if want > covered:
            gaps.append(stratum)
        elif covered > want:
            over_represented.append(stratum)
    return CoverageReport(
        collection=inventory.collection,
        inventory=inventory,
        gaps=tuple(gaps),
        over_represented=tuple(over_represented),
        infeasible=tuple(infeasible),
        debt=coverage_debt(inventory.counts, target),
        dataset_version=audit.dataset_version if audit is not None else None,
        version_chain=audit.version_chain if audit is not None else (),
        thin=tuple(thin),
    )


def stratum_label(stratum: Stratum) -> str:
    """The stable `archetype/difficulty/anchor-page-type` label of a stratum, such as `survey/hard/hub`.

    The dataset stamp's coverage summary and the coverage report both key on it.

    Args:
        stratum: The stratum to label.

    Returns:
        The `"{archetype}/{difficulty}/{anchor_page_type}"` label.
    """
    return f"{stratum.archetype.value}/{stratum.difficulty}/{stratum.anchor_page_type}"


# The anchor page type an audit gives every band. A saved case records no anchor page type, so an audit
# counts per archetype and difficulty only.
_AUDIT_ANCHOR_KIND = "*"


def covered_by_band(dataset: EvaluationDataset) -> dict[tuple[Archetype, QaDifficulty], int]:
    """Count a dataset's cases per `(archetype, difficulty)` band from each case's own metadata.

    It never reads a stamp's planned coverage. A case with no metadata or no archetype is skipped.

    Args:
        dataset: The audited evaluation dataset.

    Returns:
        The covered case count per `(archetype, difficulty)` band.
    """
    covered: dict[tuple[Archetype, QaDifficulty], int] = {}
    for case in dataset.cases:
        meta = case.metadata
        if meta is None or meta.archetype is None or meta.difficulty is None:
            continue
        key = (meta.archetype, meta.difficulty)
        covered[key] = covered.get(key, 0) + 1
    return covered


def band_inventory(inventory: StratumInventory, covered: Mapping[tuple[Archetype, QaDifficulty], int]) -> StratumInventory:
    """Collapse a full-stratum inventory to `(archetype, difficulty)` bands and overlay covered counts.

    Each band's seed pool is summed over its anchor page types and its `covered` comes from the dataset
    count. A band found only in the dataset is kept with a zero pool. Every band's anchor page type is `*`.

    Args:
        inventory: The full per-anchor-page-type census from the reader.
        covered: The dataset recount per `(archetype, difficulty)` band.

    Returns:
        The band-collapsed inventory carrying the covered counts, ordered deterministically.
    """
    available: dict[tuple[Archetype, QaDifficulty], int] = {}
    for count in inventory.counts:
        key = (count.stratum.archetype, count.stratum.difficulty)
        available[key] = available.get(key, 0) + count.available
    counts = tuple(
        StratumCount(
            stratum=Stratum(archetype=archetype, difficulty=difficulty, anchor_page_type=_AUDIT_ANCHOR_KIND),
            available=available.get((archetype, difficulty), 0),
            covered=covered.get((archetype, difficulty), 0),
        )
        for archetype, difficulty in sorted({*available, *covered}, key=lambda band: (band[0].value, band[1]))
    )
    return StratumInventory(collection=inventory.collection, counts=counts)


def band_target(allocation: QuotaAllocation) -> dict[Stratum, int]:
    """Collapse a full-stratum allocation target to `(archetype, difficulty)` bands, each with anchor page type `*`.

    Args:
        allocation: The allocation to collapse.

    Returns:
        The summed quota per band.
    """
    quotas: dict[tuple[Archetype, QaDifficulty], int] = {}
    for cell in allocation.cells:
        key = (cell.stratum.archetype, cell.stratum.difficulty)
        quotas[key] = quotas.get(key, 0) + cell.quota
    return {
        Stratum(archetype=archetype, difficulty=difficulty, anchor_page_type=_AUDIT_ANCHOR_KIND): quota
        for (archetype, difficulty), quota in quotas.items()
    }


def render_coverage_markdown(report: CoverageReport) -> str:
    """Render a `CoverageReport` as a deterministic markdown summary, the `.coverage.md` sibling.

    The text is ASCII only and identical for identical reports.

    Args:
        report: The coverage report to render.

    Returns:
        The markdown text.
    """
    mode = f"version {report.dataset_version}" if report.dataset_version is not None else "inventory-only (no dataset)"
    lines = [f"# Coverage report: {report.collection}", "", f"**Mode:** {mode}", f"**Coverage debt:** {report.debt:.3f}"]
    if report.version_chain:
        lines.append(f"**Version chain:** {' -> '.join(report.version_chain)}")
    lines += ["", "## Gaps (feasible, under target)", "", *_labelled_block(report.gaps)]
    lines += ["", "## Over-representation", "", *_labelled_block(report.over_represented)]
    lines += ["", "## Infeasible strata (manifest)", "", *_labelled_block(report.infeasible)]
    lines += ["", "## Thin seed pools (below min_pool_warn)", "", *_labelled_block(report.thin)]
    lines += ["", "## Stratum inventory", "", "| stratum | available | covered |", "|---|---|---|"]
    lines += [f"| {stratum_label(count.stratum)} | {count.available} | {count.covered} |" for count in report.inventory.counts]
    return "\n".join(lines) + "\n"


def _labelled_block(strata: tuple[Stratum, ...]) -> list[str]:
    """Render a tuple of strata as a markdown bullet list, or a single `(none)` line when empty."""
    if not strata:
        return ["(none)"]
    return [f"- {stratum_label(stratum)}" for stratum in strata]
