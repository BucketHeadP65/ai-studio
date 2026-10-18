r"""The evaluation-run summary repository over a real, migrated ParadeDB.

Exercises :class:`~engram.evals.shared.infrastructure.postgres.PostgresEvaluationRuns` — the thin Postgres summary
persist/read layer — against a freshly-migrated database inside the shared
ParadeDB container (the `migrated` fixture). The eval tables (`kb.evaluation_runs` /
`kb.evaluation_case_results`) exist only after `alembic upgrade head`, so the test runs over
the migrated URL; it connects as the **owner/primary** `memory` role (the `migrated` URL's
admin credentials), never `kb_reader` — 0003 walls the reader off `kb.*` entirely, so
these summaries are written and read solely over the admin connection.

The whole module skips when Docker is unavailable (the `pg` fixture, via `migrated`, raises
``pytest.skip``), matching the sibling integration tests. The bodies are async (``asyncio_mode =
auto``): each opens its own short-lived :class:`~sqlalchemy.ext.asyncio.AsyncSession` over the
migrated engine — there is no `halfvec` column on these tables, so no pgvector codec is needed.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING

import pytest

from pydantic import ValidationError
from sqlalchemy import NullPool
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine

from engram.evals.shared.infrastructure.postgres import PostgresEvaluationRuns
from engram.evals.shared.read_models import EvaluationCaseResultRow, EvaluationRunDetail, EvaluationRunSummary

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

    from sqlalchemy.ext.asyncio import AsyncEngine


@pytest.fixture
async def engine(migrated: str) -> AsyncIterator[AsyncEngine]:
    """A throwaway admin engine over the freshly-migrated DB (owner role, `NullPool`)."""
    eng = create_async_engine(migrated, poolclass=NullPool)
    try:
        yield eng
    finally:
        await eng.dispose()


def _summary(run_id: str, *, started_at: datetime, dataset: str = "research-golden") -> EvaluationRunSummary:
    """A representative completed-run summary keyed by `run_id` / `started_at`."""
    return EvaluationRunSummary(
        run_id=run_id,
        dataset=dataset,
        model_name="gpt-4o",
        variant={"instructions": "baseline", "k": 12},
        metrics={"recall@5": 0.8, "mrr": 0.67},
        started_at=started_at,
        finished_at=started_at,
        logfire_url=f"https://logfire/{run_id}",
    )


def _case(case_name: str) -> EvaluationCaseResultRow:
    """A representative per-case result row."""
    return EvaluationCaseResultRow(case_name=case_name, scores={"recall@5": 1.0, "mrr": 1.0}, n_tool_calls=2, total_tokens=1280, duration_s=1.5)


class TestPostgresEvaluationRuns:
    async def test_add_run_then_list_runs_returns_it(self, engine: AsyncEngine):
        started = datetime(2026, 6, 19, 12, 0, tzinfo=UTC)
        async with AsyncSession(engine, expire_on_commit=False) as session:
            repo = PostgresEvaluationRuns(session)
            await repo.add_run(_summary("run-1", started_at=started), [_case("alpha"), _case("beta")])

            runs = await repo.list_runs()

        assert [r.run_id for r in runs] == ["run-1"]
        listed = runs[0]
        assert listed.dataset == "research-golden"
        assert listed.model_name == "gpt-4o"
        assert listed.variant == {"instructions": "baseline", "k": 12}
        assert listed.metrics == {"recall@5": 0.8, "mrr": 0.67}
        assert listed.started_at == started
        assert listed.logfire_url == "https://logfire/run-1"

    async def test_get_run_returns_summary_with_case_results(self, engine: AsyncEngine):
        started = datetime(2026, 6, 19, 12, 0, tzinfo=UTC)
        async with AsyncSession(engine, expire_on_commit=False) as session:
            repo = PostgresEvaluationRuns(session)
            await repo.add_run(_summary("run-1", started_at=started), [_case("alpha"), _case("beta")])

            detail = await repo.get_run("run-1")

        assert isinstance(detail, EvaluationRunDetail)
        assert detail.run_id == "run-1"
        assert detail.metrics == {"recall@5": 0.8, "mrr": 0.67}
        assert {c.case_name for c in detail.case_results} == {"alpha", "beta"}
        alpha = next(c for c in detail.case_results if c.case_name == "alpha")
        assert alpha.scores == {"recall@5": 1.0, "mrr": 1.0}
        assert alpha.n_tool_calls == 2
        assert alpha.total_tokens == 1280
        assert alpha.duration_s == pytest.approx(1.5)

    async def test_get_run_unknown_returns_none(self, engine: AsyncEngine):
        async with AsyncSession(engine, expire_on_commit=False) as session:
            repo = PostgresEvaluationRuns(session)

            assert await repo.get_run("nope") is None

    async def test_list_runs_orders_newest_first(self, engine: AsyncEngine):
        older = datetime(2026, 6, 18, 9, 0, tzinfo=UTC)
        newer = datetime(2026, 6, 19, 9, 0, tzinfo=UTC)
        async with AsyncSession(engine, expire_on_commit=False) as session:
            repo = PostgresEvaluationRuns(session)
            await repo.add_run(_summary("older", started_at=older), [_case("a")])
            await repo.add_run(_summary("newer", started_at=newer), [_case("b")])

            runs = await repo.list_runs()

        assert [r.run_id for r in runs] == ["newer", "older"]

    async def test_returns_detached_value_objects_not_orm_rows(self, engine: AsyncEngine):
        started = datetime(2026, 6, 19, 12, 0, tzinfo=UTC)
        async with AsyncSession(engine, expire_on_commit=False) as session:
            repo = PostgresEvaluationRuns(session)
            await repo.add_run(_summary("run-1", started_at=started), [_case("alpha")])
            detail = await repo.get_run("run-1")

        # The session is closed; a frozen value object stays fully readable (no detached-instance error).
        assert detail is not None
        assert detail.case_results[0].case_name == "alpha"
        with pytest.raises(ValidationError):
            detail.run_id = "mutated"  # type: ignore[misc]  # pydantic frozen models reject attribute writes at runtime
