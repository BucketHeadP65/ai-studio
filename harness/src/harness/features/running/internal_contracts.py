"""The ports the running slice keeps its records through, and the port an entry asks a gate question through."""

from __future__ import annotations

from typing import TYPE_CHECKING, Protocol

if TYPE_CHECKING:
    from collections.abc import Sequence
    from datetime import datetime

    from harness.features.running.domain.ended_run import RunArchiveRecord
    from harness.features.running.spend import RunSpend, RunSpendHeader, SpendRow, SpendWindow


class RunArchiveQueries(Protocol):
    """Reads over the ended runs in the run archive that change nothing."""

    def get(self, run_id: str) -> RunArchiveRecord | None:
        """The whole record for `run_id`.

        Args:
            run_id: The run.

        Returns:
            The record, or `None` when nothing is filed under it.
        """
        ...

    def list_for(self, workflow_id: str, limit: int) -> list[RunArchiveRecord]:
        """Up to `limit` of `workflow_id`'s runs, newest first, with only the envelope fields loaded.

        Args:
            workflow_id: The workflow.
            limit: The most records returned.

        Returns:
            The records, newest first.
        """
        ...

    def recent(self, limit: int) -> list[RunArchiveRecord]:
        """Up to `limit` runs across every workflow, newest first, with only the envelope fields loaded.

        Args:
            limit: The most records returned.

        Returns:
            The records, newest first.
        """
        ...


class RunArchive(RunArchiveQueries, Protocol):
    """Where ended runs rest, and how long they rest there."""

    retention_days: int
    """How long an ended run rests here before a sweep removes it. `0` turns archiving off outright."""

    def put(self, record: RunArchiveRecord) -> None:
        """Write `record`, replacing any earlier record filed under the same run id.

        Args:
            record: The ended run, whole.
        """
        ...

    def set_pinned(self, run_id: str, *, pinned: bool) -> bool:
        """Set `run_id`'s pin. A pinned record is never swept.

        Args:
            run_id: The run.
            pinned: Whether the record is pinned.

        Returns:
            True when a record was there to set it on.
        """
        ...

    def delete(self, run_id: str) -> bool:
        """Remove `run_id` outright.

        Args:
            run_id: The run.

        Returns:
            True when there was a record to remove.
        """
        ...

    def sweep(self, older_than_days: int) -> int:
        """Remove every unpinned record whose run ended more than `older_than_days` ago.

        Args:
            older_than_days: The age in days past which an unpinned record is removed.

        Returns:
            How many records were removed.
        """
        ...


class SpendLogQueries(Protocol):
    """Reads over the spend log that change nothing."""

    def read(self, run_id: str, workflow_id: str) -> RunSpend | None:
        """The run's spend, folded from its header and every row.

        A torn row costs that one row, never the fold.

        Args:
            run_id: The run.
            workflow_id: The workflow the run's spend is filed under.

        Returns:
            The run's spend, or `None` when it has no header.
        """
        ...

    def find(self, run_id: str) -> RunSpend | None:
        """The run's spend, folded, found under whichever workflow filed it.

        Args:
            run_id: The run.

        Returns:
            The run's spend, or `None` when no workflow holds it with a header.
        """
        ...

    def for_workflow(self, workflow_id: str, limit: int) -> list[RunSpend]:
        """Up to `limit` of `workflow_id`'s most recently written runs, folded.

        Args:
            workflow_id: The workflow.
            limit: The most runs folded.

        Returns:
            The runs, newest started first.
        """
        ...

    def in_window(self, workflow_id: str, since: datetime, limit: int) -> SpendWindow:
        """`workflow_id`'s runs whose spend was last written at or after `since`, folded.

        Every run that started at or after `since` is among them, and so is a run that started
        earlier and was written later. When more runs fall inside the window than `limit`, only
        the newest are folded and the window reads incomplete.

        Args:
            workflow_id: The workflow.
            since: The start of the window.
            limit: The most runs folded.

        Returns:
            The window's runs, newest started first, and whether every run inside it was folded.
        """
        ...


class SpendLog(SpendLogQueries, Protocol):
    """Where a run's spend rows rest."""

    def begin(self, header: RunSpendHeader) -> None:
        """Open the run's spend with its header.

        Opening a run that is already open changes nothing.

        Args:
            header: The run's header, naming its run and its workflow.
        """
        ...

    def record(self, run_id: str, workflow_id: str, rows: Sequence[SpendRow]) -> None:
        """Append `rows` to the run's spend.

        Rows for a run that `begin` never opened are dropped, and the loss is reported.

        Args:
            run_id: The run the rows were spent by.
            workflow_id: The workflow the run's spend is filed under.
            rows: The rows, one per model response.
        """
        ...


class GateAsker(Protocol):
    """An entry that can put a gate in front of the person and hear their answer.

    An entry with no decide route must supply one, or its gates only ever time out. The browser
    needs none: its gates wait on the session until `/api/decide` resolves them.
    """

    async def ask(self, gate_id: str, rendered: str) -> bool:
        """Show `rendered` for review at `gate_id` and wait for the person's answer.

        Args:
            gate_id: The gate being decided.
            rendered: The value under review, rendered for a person.

        Returns:
            True to release the value, False to rerun upstream.
        """
        ...
