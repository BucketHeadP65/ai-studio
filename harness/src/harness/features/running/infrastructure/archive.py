"""The run archive over local files: one JSON file per ended run under its workflow's own folder, and a small envelope file beside it."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING

from core.files import atomic_write
from pydantic import ValidationError

from harness.features.running.domain.ended_run import RunArchiveRecord

if TYPE_CHECKING:
    from collections.abc import Iterable
    from pathlib import Path


_ENVELOPE_ONLY: dict[str, object] = {"spec": {}, "values": {}, "events": [], "conversations": {}, "reviewed": []}
"""Overridden onto a record for its envelope file: the heavy fields never enter it."""

_ENVELOPE_SUFFIX = ".envelope.json"
"""How an envelope file's name ends. Every listing and sweep reads these files, never a whole record."""


class FileRunArchive:
    """`RunArchive` over one JSON file per run, at `<root>/<workflow_id>/<run_id>.json`, with its envelope at `<run_id>.envelope.json`."""

    def __init__(self, root: Path, *, retention_days: int) -> None:
        """Hold the folder the archive lives in and how long an unpinned record is kept.

        Args:
            root: The folder holding one subfolder per workflow.
            retention_days: How many days the retention sweep keeps an unpinned record.
        """
        self._root = root
        self.retention_days = retention_days

    def put(self, record: RunArchiveRecord) -> None:
        """Write `record` to its own file and its envelope beside it, creating the workflow's folder as needed.

        Args:
            record: The ended run's record.
        """
        path = self._path_for(record.workflow_id, record.run_id)
        path.parent.mkdir(parents=True, exist_ok=True)
        atomic_write(path, record.model_dump_json())
        atomic_write(self._envelope_of(path), record.model_copy(update=_ENVELOPE_ONLY).model_dump_json())

    def get(self, run_id: str) -> RunArchiveRecord | None:
        """The whole record for `run_id`, read from wherever its workflow filed it.

        Args:
            run_id: The run to read.

        Returns:
            The record, or `None` when no file holds it or the file cannot be read.
        """
        path = self._find(run_id)
        return None if path is None else self._read(path)

    def list_for(self, workflow_id: str, limit: int) -> list[RunArchiveRecord]:
        """`workflow_id`'s own folder, newest first, envelope fields only.

        Args:
            workflow_id: The workflow whose runs are listed.
            limit: The most records to return.

        Returns:
            The records with their heavy fields empty, the latest ended first.
        """
        folder = self._root / workflow_id
        if not folder.is_dir():
            return []
        return self._newest(folder.glob(f"*{_ENVELOPE_SUFFIX}"), limit)

    def recent(self, limit: int) -> list[RunArchiveRecord]:
        """Every workflow's folder, newest first, envelope fields only.

        Args:
            limit: The most records to return.

        Returns:
            The records with their heavy fields empty, the latest ended first.
        """
        return self._newest(self._root.glob(f"*/*{_ENVELOPE_SUFFIX}"), limit)

    def set_pinned(self, run_id: str, *, pinned: bool) -> bool:
        """Load the record, set its pin, and write it back.

        Args:
            run_id: The run to pin or unpin.
            pinned: Whether the record survives the retention sweep.

        Returns:
            True when the record was written, False when no readable record exists for `run_id`.
        """
        path = self._find(run_id)
        if path is None:
            return False
        record = self._read(path)
        if record is None:
            return False
        self.put(record.model_copy(update={"pinned": pinned}))
        return True

    def delete(self, run_id: str) -> bool:
        """Remove the files `run_id` is filed under.

        Args:
            run_id: The run to delete.

        Returns:
            Whether a file existed.
        """
        path = self._find(run_id)
        if path is None:
            return False
        self._envelope_of(path).unlink(missing_ok=True)
        path.unlink()
        return True

    def sweep(self, older_than_days: int) -> int:
        """Delete every unpinned record older than `older_than_days`.

        Args:
            older_than_days: How many days after a run ended its record is deleted.

        Returns:
            How many records were deleted.
        """
        cutoff = datetime.now(UTC) - timedelta(days=older_than_days)
        removed = 0
        for envelope in self._root.glob(f"*/*{_ENVELOPE_SUFFIX}"):
            record = self._read(envelope)
            if record is None or record.pinned:
                continue
            if datetime.fromisoformat(record.ended_at) < cutoff:
                envelope.unlink()
                self._path_for(record.workflow_id, record.run_id).unlink(missing_ok=True)
                removed += 1
        return removed

    def _path_for(self, workflow_id: str, run_id: str) -> Path:
        return self._root / workflow_id / f"{run_id}.json"

    def _envelope_of(self, path: Path) -> Path:
        return path.with_name(f"{path.stem}{_ENVELOPE_SUFFIX}")

    def _find(self, run_id: str) -> Path | None:
        return next(self._root.glob(f"*/{run_id}.json"), None)

    def _newest(self, paths: Iterable[Path], limit: int) -> list[RunArchiveRecord]:
        records = [record for path in paths if (record := self._read(path)) is not None]
        records.sort(key=lambda record: record.ended_at, reverse=True)
        return records[:limit]

    def _read(self, path: Path) -> RunArchiveRecord | None:
        """The record at `path`, whole or an envelope, or `None` when the file is missing or unreadable."""
        try:
            return RunArchiveRecord.model_validate_json(path.read_text(encoding="utf-8"))
        except (OSError, ValidationError, ValueError):
            return None
