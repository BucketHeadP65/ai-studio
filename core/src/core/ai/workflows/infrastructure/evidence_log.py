"""The evidence log as one append-only JSONL file, with no database and no dependencies.

Lines are only ever added, never edited. A line that no longer parses is skipped with a warning naming the file,
the line number and why, and the rest of the history is still read. Every read scans the whole file, so a lookup can match a workflow's content hash rather than the
library id a line carries.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from pydantic import TypeAdapter, ValidationError

from core.ai.workflows.domain.evidence import Approval, Grade, RunEvidence, require_spec_identity
from core.observability import get_logger

if TYPE_CHECKING:
    from collections.abc import Iterator
    from pathlib import Path

    from pydantic_core import ErrorDetails

_EVENT: TypeAdapter[RunEvidence | Approval | Grade] = TypeAdapter(RunEvidence | Approval | Grade)
_logger = get_logger(__name__)


class FileEvidenceLog:
    """`EvidenceLog` over one JSONL file."""

    def __init__(self, path: Path) -> None:
        """Keep the log in the JSONL file at `path`.

        Args:
            path: The log file. It and its folder are created on the first append.
        """
        self._path = path

    async def append(self, event: RunEvidence | Approval | Grade) -> None:
        """Write `event` as one new line at the end of the file. Earlier lines are never touched.

        Args:
            event: The run evidence, approval or grade to add.
        """
        self._path.parent.mkdir(parents=True, exist_ok=True)
        with self._path.open("a", encoding="utf-8") as handle:
            handle.write(event.model_dump_json() + "\n")

    async def events_for(self, workflow_id: str) -> tuple[RunEvidence | Approval | Grade, ...]:
        """Every readable event filed under the library id `workflow_id`, in the order it was written.

        An event the same workflow filed under another id is not found. `history_for` finds it by content hash.

        Args:
            workflow_id: The library id the events were filed under.

        Returns:
            The matching events, oldest first.
        """
        return tuple(event for event in self._surviving() if event.workflow_id == workflow_id)

    async def history_for(self, *, spec_hash: str) -> tuple[RunEvidence | Approval | Grade, ...]:
        """One workflow's events by content hash, wherever the workflow was filed.

        Args:
            spec_hash: The workflow's content hash. It must not be empty.

        Returns:
            The events carrying `spec_hash`, oldest first.

        Raises:
            ValueError: If `spec_hash` is empty.
        """
        require_spec_identity(spec_hash)
        return tuple(event for event in self._surviving() if event.spec_hash == spec_hash)

    def _surviving(self) -> Iterator[RunEvidence | Approval | Grade]:
        """Every line that still parses, in the order it was written. A missing file holds nothing."""
        try:
            lines = self._path.read_text(encoding="utf-8").splitlines()
        except FileNotFoundError:
            return
        for number, line in enumerate(lines, start=1):
            if not line.strip():
                continue
            try:
                yield _EVENT.validate_json(line)
            except ValidationError as error:
                _logger.warning(
                    "line {line} of {path} was skipped: {reason}", line=number, path=str(self._path), reason=_why_unreadable(error)
                )


def _why_unreadable(error: ValidationError) -> str:
    """Why a line is no record, told by the record its `kind` names, or by the one it came closest to."""
    by_record: dict[str, list[ErrorDetails]] = {}
    for detail in error.errors():
        if not detail["loc"]:
            return "it is not JSON"
        by_record.setdefault(str(detail["loc"][0]), []).append(detail)
    named = [details for details in by_record.values() if not any(detail["loc"][1:] == ("kind",) for detail in details)]
    closest = min(named or list(by_record.values()), key=len)
    missing = [".".join(str(part) for part in detail["loc"][1:]) for detail in closest if detail["type"] == "missing"]
    if missing:
        return "it has no " + ", ".join(missing)
    first = closest[0]
    field = ".".join(str(part) for part in first["loc"][1:])
    return f"{field}: {first['msg']}" if field else first["msg"]
