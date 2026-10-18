"""Unit tests for the pure coverage domain.

These lock down the pure surfaces of `coverage.py`: `feasible_bands` (the difficulty bands
each archetype can structurally produce, derived from `derive_difficulty`'s thresholds),
`coverage_debt` (the single trendable scalar — zero at target, monotone in gaps),
`build_coverage_report` (the gaps / over-representation / infeasibility partition), and the
report serialization helpers (`stratum_label`, the YAML round-trip, and the markdown render).
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from engram.evals.generation.contracts import (
    AuditedVersion,
    AuthoredCase,
    CoverageReport,
    QuotaAllocation,
    Stratum,
    StratumCount,
    StratumInventory,
    band_inventory,
    band_target,
    build_coverage_report,
    build_dataset,
    covered_by_band,
    render_coverage_markdown,
    save_coverage_report,
    stratum_label,
)
from engram.evals.generation.domain.allocation import CellQuota
from engram.evals.generation.domain.coverage import coverage_debt, feasible_bands
from engram.evals.generation.infrastructure.files import load_coverage_report
from engram.evals.shared.domain import (
    DEFAULT_DIFFICULTY_THRESHOLDS,
    Archetype,
    DifficultyThresholds,
    ExpectedQa,
    QaCaseMeta,
    QaDifficulty,
    QaInput,
)

if TYPE_CHECKING:
    from pathlib import Path


def _stratum(archetype: Archetype, difficulty: QaDifficulty, anchor_page_type: str = "page") -> Stratum:
    return Stratum(archetype=archetype, difficulty=difficulty, anchor_page_type=anchor_page_type)


# --- feasible_bands: derived from derive_difficulty's actual thresholds ------------------------


def test_feasible_bands_pinpoint_is_easy_only():
    # A pinpoint seed is always single-source, zero-hop -> derive_difficulty returns "easy".
    assert feasible_bands(Archetype.PINPOINT, DEFAULT_DIFFICULTY_THRESHOLDS) == frozenset({"easy"})


def test_feasible_bands_passage_is_easy_only():
    # A passage seed is one section of one page: one source, zero hops, one section -> "easy".
    assert feasible_bands(Archetype.PASSAGE, DEFAULT_DIFFICULTY_THRESHOLDS) == frozenset({"easy"})


def test_feasible_bands_trace_is_hard_at_default_hops():
    # A trace path is fixed at two hops; default deep_path_hops == 2, so it derives "hard".
    assert feasible_bands(Archetype.TRACE, DEFAULT_DIFFICULTY_THRESHOLDS) == frozenset({"hard"})


def test_feasible_bands_trace_downgrades_when_hop_threshold_raised():
    # Raise deep_path_hops above the trace's fixed two hops: the path is no longer a deep path,
    # its three sources sit below wide_fanout, and one section is below cross_section -> "medium".
    thresholds = DifficultyThresholds(deep_path_hops=3)
    assert feasible_bands(Archetype.TRACE, thresholds) == frozenset({"medium"})


def test_feasible_bands_survey_is_hard():
    # A survey's fan-out puts it at or above wide_fanout_sources (hub + >= 3 contributors == 4).
    assert feasible_bands(Archetype.SURVEY, DEFAULT_DIFFICULTY_THRESHOLDS) == frozenset({"hard"})


def test_feasible_bands_synthesis_spans_medium_and_hard():
    # Synthesis fans out to >= 2 pages over an independent section count: a two-source single-section
    # shape is "medium", while a wider fan-out or a cross-section shape is "hard". Never single-source,
    # so never "easy".
    assert feasible_bands(Archetype.SYNTHESIS, DEFAULT_DIFFICULTY_THRESHOLDS) == frozenset({"medium", "hard"})


def test_feasible_bands_covers_every_archetype():
    # Every archetype yields at least one feasible band (no archetype is structurally impossible).
    for archetype in Archetype:
        assert feasible_bands(archetype, DEFAULT_DIFFICULTY_THRESHOLDS)


# --- coverage_debt: zero at target, monotone in gaps -------------------------------------------


def test_coverage_debt_is_zero_when_target_met():
    a = _stratum(Archetype.SURVEY, "hard", "hub")
    b = _stratum(Archetype.PINPOINT, "easy")
    counts = [StratumCount(stratum=a, available=10, covered=4), StratumCount(stratum=b, available=8, covered=2)]
    target = {a: 4, b: 2}
    assert coverage_debt(counts, target) == 0.0


def test_coverage_debt_is_zero_when_over_represented():
    a = _stratum(Archetype.SURVEY, "hard", "hub")
    counts = [StratumCount(stratum=a, available=10, covered=9)]
    target = {a: 4}
    # Over-representation is not debt (it is reported separately); only shortfall counts.
    assert coverage_debt(counts, target) == 0.0


def test_coverage_debt_empty_target_is_zero():
    a = _stratum(Archetype.SURVEY, "hard", "hub")
    counts = [StratumCount(stratum=a, available=10, covered=0)]
    assert coverage_debt(counts, {}) == 0.0
    # A target of all-zero quotas is likewise empty of obligations.
    assert coverage_debt(counts, {a: 0}) == 0.0


def test_coverage_debt_is_monotone_in_gaps():
    a = _stratum(Archetype.SURVEY, "hard", "hub")
    b = _stratum(Archetype.PINPOINT, "easy")
    target = {a: 4, b: 4}

    def debt(covered_a: int) -> float:
        counts = [StratumCount(stratum=a, available=10, covered=covered_a), StratumCount(stratum=b, available=10, covered=4)]
        return coverage_debt(counts, target)

    # Widening the gap on `a` (3 -> 1 -> 0 covered) strictly increases debt; hitting zero adds the
    # feasible-strata-at-zero penalty on top of the L1 shortfall.
    assert debt(4) == 0.0
    assert debt(3) < debt(1) < debt(0)


def test_coverage_debt_counts_strata_absent_from_counts_as_fully_uncovered():
    a = _stratum(Archetype.SURVEY, "hard", "hub")
    missing = _stratum(Archetype.PINPOINT, "easy")
    counts = [StratumCount(stratum=a, available=10, covered=4)]
    # `missing` is targeted but has no count row -> treated as covered 0, so it drives debt up.
    assert coverage_debt(counts, {a: 4, missing: 4}) > 0.0


# --- build_coverage_report: gaps / over / infeasible partition ---------------------------------


def test_build_report_partitions_gaps_over_and_infeasible():
    gap = _stratum(Archetype.SURVEY, "hard", "hub")  # feasible, target > covered
    over = _stratum(Archetype.PINPOINT, "easy")  # feasible, covered > target
    infeasible = _stratum(Archetype.PINPOINT, "hard")  # pinpoint can only be "easy"
    met = _stratum(Archetype.SYNTHESIS, "medium", "comparison")  # feasible, covered == target
    inventory = StratumInventory(
        collection="research",
        counts=(
            StratumCount(stratum=gap, available=10, covered=1),
            StratumCount(stratum=over, available=5, covered=3),
            StratumCount(stratum=infeasible, available=0, covered=0),
            StratumCount(stratum=met, available=4, covered=2),
        ),
    )
    target = {gap: 4, over: 1, infeasible: 2, met: 2}

    report = build_coverage_report(inventory=inventory, target=target, thresholds=DEFAULT_DIFFICULTY_THRESHOLDS)

    assert isinstance(report, CoverageReport)
    assert report.collection == "research"  # derived from the inventory
    assert set(report.gaps) == {gap}
    assert set(report.over_represented) == {over}
    assert set(report.infeasible) == {infeasible}
    # A met stratum belongs to none of the three tuples.
    assert met not in report.gaps
    assert met not in report.over_represented
    assert met not in report.infeasible
    assert report.debt > 0.0
    assert report.dataset_version is None
    assert report.version_chain == ()


def test_build_report_partitions_are_pairwise_disjoint():
    gap = _stratum(Archetype.SURVEY, "hard", "hub")
    infeasible = _stratum(Archetype.TRACE, "easy")  # trace can never be "easy"
    inventory = StratumInventory(
        collection="research",
        counts=(StratumCount(stratum=gap, available=10, covered=0), StratumCount(stratum=infeasible, available=0, covered=0)),
    )
    report = build_coverage_report(inventory=inventory, target={gap: 4, infeasible: 2}, thresholds=DEFAULT_DIFFICULTY_THRESHOLDS)
    gaps, over, infeasible_set = set(report.gaps), set(report.over_represented), set(report.infeasible)
    assert gaps.isdisjoint(over)
    assert gaps.isdisjoint(infeasible_set)
    assert over.isdisjoint(infeasible_set)
    assert gap in gaps
    assert infeasible in infeasible_set


def test_build_report_flags_thin_pools_below_the_floor():
    thin = _stratum(Archetype.SURVEY, "hard", "hub")  # feasible, 0 < available < min_pool
    healthy = _stratum(Archetype.PINPOINT, "easy")  # feasible, at the floor
    empty = _stratum(Archetype.SYNTHESIS, "medium", "comparison")  # feasible but empty: a gap, never thin
    infeasible = _stratum(Archetype.PINPOINT, "hard")  # infeasible strata are never thin
    inventory = StratumInventory(
        collection="research",
        counts=(
            StratumCount(stratum=thin, available=2, covered=0),
            StratumCount(stratum=healthy, available=3, covered=0),
            StratumCount(stratum=empty, available=0, covered=0),
            StratumCount(stratum=infeasible, available=1, covered=0),
        ),
    )
    target = {thin: 2, healthy: 2, empty: 2, infeasible: 1}

    report = build_coverage_report(inventory=inventory, target=target, thresholds=DEFAULT_DIFFICULTY_THRESHOLDS, min_pool=3)

    assert report.thin == (thin,)
    assert empty in report.gaps  # empty pools stay gaps, not thin
    assert infeasible in report.infeasible


def test_build_report_min_pool_zero_disables_the_thin_flag():
    a = _stratum(Archetype.SURVEY, "hard", "hub")
    inventory = StratumInventory(collection="research", counts=(StratumCount(stratum=a, available=1, covered=0),))

    report = build_coverage_report(inventory=inventory, target={a: 2}, thresholds=DEFAULT_DIFFICULTY_THRESHOLDS)

    assert report.thin == ()


def test_build_report_passes_through_version_metadata():
    a = _stratum(Archetype.SURVEY, "hard", "hub")
    inventory = StratumInventory(collection="research", counts=(StratumCount(stratum=a, available=10, covered=4),))
    report = build_coverage_report(
        inventory=inventory,
        target={a: 4},
        thresholds=DEFAULT_DIFFICULTY_THRESHOLDS,
        audit=AuditedVersion(dataset_version="research.v2", version_chain=("research.v1", "research.v2")),
    )
    assert report.dataset_version == "research.v2"
    assert report.version_chain == ("research.v1", "research.v2")


# --- report serialization helpers ---------------------------------------------------------------


def _audit_report() -> CoverageReport:
    gap = _stratum(Archetype.SURVEY, "hard", "hub")
    over = _stratum(Archetype.PINPOINT, "easy")
    inventory = StratumInventory(
        collection="research",
        counts=(StratumCount(stratum=gap, available=10, covered=1), StratumCount(stratum=over, available=5, covered=4)),
    )
    return build_coverage_report(
        inventory=inventory,
        target={gap: 4, over: 1},
        thresholds=DEFAULT_DIFFICULTY_THRESHOLDS,
        audit=AuditedVersion(dataset_version="research.v2.yaml", version_chain=("research.v1.yaml", "research.v2.yaml")),
    )


def test_stratum_label_is_the_archetype_difficulty_kind_triple():
    assert stratum_label(_stratum(Archetype.SURVEY, "hard", "hub")) == "survey/hard/hub"
    assert stratum_label(_stratum(Archetype.PINPOINT, "easy", "paper")) == "pinpoint/easy/paper"


def test_coverage_report_round_trips_through_yaml(tmp_path: Path):
    report = _audit_report()
    path = tmp_path / "research.coverage.yaml"

    save_coverage_report(report, path)
    reloaded = load_coverage_report(path)

    # The frozen value object survives the JSON-mode dump + re-validation exactly.
    assert reloaded == report
    assert reloaded.dataset_version == "research.v2.yaml"
    assert reloaded.version_chain == ("research.v1.yaml", "research.v2.yaml")
    assert reloaded.inventory.counts[0].covered == report.inventory.counts[0].covered


def test_save_coverage_report_is_deterministic(tmp_path: Path):
    # The report body carries no wall-clock, so two saves of equal reports are byte-identical.
    first, second = tmp_path / "a.yaml", tmp_path / "b.yaml"
    save_coverage_report(_audit_report(), first)
    save_coverage_report(_audit_report(), second)
    assert first.read_bytes() == second.read_bytes()


def test_render_coverage_markdown_lists_gaps_debt_and_chain():
    markdown = render_coverage_markdown(_audit_report())

    assert markdown.startswith("# Coverage report: research")
    assert "**Mode:** version research.v2.yaml" in markdown
    assert "survey/hard/hub" in markdown  # the gap stratum is listed
    assert "**Coverage debt:**" in markdown
    assert "research.v1.yaml -> research.v2.yaml" in markdown  # the version chain
    assert "| stratum | available | covered |" in markdown  # the inventory census table


def test_render_coverage_markdown_inventory_only_mode():
    inventory = StratumInventory(
        collection="research", counts=(StratumCount(stratum=_stratum(Archetype.SURVEY, "hard", "hub"), available=10, covered=0),)
    )
    report = build_coverage_report(inventory=inventory, target={}, thresholds=DEFAULT_DIFFICULTY_THRESHOLDS)

    markdown = render_coverage_markdown(report)

    assert "**Mode:** inventory-only (no dataset)" in markdown
    # No gaps/over/infeasible/thin: each section renders the `(none)` placeholder.
    assert markdown.count("(none)") == 4


def test_render_coverage_markdown_lists_thin_pools():
    a = _stratum(Archetype.SURVEY, "hard", "hub")
    inventory = StratumInventory(collection="research", counts=(StratumCount(stratum=a, available=1, covered=0),))
    report = build_coverage_report(inventory=inventory, target={a: 2}, thresholds=DEFAULT_DIFFICULTY_THRESHOLDS, min_pool=3)

    markdown = render_coverage_markdown(report)

    assert "## Thin seed pools (below min_pool_warn)" in markdown
    assert markdown.count("survey/hard/hub") >= 2  # listed as a gap and as a thin pool


# --- band audit helpers: (archetype x difficulty) collapse for audit mode -----------------------


def _authored(archetype: Archetype | None, difficulty: QaDifficulty) -> AuthoredCase:
    """An authored case carrying the `(archetype, difficulty)` band the audit recount reads from metadata."""
    return AuthoredCase(
        question=QaInput(question="q"),
        expected=ExpectedQa(answer="a", expected_source_ids=("p.x",)),
        meta=QaCaseMeta(anchor_page_id="p", difficulty=difficulty, archetype=archetype),
    )


def test_covered_by_band_recounts_cases_per_archetype_difficulty_band():
    dataset = build_dataset(
        [_authored(Archetype.PINPOINT, "easy"), _authored(Archetype.PINPOINT, "easy"), _authored(Archetype.SURVEY, "hard")]
    )

    assert covered_by_band(dataset) == {(Archetype.PINPOINT, "easy"): 2, (Archetype.SURVEY, "hard"): 1}


def test_covered_by_band_skips_unlabelled_legacy_cases():
    # A case with no archetype has no stratum to attribute it to, so it is not counted.
    assert covered_by_band(build_dataset([_authored(None, "easy")])) == {}


def test_band_inventory_collapses_over_anchor_kind_and_overlays_covered():
    inventory = StratumInventory(
        collection="research",
        counts=(
            StratumCount(stratum=_stratum(Archetype.PINPOINT, "easy", "paper"), available=3),
            StratumCount(stratum=_stratum(Archetype.PINPOINT, "easy", "figure"), available=2),
        ),
    )

    banded = band_inventory(inventory, {(Archetype.PINPOINT, "easy"): 4})

    assert banded.collection == "research"
    # The two anchor kinds fold into one `*` band summing their pools, carrying the dataset recount.
    assert banded.counts == (StratumCount(stratum=_stratum(Archetype.PINPOINT, "easy", "*"), available=5, covered=4),)


def test_band_inventory_keeps_a_covered_only_band_with_a_zero_pool():
    # A band present only in the dataset (knowledge base drift) is kept with a zero seed pool.
    banded = band_inventory(StratumInventory(collection="research", counts=()), {(Archetype.SURVEY, "hard"): 2})

    assert banded.counts == (StratumCount(stratum=_stratum(Archetype.SURVEY, "hard", "*"), available=0, covered=2),)


def test_band_target_collapses_allocation_cells_to_bands():
    allocation = QuotaAllocation(
        cells=(
            CellQuota(stratum=_stratum(Archetype.PINPOINT, "easy", "paper"), quota=3),
            CellQuota(stratum=_stratum(Archetype.PINPOINT, "easy", "figure"), quota=2),
        )
    )

    assert band_target(allocation) == {_stratum(Archetype.PINPOINT, "easy", "*"): 5}


def test_a_human_judged_case_counts_toward_no_band():
    authored = AuthoredCase(
        question=QaInput(question="Q?"), expected=ExpectedQa(answer="", expected_source_ids=("d1",)), meta=QaCaseMeta(judgments={"d1": 1}), case_name="q1"
    )

    assert covered_by_band(build_dataset([authored])) == {}
