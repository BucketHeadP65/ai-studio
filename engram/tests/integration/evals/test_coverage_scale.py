r"""The 5-10k-page scale envelope for the stratified salted-hash draws.

Mechanism exactness is locked by the unit tier; here the draws are exercised against a synthetic
**row-level** knowledge base (`tests/support/synthetic_knowledge_base.py`) at scale on a real, migrated database.
The structural assertions are below; the wall-clock envelope is measured and printed, never
asserted, so there is no flaky time gate:

* **Anti-materialisation.** Every seeder draw's selection statement (`md5(...)` ordering
  under a `LIMIT`) returns rows bounded by the quota, not the knowledge base, and no statement pulls the whole
  seed space into Python. The trace staged draw in particular never enumerates all 2-hop paths.
* **Census exactness.** `StratumInventoryQueries.inventory` recomputes, from aggregate SQL, exactly the
  per-stratum seed pool the builder constructed (`KnowledgeBasePlan.ground_truth`).
* **Determinism + validity at scale.** A salted draw is reproducible (same salt) and reshuffled
  (different salt), and every drawn trace seed is a genuine acyclic 2-hop path in the edge set.

The knowledge base defaults to 5000 pages (completes in a couple of minutes on a laptop, migration included).
Set `WIKI_SCALE_PAGES=10000` to run the larger envelope. The whole module skips when Docker is
unavailable (the `pg` fixture, via `migrated`).
"""

from __future__ import annotations

import os
import sys

from time import perf_counter
from typing import TYPE_CHECKING

import pytest

from sqlalchemy import NullPool
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine

from engram.evals.generation.infrastructure import seeds as sr
from engram.evals.generation.infrastructure.seeds import SeedQueries
from engram.evals.generation.infrastructure.stratum_inventory import StratumInventoryQueries
from engram.evals.shared.domain import DEFAULT_DIFFICULTY_THRESHOLDS
from engram.testing.infrastructure.synthetic_knowledge_base import (
    KnowledgeBasePlan,
    SyntheticKnowledgeBaseSpec,
    build_knowledge_base,
    synthetic_schema,
)

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Sequence

    from sqlalchemy import Result, Select
    from sqlalchemy.ext.asyncio import AsyncEngine

_COLLECTION = "research"

# The knowledge base size: 5000 by default, 10000 behind the env knob (documented in the module docstring).
_N_PAGES = int(os.environ.get("WIKI_SCALE_PAGES", "5000"))

# The per-draw quota the bounds are measured at — tiny relative to any archetype's seed pool, so a
# draw fetching thousands of rows would be materialising the knowledge base rather than sampling it.
_LIMIT = 10

# The largest gold set any single follow-up statement can fetch is bounded by the quota times the
# widest fan-out cap (survey fan-in dominates), plus the staged trace's first-hop-buffer x degree-cap.
_MAX_GOLD = max(16, sr._TRACE_FIRST_HOP_BUFFER * sr._TRACE_DEGREE_CAP)

# The selection statement (the `md5`-ordered `LIMIT` draw) is bounded by the staged trace's widest
# stage; the point-archetype selections return at most `_LIMIT`.
_SELECTION_BOUND = _LIMIT * sr._TRACE_FIRST_HOP_BUFFER * sr._TRACE_DEGREE_CAP


class _Recorded:
    """One executed statement: its rendered SQL and the number of rows it returned to Python."""

    def __init__(self, sql: str, rows: int) -> None:
        self.sql = sql
        self.rows = rows


class _RecordingExecutor:
    """A `SupportsExecute` that delegates to a real session and records rows returned per statement.

    Rows-returned-to-Python is the anti-materialisation metric: the seeders push selection
    into SQL, so a draw should hand Python only the quota's worth of rows regardless of knowledge base size.
    The result is frozen and replayed so counting never consumes it for the caller.
    """

    def __init__(self, session: AsyncSession) -> None:
        self._session = session
        self.calls: list[_Recorded] = []

    async def execute[T: tuple[object, ...]](self, statement: Select[T]) -> Result[T]:
        result = await self._session.execute(statement)
        frozen = result.freeze()
        self.calls.append(_Recorded(sql=str(statement), rows=len(frozen().all())))
        return frozen()

    def reset(self) -> None:
        """Drop the recorded calls so the next draw is measured in isolation."""
        self.calls.clear()


@pytest.fixture
async def engine(migrated: str) -> AsyncIterator[AsyncEngine]:
    """A throwaway admin engine over the freshly-migrated DB (owner role, `NullPool`)."""
    eng = create_async_engine(migrated, poolclass=NullPool)
    try:
        yield eng
    finally:
        await eng.dispose()


@pytest.fixture
async def built(engine: AsyncEngine) -> tuple[AsyncEngine, KnowledgeBasePlan]:
    """Build the synthetic knowledge base once over the migrated engine; yield the engine and its plan."""
    async with AsyncSession(engine, expire_on_commit=False) as session:
        plan = await build_knowledge_base(session, SyntheticKnowledgeBaseSpec(n_pages=_N_PAGES, seed=20260703), collection=_COLLECTION)
    return engine, plan


def _max_rows(calls: Sequence[_Recorded]) -> int:
    """The most rows any single statement returned to Python during a draw."""
    return max((call.rows for call in calls), default=0)


def _selection_rows(calls: Sequence[_Recorded]) -> int:
    """The most rows any `md5`-ordered selection statement returned (the draw itself)."""
    return max((call.rows for call in calls if "md5(" in call.sql.lower()), default=0)


def _assert_bounded(calls: Sequence[_Recorded], *, knowledge_base_metric: int, name: str) -> None:
    """Assert the draw sampled the seed space: selection and every statement bounded by the quota.

    Args:
        calls: The statements recorded during one seeder draw.
        knowledge_base_metric: The size of the space the pre-Phase-5 code would have materialised (claims,
            edges, or 2-hop paths), which the draw must stay well under.
        name: The archetype name, for assertion messages.
    """
    selection = _selection_rows(calls)
    fetched = _max_rows(calls)
    assert selection <= _SELECTION_BOUND, f"{name}: selection returned {selection} rows (> quota bound {_SELECTION_BOUND})"
    assert fetched <= _LIMIT * _MAX_GOLD, f"{name}: a statement returned {fetched} rows (> f(quota) {_LIMIT * _MAX_GOLD})"
    assert fetched < knowledge_base_metric, f"{name}: returned {fetched} rows, not < knowledge base metric {knowledge_base_metric} (materialised the space)"


class TestInventoryCensusAtScale:
    async def test_census_matches_the_constructed_ground_truth(self, built: tuple[AsyncEngine, KnowledgeBasePlan]):
        engine, plan = built
        async with AsyncSession(engine, expire_on_commit=False) as session:
            reader = StratumInventoryQueries(session, synthetic_schema(plan.spec), DEFAULT_DIFFICULTY_THRESHOLDS)
            inventory = await reader.inventory(collection=_COLLECTION)

        actual = {count.stratum: count.available for count in inventory.counts}
        expected = {count.stratum: count.available for count in plan.ground_truth}
        assert actual == expected

    async def test_census_is_computed_server_side_not_materialised(self, built: tuple[AsyncEngine, KnowledgeBasePlan]):
        engine, plan = built
        async with AsyncSession(engine, expire_on_commit=False) as session:
            executor = _RecordingExecutor(session)
            reader = StratumInventoryQueries(executor, synthetic_schema(plan.spec), DEFAULT_DIFFICULTY_THRESHOLDS)
            await reader.inventory(collection=_COLLECTION)

        # Each aggregate returns one row per (kind, band) group — a handful, never O(knowledge base).
        strata = len(plan.ground_truth)
        assert _max_rows(executor.calls) <= strata + 8
        assert _max_rows(executor.calls) < plan.total_pages


class TestDrawsBoundedAndEnvelopeReported:
    async def test_draws_are_bounded_by_quota_and_envelope_is_reported(
        self, built: tuple[AsyncEngine, KnowledgeBasePlan], capsys: pytest.CaptureFixture[str]
    ):
        engine, plan = built
        async with AsyncSession(engine, expire_on_commit=False) as session:
            executor = _RecordingExecutor(session)
            repo = SeedQueries(executor, synthetic_schema(plan.spec), DEFAULT_DIFFICULTY_THRESHOLDS)
            reader = StratumInventoryQueries(executor, synthetic_schema(plan.spec), DEFAULT_DIFFICULTY_THRESHOLDS)
            timings: dict[str, float] = {}
            fetched: dict[str, int] = {}
            selection: dict[str, int] = {}

            metric = {
                "pinpoint": plan.total_claims,
                "survey": plan.total_edges,
                "trace": plan.total_trace_paths,
                "synthesis": plan.total_edges,
                "passage": plan.total_sections,
            }
            seeders = {
                "pinpoint": repo.pinpoint_seeds,
                "survey": repo.survey_seeds,
                "trace": repo.trace_seeds,
                "synthesis": repo.synthesis_seeds,
                "passage": repo.passage_seeds,
            }
            trace_seeds = []
            for name, seeder in seeders.items():
                executor.reset()
                started = perf_counter()
                seeds = await seeder(collection=_COLLECTION, page_ids=(), limit=_LIMIT, salt="scale")
                timings[name] = (perf_counter() - started) * 1000.0
                fetched[name] = _max_rows(executor.calls)
                selection[name] = _selection_rows(executor.calls)
                _assert_bounded(executor.calls, knowledge_base_metric=metric[name], name=name)
                assert seeds, f"{name}: the draw returned no seeds from a {plan.total_pages}-page knowledge_base"
                if name == "trace":
                    trace_seeds = seeds

            executor.reset()
            started = perf_counter()
            await reader.inventory(collection=_COLLECTION)
            timings["inventory"] = (perf_counter() - started) * 1000.0
            fetched["inventory"] = _max_rows(executor.calls)

            # Determinism at scale (spot-checked on pinpoint): same salt reproduces, a fresh salt reshuffles.
            keys_a = [seed.seed_key for seed in await repo.pinpoint_seeds(collection=_COLLECTION, page_ids=(), limit=_LIMIT, salt="sA")]
            keys_a2 = [seed.seed_key for seed in await repo.pinpoint_seeds(collection=_COLLECTION, page_ids=(), limit=_LIMIT, salt="sA")]
            keys_b = [seed.seed_key for seed in await repo.pinpoint_seeds(collection=_COLLECTION, page_ids=(), limit=_LIMIT, salt="sB")]

        # Every drawn trace seed is a genuine acyclic 2-hop path in the constructed edge set.
        for seed in trace_seeds:
            assert seed.n_hops == 2
            assert len(seed.gold_ids) == 3
            assert len(set(seed.gold_ids)) == 3
            src, mid, dst = seed.gold_ids
            assert (src, mid) in plan.directed_edges
            assert (mid, dst) in plan.directed_edges

        assert keys_a == keys_a2, "same salt must reproduce the draw"
        assert keys_a != keys_b, "a fresh salt must reshuffle the draw"

        lines = [
            f"\nSCALE ENVELOPE @ {plan.total_pages} pages ({plan.total_edges} edges, {plan.total_trace_paths} 2-hop paths, {plan.total_claims} claims):"
        ]
        lines += [
            f"  {name:<10} draw {timings[name]:7.1f} ms   max rows {fetched[name]:>4}   selection {selection[name]:>4}   (knowledge base metric {metric[name]})"
            for name in ("pinpoint", "survey", "trace", "synthesis", "passage")
        ]
        lines.append(
            f"  {'inventory':<10} census {timings['inventory']:6.1f} ms   max rows {fetched['inventory']:>4}   (strata {len(plan.ground_truth)})"
        )
        with capsys.disabled():
            sys.stdout.write("\n".join(lines) + "\n")
