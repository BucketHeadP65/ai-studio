"""The evaluation run ports over the `kb.evaluation_runs` and `kb.evaluation_case_results` tables.

You must pass the owner session. The reader role has no grant on the `kb` schema.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from sqlalchemy import select

from engram.evals.shared.infrastructure.orm import EvaluationCaseResultORM, EvaluationRunORM
from engram.evals.shared.read_models import EvaluationCaseResultRow, EvaluationRunDetail, EvaluationRunSummary

if TYPE_CHECKING:
    from collections.abc import Sequence

    from sqlalchemy.ext.asyncio import AsyncSession


class PostgresEvaluationRuns:
    """The stored evaluation runs and their reads, over one owner `AsyncSession`.

    `add_run` commits its own transaction.
    """

    def __init__(self, session: AsyncSession) -> None:
        """Hold the owner session every write and read goes through.

        Args:
            session: The owner `AsyncSession`.
        """
        self._session = session

    async def add_run(self, run: EvaluationRunSummary, case_results: Sequence[EvaluationCaseResultRow]) -> None:
        """Insert the run, flush it, insert its case results and commit once.

        Args:
            run: The run summary. Its `run_id` becomes the `evaluation_runs.id` key and every case row's `run_id`.
            case_results: The run's evaluation case results. An empty sequence stores the run alone.
        """
        self._session.add(_to_run_orm(run))
        # The two tables have no ORM relationship, so the run is flushed first to give the case rows their foreign key target.
        await self._session.flush()
        self._session.add_all([_to_case_orm(run.run_id, case) for case in case_results])
        await self._session.commit()

    async def list_runs(self) -> Sequence[EvaluationRunSummary]:
        """Select every run, newest `started_at` first.

        Returns:
            The run summaries.
        """
        result = await self._session.execute(select(EvaluationRunORM).order_by(EvaluationRunORM.started_at.desc()))
        return [_to_summary(row) for row in result.scalars().all()]

    async def get_run(self, run_id: str) -> EvaluationRunDetail | None:
        """Load the run by key and its case results ordered by row id.

        Args:
            run_id: The run's id.

        Returns:
            The run with its case results, or `None` when no run has that id.
        """
        run = await self._session.get(EvaluationRunORM, run_id)
        if run is None:
            return None
        cases = await self._session.execute(
            select(EvaluationCaseResultORM).where(EvaluationCaseResultORM.run_id == run_id).order_by(EvaluationCaseResultORM.id)
        )
        return EvaluationRunDetail(**_to_summary(run).model_dump(), case_results=tuple(_to_case_row(case) for case in cases.scalars().all()))


def _to_run_orm(run: EvaluationRunSummary) -> EvaluationRunORM:
    """The run summary as an ORM row."""
    return EvaluationRunORM(
        id=run.run_id,
        dataset=run.dataset,
        model_name=run.model_name,
        variant=dict(run.variant),
        started_at=run.started_at,
        finished_at=run.finished_at,
        metrics=dict(run.metrics),
        logfire_url=run.logfire_url,
    )


def _to_case_orm(run_id: str, case: EvaluationCaseResultRow) -> EvaluationCaseResultORM:
    """The case result as an ORM row under `run_id`."""
    return EvaluationCaseResultORM(
        run_id=run_id,
        case_name=case.case_name,
        scores=dict(case.scores),
        n_tool_calls=case.n_tool_calls,
        total_tokens=case.total_tokens,
        duration_s=case.duration_s,
    )


def _to_summary(run: EvaluationRunORM) -> EvaluationRunSummary:
    """The ORM row as a run summary."""
    return EvaluationRunSummary(
        run_id=run.id,
        dataset=run.dataset,
        model_name=run.model_name,
        variant=run.variant,
        metrics=run.metrics,
        started_at=run.started_at,
        finished_at=run.finished_at,
        logfire_url=run.logfire_url,
    )


def _to_case_row(case: EvaluationCaseResultORM) -> EvaluationCaseResultRow:
    """The ORM row as an evaluation case result."""
    return EvaluationCaseResultRow(
        case_name=case.case_name,
        scores=case.scores,
        n_tool_calls=case.n_tool_calls,
        total_tokens=case.total_tokens,
        duration_s=case.duration_s,
    )
