"""The registry of projects' own file: one flat JSON line per log entry, appended and never edited."""

from __future__ import annotations

import json

from datetime import UTC, datetime
from typing import TYPE_CHECKING

from pydantic import TypeAdapter, ValidationError

from harness.features.sessions.domain import RegistryRecord

if TYPE_CHECKING:
    from pathlib import Path

    from harness.features.sessions.domain import RegistryEvent

_RECORD = TypeAdapter(RegistryRecord)


def _encode(record: RegistryRecord) -> str:
    """One flat JSON line: `at`, then the log entry's own keys."""
    return json.dumps({"at": record.at.isoformat(), **record.event.model_dump(mode="json")})


def _decode(line: str) -> RegistryRecord | None:
    """The line's log entry, or `None` for a torn tail or a `kind` this reader does not know."""
    try:
        payload = json.loads(line)
    except ValueError:
        return None
    if not isinstance(payload, dict) or "at" not in payload:
        return None
    at = payload.pop("at")
    try:
        return _RECORD.validate_python({"at": at, "event": payload})
    except ValidationError:
        return None


class ProjectsLog:
    """The registry of projects over one file."""

    def __init__(self, path: Path) -> None:
        """Open the registry of projects over one file, which need not exist yet.

        Args:
            path: The file the log entries are appended to.
        """
        self._path = path

    def record(self, event: RegistryEvent) -> None:
        """Append one log entry, stamped with the current second.

        Args:
            event: The log entry.
        """
        self._path.parent.mkdir(parents=True, exist_ok=True)
        record = RegistryRecord(at=datetime.now(tz=UTC).replace(microsecond=0), event=event)
        with self._path.open("a", encoding="utf-8") as handle:
            handle.write(_encode(record) + "\n")

    def events(self) -> tuple[RegistryEvent, ...]:
        """Every readable line's log entry. A torn tail costs one line, never the read.

        Returns:
            The log entries, oldest first, or none when the file cannot be read.
        """
        try:
            text = self._path.read_text(encoding="utf-8")
        except OSError:
            return ()
        return tuple(record.event for line in text.splitlines() if (record := _decode(line)) is not None)
