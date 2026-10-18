"""A spend ledger that holds whatever a test filed, instead of writing files."""

from __future__ import annotations

from typing import TYPE_CHECKING

from harness.features.running.spend import SpendWindow

if TYPE_CHECKING:
    from collections.abc import Sequence
    from datetime import datetime

    from harness.features.running.spend import RunSpend, RunSpendHeader, SpendRow


class FakeSpendLog:
    """Holds the runs a test filed, keyed by run id."""

    def __init__(self, runs: list[RunSpend] | None = None) -> None:
        self._runs: dict[str, RunSpend] = {run.run_id: run for run in (runs or [])}

    def begin(self, header: RunSpendHeader) -> None:
        """A fake takes whole runs through its constructor; a bare header records nothing."""
        del header

    def record(self, run_id: str, workflow_id: str, rows: Sequence[SpendRow]) -> None:
        """A fake takes whole `RunSpend` rows through its constructor, never appended rows."""
        del run_id, workflow_id, rows

    def read(self, run_id: str, workflow_id: str) -> RunSpend | None:
        """The run's ledger, when it is filed under `workflow_id`."""
        run = self._runs.get(run_id)
        return run if run is not None and run.workflow_id == workflow_id else None

    def find(self, run_id: str) -> RunSpend | None:
        """The run's ledger, found under whichever workflow filed it."""
        return self._runs.get(run_id)

    def for_workflow(self, workflow_id: str, limit: int) -> list[RunSpend]:
        """Up to `limit` of `workflow_id`'s runs, newest started first."""
        matching = [run for run in self._runs.values() if run.workflow_id == workflow_id]
        return sorted(matching, key=lambda run: run.started_at, reverse=True)[:limit]

    def in_window(self, workflow_id: str, since: datetime, limit: int) -> SpendWindow:
        """Every one of `workflow_id`'s filed runs: a fake never truncates a window."""
        del since, limit
        return SpendWindow(runs=self.for_workflow(workflow_id, limit=len(self._runs)), complete=True)
