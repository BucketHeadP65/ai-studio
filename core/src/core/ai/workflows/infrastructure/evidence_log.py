"""The evidence log as one append-only JSONL file, with no database and no dependencies.

Lines are only ever added, never edited. A line that no longer parses is skipped and the rest of the history
is still read. Every read scans the whole file, so a lookup can match a workflow's content hash rather than the
library id a line carries.
"""

from __future__ import annotations

import json

from typing import TYPE_CHECKING

from pydantic import TypeAdapter, ValidationError

from core.ai.workflows.domain.evidence import Approval, EvidenceHistory, Grade, RunEvidence, require_spec_identity

if TYPE_CHECKING:
    from collections.abc import Iterator
    from pathlib import Path

_EVENT: TypeAdapter[RunEvidence | Approval | Grade] = TypeAdapter(RunEvidence | Approval | Grade)


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

    async def history_for(self, *, spec_hash: str, workflow_id: str | None = None) -> EvidenceHistory:
        """One workflow's history by content hash, plus the events with no hash filed under `workflow_id`, kept apart.

        The matched events are found wherever the workflow was filed. An event carrying a different
        hash is in neither part.

        Args:
            spec_hash: The workflow's content hash. It must not be empty.
            workflow_id: The library id to search for events written without a hash. None finds none.

        Returns:
            The events carrying `spec_hash` as `matched`, and the unhashed events filed under
            `workflow_id` as `legacy`, each oldest first.

        Raises:
            ValueError: If `spec_hash` is empty.
        """
        require_spec_identity(spec_hash)
        matched: list[RunEvidence | Approval | Grade] = []
        legacy: list[RunEvidence | Approval | Grade] = []
        for event in self._surviving():
            if event.spec_hash == spec_hash:
                matched.append(event)
            elif not event.spec_hash and event.workflow_id == workflow_id:
                legacy.append(event)
        return EvidenceHistory(matched=tuple(matched), legacy=tuple(legacy))

    def _surviving(self) -> Iterator[RunEvidence | Approval | Grade]:
        """Every line that still parses, in the order it was written. A missing file holds nothing."""
        try:
            lines = self._path.read_text(encoding="utf-8").splitlines()
        except FileNotFoundError:
            return
        for line in lines:
            if not line.strip():
                continue
            try:
                yield _EVENT.validate_json(line)
            except (ValidationError, json.JSONDecodeError, ValueError):
                continue  # one bad line never hides the rest of the log
