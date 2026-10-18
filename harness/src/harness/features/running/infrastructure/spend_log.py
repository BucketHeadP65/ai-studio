"""The spend log over local files: one JSONL file per run, its header first and then one row per model response."""

from __future__ import annotations

import os

from typing import TYPE_CHECKING

from core.observability import get_logger
from pydantic import ValidationError

from harness.features.running.spend import RunSpend, RunSpendHeader, SpendRow, SpendTotals, SpendWindow

if TYPE_CHECKING:
    from collections.abc import Sequence
    from datetime import datetime
    from pathlib import Path

_logger = get_logger(__name__)


def _decode_line(chunk: bytes) -> str:
    """`chunk` decoded on its own, or empty when it is not valid UTF-8, so a corrupted line costs one row."""
    try:
        return chunk.decode("utf-8")
    except UnicodeDecodeError:
        return ""


def _listed(folder: Path) -> list[tuple[int, str]]:
    """Every run file in `folder` as (last write, name), newest written first, read from the listing alone."""
    listed: list[tuple[int, str]] = []
    try:
        with os.scandir(folder) as entries:
            for entry in entries:
                if not entry.name.endswith(".jsonl"):
                    continue
                try:
                    listed.append((entry.stat().st_mtime_ns, entry.name))
                except OSError:
                    continue
    except OSError:
        return []
    listed.sort(reverse=True)
    return listed


class FileSpendLog:
    """`SpendLog` over one JSONL file per run at `<root>/<workflow_id>/<run_id>.jsonl`."""

    def __init__(self, root: Path) -> None:
        """A spend log whose files live under `root`.

        Args:
            root: The folder that holds one folder per workflow.
        """
        self._root = root

    def begin(self, header: RunSpendHeader) -> None:
        """Write `header` as the first line of a new run file, leaving a file that already exists untouched.

        Args:
            header: The run's header, naming its run and its workflow.
        """
        path = self._path(header.workflow_id, header.run_id)
        if path.exists():
            return
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(header.model_dump_json() + "\n", encoding="utf-8")

    def record(self, run_id: str, workflow_id: str, rows: Sequence[SpendRow]) -> None:
        """Append `rows` to the run's file, or drop them with a logged warning when the run has no file.

        Args:
            run_id: The run the rows were spent by.
            workflow_id: The workflow whose folder holds the run's file.
            rows: The rows, one per model response.
        """
        if not rows:
            return
        path = self._path(workflow_id, run_id)
        if not path.exists():
            _logger.warning(
                "{count} spend rows dropped for run {run} of {workflow}: no spend log header was opened for it, so its cost is understated",
                count=len(rows),
                run=run_id,
                workflow=workflow_id,
            )
            return
        with path.open("a", encoding="utf-8") as handle:
            handle.writelines(row.model_dump_json() + "\n" for row in rows)

    def read(self, run_id: str, workflow_id: str) -> RunSpend | None:
        """Fold the run's file in its workflow's folder.

        Args:
            run_id: The run.
            workflow_id: The workflow whose folder holds the run's file.

        Returns:
            The run's spend, or `None` when the file is missing or has no readable header.
        """
        return self._fold(self._path(workflow_id, run_id))

    def find(self, run_id: str) -> RunSpend | None:
        """Fold the run's file, found by searching every workflow's folder.

        Args:
            run_id: The run.

        Returns:
            The run's spend, or `None` when no folder holds a readable file for it.
        """
        path = next(self._root.glob(f"*/{run_id}.jsonl"), None)
        return None if path is None else self._fold(path)

    def for_workflow(self, workflow_id: str, limit: int) -> list[RunSpend]:
        """Fold the most recently written files in `workflow_id`'s folder, chosen from the listing alone.

        Args:
            workflow_id: The workflow.
            limit: The most runs folded.

        Returns:
            The runs that could be read, newest started first.
        """
        folder = self._root / workflow_id
        if not folder.is_dir():
            return []
        return self._folded(folder, [name for _, name in _listed(folder)[:limit]])

    def in_window(self, workflow_id: str, since: datetime, limit: int) -> SpendWindow:
        """Fold the files in `workflow_id`'s folder last modified at or after `since`, chosen from the listing alone.

        Args:
            workflow_id: The workflow.
            since: The start of the window.
            limit: The most runs folded.

        Returns:
            The window's runs that could be read, newest started first, and whether every file inside it was folded.
        """
        folder = self._root / workflow_id
        if not folder.is_dir():
            return SpendWindow(runs=[], complete=True)
        opened = int(since.timestamp() * 1_000_000_000)
        inside = [name for written, name in _listed(folder) if written >= opened]
        return SpendWindow(runs=self._folded(folder, inside[:limit]), complete=len(inside) <= limit)

    def _path(self, workflow_id: str, run_id: str) -> Path:
        return self._root / workflow_id / f"{run_id}.jsonl"

    def _folded(self, folder: Path, names: Sequence[str]) -> list[RunSpend]:
        """`names` in `folder` folded, newest started first, whichever of them could be read."""
        runs = [spend for name in names if (spend := self._fold(folder / name)) is not None]
        runs.sort(key=lambda spend: spend.started_at, reverse=True)
        return runs

    def _fold(self, path: Path) -> RunSpend | None:
        try:
            raw = path.read_bytes()
        except OSError:
            return None
        lines = [_decode_line(chunk) for chunk in raw.splitlines()]
        if not lines:
            return None
        try:
            header = RunSpendHeader.model_validate_json(lines[0])
        except ValidationError:
            return None
        totals = SpendTotals()
        steps: dict[str, SpendTotals] = {}
        for line in lines[1:]:
            try:
                row = SpendRow.model_validate_json(line)
                totals = totals.plus(row)
                steps[row.step] = steps.get(row.step, SpendTotals()).plus(row)
            except (ValidationError, ValueError):
                continue
        return RunSpend(**header.model_dump(), totals=totals, steps=steps)
