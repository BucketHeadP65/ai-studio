r"""The stratum inventory + `ComputeCoverage` audit over a real, migrated DB.

Exercises two things against a freshly-migrated database in the shared container (the `migrated`
fixture), connecting as the owner/primary `memory` role (the seeder / reader read `memory.*` directly,
which 0003 walls the `kb_reader` role off):

* :class:`~engram.evals.generation.infrastructure.stratum_inventory.StratumInventoryQueries` -- the aggregate per-stratum
  census. Hand-builds a knowledge base with one clean instance of every archetype (isolated subgraphs so each
  cell's count is an exact, non-brittle ground truth) and asserts the non-pinpoint cells (survey /
  trace / synthesis) are non-empty and land in the expected `(difficulty, anchor-kind)`
  stratum.
* :class:`~engram.evals.handlers.ComputeCoverageHandler` -- the read-only `evals coverage` seam,
  end to end. It censuses the real knowledge base through the per-command reader factory, audits a
  hand-built golden dataset against it, and writes the CoverageReport artifact, whose band-collapsed
  covered counts must match the dataset's construction.

The knowledge base is inserted directly over the admin session (the sanctioned fallback, mirroring
`test_seed_repository.py`), so each cell's count is asserted against a fully-known graph shape. The
whole module skips when Docker is unavailable (the `pg` fixture, via `migrated`).
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from engram.evals.commands import ComputeCoverage
from engram.evals.generation.contracts import (
    AuthoredCase,
    DatasetStamp,
    GenerationConfig,
    Stratum,
    build_dataset,
    dataset_version_path,
    save_dataset_version,
)
from engram.evals.generation.infrastructure.files import load_coverage_report
from engram.evals.generation.infrastructure.stratum_inventory import StratumInventoryQueries
from engram.evals.handlers import ComputeCoverageHandler
from engram.evals.shared.domain import DEFAULT_DIFFICULTY_THRESHOLDS, Archetype, ExpectedQa, QaCaseMeta, QaDifficulty, QaInput
from engram.indexing.contracts import ClaimORM, EdgeORM, PageORM
from engram.testing.specs import research_spec
from sqlalchemy import NullPool
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine

from studio.knowledge_base.wiring.evals import inventory_reader_factory_provider

if TYPE_CHECKING:
    from collections.abc import AsyncIterator
    from pathlib import Path

    from engram.evals.generation.contracts import StratumInventory
    from sqlalchemy.ext.asyncio import AsyncEngine

_COLLECTION = "research"
_OTHER_COLLECTION = "cooking"


@pytest.fixture
async def engine(migrated: str) -> AsyncIterator[AsyncEngine]:
    """A throwaway admin engine over the freshly-migrated DB (owner role, `NullPool`)."""
    eng = create_async_engine(migrated, poolclass=NullPool)
    try:
        yield eng
    finally:
        await eng.dispose()


def _page(page_id: str, kind: str, *, collection: str = _COLLECTION) -> PageORM:
    """A minimal `kb.pages` row (body_hash derived from the id)."""
    return PageORM(
        id=page_id,
        collection=collection,
        page_type=kind,
        slug=page_id.split(".", 1)[-1],
        title=page_id,
        status="synthesized",
        card_text=f"card for {page_id}",
        body_hash=f"hash-{page_id}",
    )


def _claim(page_id: str, n: int = 1, *, collection: str = _COLLECTION) -> ClaimORM:
    """A minimal `kb.claims` row under `page_id`."""
    return ClaimORM(
        id=f"{page_id}#c{n}",
        collection=collection,
        page_id=page_id,
        block_id=f"c{n}",
        text=f"claim {n} on {page_id}",
        quote=f"verbatim quote {n}",
        loc="§1",
        kind="observation",
        subject_id=None,
        subject_collection=None,
        status="active",
    )


def _edge(src_id: str, rel: str, dst_id: str, *, src_collection: str = _COLLECTION, dst_collection: str = _COLLECTION) -> EdgeORM:
    """A minimal resolved `kb.edges` row."""
    return EdgeORM(
        src_collection=src_collection, src_id=src_id, src_kind="page", rel=rel, dst_collection=dst_collection, dst_id=dst_id, dst_kind="page"
    )


async def _seed_knowledge_base(session: AsyncSession) -> None:
    """Insert one clean, isolated instance of every archetype so each cell's count is exact.

    The subgraphs are deliberately disjoint so the aggregate census never cross-counts:

    * pinpoint: `p1`, `p2` each carry one claim -> two pinpoint/easy/paper seeds. An off-collection
      `p_other` claim must never leak into the `research` count.
    * survey: `hub` has exactly three incoming `builds_on` edges (from `s1`/`s2`/`s3`) -> one
      survey/hard/paper hub. `s1`/`s2`/`s3` fan out to a single target, below the synthesis floor.
    * trace: a clean two-hop `builds_on` chain `t1 -> t2 -> t3` -> one trace/hard/paper path. No node
      here reaches the survey floor or the synthesis fan-out floor.
    * synthesis: `c1` (kind=concept) `synthesizes` `d1` and `d2` over one relation, with no sections
      -> one synthesis/medium/concept seed.
    """
    session.add_all(
        [
            _page("paper.p1", "paper"),
            _page("paper.p2", "paper"),
            _page("paper.hub", "paper"),
            _page("paper.s1", "paper"),
            _page("paper.s2", "paper"),
            _page("paper.s3", "paper"),
            _page("paper.t1", "paper"),
            _page("paper.t2", "paper"),
            _page("paper.t3", "paper"),
            _page("con.c1", "concept"),
            _page("paper.d1", "paper"),
            _page("paper.d2", "paper"),
            _page("paper.other", "paper", collection=_OTHER_COLLECTION),
        ]
    )
    await session.flush()  # flush pages before the FK-bearing children
    session.add_all([_claim("paper.p1"), _claim("paper.p2"), _claim("paper.other", collection=_OTHER_COLLECTION)])
    session.add_all(
        [
            # survey: three distinct contributors fan into the hub
            _edge("paper.s1", "builds_on", "paper.hub"),
            _edge("paper.s2", "builds_on", "paper.hub"),
            _edge("paper.s3", "builds_on", "paper.hub"),
            # trace: a two-hop builds_on chain
            _edge("paper.t1", "builds_on", "paper.t2"),
            _edge("paper.t2", "builds_on", "paper.t3"),
            # synthesis: one concept fanning out to two pages over one relation
            _edge("con.c1", "synthesizes", "paper.d1"),
            _edge("con.c1", "synthesizes", "paper.d2"),
        ]
    )
    await session.commit()


@pytest.fixture
async def seeded(engine: AsyncEngine) -> AsyncEngine:
    """The migrated engine after the hand-built knowledge base is inserted."""
    async with AsyncSession(engine, expire_on_commit=False) as session:
        await _seed_knowledge_base(session)
    return engine


def _stratum(archetype: Archetype, difficulty: QaDifficulty, anchor_page_type: str) -> Stratum:
    return Stratum(archetype=archetype, difficulty=difficulty, anchor_page_type=anchor_page_type)


async def _inventory(engine: AsyncEngine) -> StratumInventory:
    """Census the seeded knowledge base per stratum through a real `StratumInventoryQueries`."""
    async with AsyncSession(engine, expire_on_commit=False) as session:
        reader = StratumInventoryQueries(session, research_spec(), DEFAULT_DIFFICULTY_THRESHOLDS)
        return await reader.inventory(collection=_COLLECTION)


class TestStratumInventoryReader:
    async def test_census_matches_the_constructed_ground_truth(self, seeded: AsyncEngine):
        inventory = await _inventory(seeded)

        available = {count.stratum: count.available for count in inventory.counts}
        # The isolated subgraphs give an exact per-cell census.
        assert available[_stratum(Archetype.PINPOINT, "easy", "paper")] == 2
        assert available[_stratum(Archetype.SURVEY, "hard", "paper")] == 1
        assert available[_stratum(Archetype.TRACE, "hard", "paper")] == 1
        assert available[_stratum(Archetype.SYNTHESIS, "medium", "concept")] == 1

    async def test_non_pinpoint_cells_are_non_empty(self, seeded: AsyncEngine):
        # Survey, trace and synthesis must all draw real seeds, not just pinpoint.
        inventory = await _inventory(seeded)
        drawn = {count.stratum.archetype for count in inventory.counts if count.available > 0}

        assert {Archetype.SYNTHESIS, Archetype.SURVEY, Archetype.TRACE} <= drawn

    async def test_off_collection_claim_never_leaks_into_the_count(self, seeded: AsyncEngine):
        # paper.other (collection=cooking) carries a claim, but only p1/p2 count under research.
        inventory = await _inventory(seeded)

        available = {count.stratum: count.available for count in inventory.counts}
        assert available[_stratum(Archetype.PINPOINT, "easy", "paper")] == 2


def _config() -> GenerationConfig:
    """A balanced four-archetype config, a 20-case budget the target allocation spreads over the knowledge base."""
    return GenerationConfig(
        target_distribution={
            Archetype.PINPOINT: 0.5,
            Archetype.SURVEY: 0.2,
            Archetype.TRACE: 0.1,
            Archetype.SYNTHESIS: 0.2,
        },
        total_cases=20,
    )


def _labelled_dataset_path(datasets_dir: Path) -> Path:
    """Write a stamped `research.v1.yaml` covering two pinpoint-easy and one survey-hard case."""
    cases = [
        AuthoredCase(
            question=QaInput(question="pp-1"),
            expected=ExpectedQa(answer="a", expected_source_ids=("paper.p1",)),
            meta=QaCaseMeta(anchor_page_id="paper.p1", difficulty="easy", archetype=Archetype.PINPOINT),
        ),
        AuthoredCase(
            question=QaInput(question="pp-2"),
            expected=ExpectedQa(answer="a", expected_source_ids=("paper.p2",)),
            meta=QaCaseMeta(anchor_page_id="paper.p2", difficulty="easy", archetype=Archetype.PINPOINT),
        ),
        AuthoredCase(
            question=QaInput(question="sv-1"),
            expected=ExpectedQa(answer="a", expected_source_ids=("paper.hub",)),
            meta=QaCaseMeta(anchor_page_id="paper.hub", difficulty="hard", archetype=Archetype.SURVEY),
        ),
    ]
    path = dataset_version_path(datasets_dir, _COLLECTION, 1)
    stamp = DatasetStamp(salt="s", knowledge_base_fingerprint="fp:research", budget=20, parent_version=None, coverage={})
    save_dataset_version(build_dataset(cases), stamp, path)
    return path


class TestComputeCoverageEndToEnd:
    async def test_inventory_only_run_reports_gaps_over_the_real_knowledge_base(self, seeded: AsyncEngine, tmp_path: Path):
        config = _config()
        handler = ComputeCoverageHandler(inventory_reader_factory_provider(seeded, research_spec(), config), config, tmp_path)
        out = tmp_path / "coverage"

        report = await handler(ComputeCoverage(collection=_COLLECTION, out=str(out)))

        # No dataset: every targeted, feasible cell is an uncovered gap, so debt is positive.
        assert report.dataset_version is None
        assert report.gaps
        assert report.debt > 0.0
        # The persisted census carries the full per-anchor-kind inventory of the real knowledge_base.
        persisted = load_coverage_report(out / f"{_COLLECTION}.coverage.yaml")
        assert any(count.stratum.archetype is Archetype.SURVEY for count in persisted.inventory.counts)

    async def test_audit_recounts_the_dataset_against_the_real_inventory(self, seeded: AsyncEngine, tmp_path: Path):
        dataset_path = _labelled_dataset_path(tmp_path)
        config = _config()
        handler = ComputeCoverageHandler(inventory_reader_factory_provider(seeded, research_spec(), config), config, tmp_path)
        out = tmp_path / "coverage"

        report = await handler(ComputeCoverage(collection=_COLLECTION, dataset=str(dataset_path), out=str(out)))

        assert report.dataset_version == "research.v1.yaml"
        assert report.version_chain == ("research.v1.yaml",)
        # The band-collapsed covered counts match the dataset's construction (2 pinpoint-easy, 1 survey-hard).
        persisted = load_coverage_report(out / f"{_COLLECTION}.coverage.yaml")
        covered = {count.stratum: count.covered for count in persisted.inventory.counts}
        assert covered[_stratum(Archetype.PINPOINT, "easy", "*")] == 2
        assert covered[_stratum(Archetype.SURVEY, "hard", "*")] == 1
