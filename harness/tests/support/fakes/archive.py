"""A run archive that holds whatever a test filed, instead of writing files."""

from __future__ import annotations

from typing import TYPE_CHECKING

from tests.support.state import KEPT_DAYS

if TYPE_CHECKING:
    from harness.features.running.domain.ended_run import RunArchiveRecord


class FakeRunArchive:
    """Holds the records a test filed, keyed by run id."""

    def __init__(self, records: list[RunArchiveRecord] | None = None, retention_days: int = KEPT_DAYS) -> None:
        self._records: dict[str, RunArchiveRecord] = {record.run_id: record for record in (records or [])}
        self.retention_days = retention_days

    def put(self, record: RunArchiveRecord) -> None:
        """Keep `record` under its own run id, replacing whatever was filed there."""
        self._records[record.run_id] = record

    def get(self, run_id: str) -> RunArchiveRecord | None:
        """The record filed under `run_id`, or `None` when nothing is."""
        return self._records.get(run_id)

    def list_for(self, workflow_id: str, limit: int) -> list[RunArchiveRecord]:
        """Up to `limit` of `workflow_id`'s records, newest first by `started_at`."""
        matching = [record for record in self._records.values() if record.workflow_id == workflow_id]
        return sorted(matching, key=lambda record: record.started_at, reverse=True)[:limit]

    def recent(self, limit: int) -> list[RunArchiveRecord]:
        """Up to `limit` records across every workflow, newest first by `started_at`."""
        return sorted(self._records.values(), key=lambda record: record.started_at, reverse=True)[:limit]

    def set_pinned(self, run_id: str, *, pinned: bool) -> bool:
        """Set the pin on `run_id`'s record, and say whether one was filed to set it on."""
        record = self._records.get(run_id)
        if record is None:
            return False
        self._records[run_id] = record.model_copy(update={"pinned": pinned})
        return True

    def delete(self, run_id: str) -> bool:
        """Remove `run_id`'s record, and say whether there was one to remove."""
        return self._records.pop(run_id, None) is not None

    def sweep(self, older_than_days: int) -> int:
        """A fake keeps whatever it is given: nothing is ever swept."""
        del older_than_days
        return 0
