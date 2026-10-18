"""The ports evaluation runs are written and read through."""

from __future__ import annotations

from typing import TYPE_CHECKING, Protocol

if TYPE_CHECKING:
    from collections.abc import Sequence

    from engram.evals.shared.read_models import EvaluationCaseResultRow, EvaluationRunDetail, EvaluationRunSummary


class EvaluationRuns(Protocol):
    """The stored evaluation runs, each written once with its evaluation case results."""

    async def add_run(self, run: EvaluationRunSummary, case_results: Sequence[EvaluationCaseResultRow]) -> None:
        """Store a finished run and its evaluation case results in one transaction.

        Args:
            run: The run summary. Its `run_id` identifies the run and every one of its case results.
            case_results: The run's evaluation case results. An empty sequence stores the run alone.
        """
        ...


class EvaluationRunQueries(Protocol):
    """The reads over the stored evaluation runs. They return frozen read models that outlive the session."""

    async def list_runs(self) -> Sequence[EvaluationRunSummary]:
        """Every stored run's summary.

        Returns:
            The summaries, newest `started_at` first.
        """
        ...

    async def get_run(self, run_id: str) -> EvaluationRunDetail | None:
        """One run's summary with its evaluation case results.

        Args:
            run_id: The run's id.

        Returns:
            The run with its case results in the order they were stored, or `None` when no run has that id.
        """
        ...
