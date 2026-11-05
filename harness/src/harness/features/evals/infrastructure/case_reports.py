"""Case reports as JSON files, one per step and cases content address, each written once."""

from __future__ import annotations

from contextlib import suppress
from typing import TYPE_CHECKING

from core.files import atomic_create

from harness.domain.contracts import CaseReport

if TYPE_CHECKING:
    from pathlib import Path


class FileCaseReports:
    """`CaseReports` over one folder, each report at `<step hash>-<cases hash>.json`."""

    def __init__(self, root: Path) -> None:
        """Keep reports in `root`.

        Args:
            root: The folder the reports live in. It is created on the first `put`.
        """
        self._root = root

    def get(self, step_hash: str, cases_hash: str) -> CaseReport | None:
        """The report kept for this step and these cases, read from its file.

        Args:
            step_hash: The step's content address.
            cases_hash: The content address of the step's dataset.

        Returns:
            The kept report, or `None` when no file holds one.
        """
        path = self._path(step_hash, cases_hash)
        if not path.is_file():
            return None
        return CaseReport.model_validate_json(path.read_bytes())

    def put(self, report: CaseReport) -> None:
        """Write `report` to its file, leaving a report already kept there as it is.

        Args:
            report: The report to keep.
        """
        self._root.mkdir(parents=True, exist_ok=True)
        with suppress(FileExistsError):
            atomic_create(self._path(report.step_hash, report.cases_hash), report.model_dump_json(indent=2).encode("utf-8"))

    def _path(self, step_hash: str, cases_hash: str) -> Path:
        """The file one report lives in."""
        return self._root / f"{step_hash}-{cases_hash}.json"
