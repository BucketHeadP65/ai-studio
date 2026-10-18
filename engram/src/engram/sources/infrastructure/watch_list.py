"""The watch-list file: one row per watch entry in `_meta/watch-list.yaml`, with the last scan stamp."""

from __future__ import annotations

from typing import TYPE_CHECKING

import yaml

from core.files import atomic_write

from engram.sources.domain.watch_list import WatchEntry
from engram.sources.infrastructure.file_lock import flock_lock

if TYPE_CHECKING:
    from datetime import datetime
    from pathlib import Path

    from engram.sources.infrastructure.file_lock import QueueLock


class WatchListFile:
    """Reads and rewrites one knowledge base's watch-list file, idempotent on `(scanner, ref)`.

    Every mutation reads and rewrites the whole file under an exclusive lock on
    `<watch-list dir>/.watch-list.yaml.lock`, so two processes changing the watch list at once
    cannot lose each other's rows. Reading takes no lock: every write lands through a rename, so a
    reader sees the whole old file or the whole new one.
    """

    def __init__(self, path: Path, *, lock: QueueLock = flock_lock) -> None:
        """Store the watch-list file's path and the file lock.

        Args:
            path: The watch-list file.
            lock: How the file is held while it is rewritten.
        """
        self._path = path
        self._lock = lock

    def entries(self) -> list[WatchEntry]:
        """Every entry in file order, or an empty list when the file does not exist yet.

        Returns:
            The entries.
        """
        return self._read()

    def add(self, scanner: str, ref: str, *, max_items: int = 15) -> WatchEntry:
        """Append an entry, or return the existing one for the same scanner and ref unchanged.

        Args:
            scanner: The scanner's registered name.
            ref: What that scanner reads.
            max_items: The cap on items pulled per scan, for an entry this call adds.

        Returns:
            The entry on the watch list.
        """
        with self._lock(self._path, exclusive=True):
            rows = self._read()
            existing = next((row for row in rows if row.scanner == scanner and row.ref == ref), None)
            if existing is not None:
                return existing
            entry = WatchEntry(scanner=scanner, ref=ref, max_items=max_items)
            self._write([*rows, entry])
            return entry

    def remove(self, scanner: str, ref: str) -> bool:
        """Drop an entry.

        Args:
            scanner: The entry's scanner name.
            ref: The entry's reference.

        Returns:
            `False` when the entry was not there.
        """
        with self._lock(self._path, exclusive=True):
            rows = self._read()
            kept = [row for row in rows if not (row.scanner == scanner and row.ref == ref)]
            if len(kept) == len(rows):
                return False
            self._write(kept)
            return True

    def stamp(self, scanner: str, ref: str, when: datetime) -> None:
        """Record `when` as the entry's last successful scan.

        Args:
            scanner: The entry's scanner name.
            ref: The entry's reference.
            when: The scan's time.
        """
        with self._lock(self._path, exclusive=True):
            self._write(
                [row.model_copy(update={"last_scanned": when}) if row.scanner == scanner and row.ref == ref else row for row in self._read()]
            )

    def scanners(self) -> tuple[str, ...]:
        """Every distinct scanner name the file's entries carry, in first-seen order: what a host has to resolve to scan it.

        Returns:
            The scanner names.
        """
        return tuple(dict.fromkeys(entry.scanner for entry in self.entries()))

    def _read(self) -> list[WatchEntry]:
        if not self._path.is_file():
            return []
        raw = yaml.safe_load(self._path.read_text(encoding="utf-8"))
        return [WatchEntry.model_validate(row) for row in raw] if isinstance(raw, list) else []

    def _write(self, rows: list[WatchEntry]) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        atomic_write(self._path, yaml.safe_dump([row.model_dump(mode="json") for row in rows], sort_keys=False, allow_unicode=True))
