"""The source queue file: requested URLs and their sync status, one YAML list rewritten atomically."""

from __future__ import annotations

from typing import TYPE_CHECKING

import yaml

from core.files import atomic_write

from engram.sources.domain.source_queue import SourceQueueEntry, SourceQueueStatus, classify_url
from engram.sources.infrastructure.file_lock import flock_lock

if TYPE_CHECKING:
    from collections.abc import Iterable, Sequence
    from datetime import datetime
    from pathlib import Path

    from engram.sources.domain.source_queue import SourceQueueDecision, UrlClaimant
    from engram.sources.infrastructure.file_lock import QueueLock


class SourceQueue:
    """Reads and rewrites one knowledge base's `_meta/source-queue.yaml`, idempotent on the row key.

    Every mutation reads and rewrites the whole file under an exclusive lock on
    `<queue dir>/.source-queue.yaml.lock`, so two processes queueing sources at once cannot lose each
    other's rows. Reading takes no lock: every write lands through a rename, so a reader sees the
    whole old file or the whole new one, never half of either. A knowledge base directory nobody
    may write to can still be listed.
    """

    def __init__(self, path: Path, *, connectors: Sequence[UrlClaimant], lock: QueueLock = flock_lock) -> None:
        """Store the queue file's path, the knowledge base's own connectors, and the file lock.

        Args:
            path: The queue file.
            connectors: The knowledge base's connectors, which classify a new URL. It has no
                default: a knowledge base with none passes an empty sequence.
            lock: How the file is held while it is rewritten.
        """
        self._path = path
        self._providers = connectors
        self._lock = lock

    def entries(self) -> list[SourceQueueEntry]:
        """Every row in queue order, or an empty list when the file does not exist yet.

        Returns:
            The rows.
        """
        return self._read()

    def get(self, key: str) -> SourceQueueEntry | None:
        """The row under `key`.

        Args:
            key: The row key.

        Returns:
            The row, or `None`.
        """
        return next((row for row in self.entries() if row.key == key), None)

    def add(self, url: str, *, found_via: str | None = None, found_at: datetime | None = None) -> SourceQueueEntry:
        """Append a `requested` row for `url`, or return the existing row for its key unchanged.

        Args:
            url: The source URL, classified into a source type and a key.
            found_via: Where the URL came from, for a row this call adds.
            found_at: When it was found, for a row this call adds.

        Returns:
            The row on the queue for `url`.
        """
        entry, _ = self.add_once(url, found_via=found_via, found_at=found_at)
        return entry

    def add_once(self, url: str, *, found_via: str | None = None, found_at: datetime | None = None) -> tuple[SourceQueueEntry, bool]:
        """The row for `url` after this call, and whether this call is what put it on the queue.

        Both answers come from the one hold on the lock, so a caller that reports "already
        requested" is reading the same file state it wrote against, never a later one.

        Args:
            url: The source URL, classified into a source type and a key.
            found_via: Where the URL came from, for a row this call adds.
            found_at: When it was found, for a row this call adds.

        Returns:
            The row on the queue for `url`, and `True` when this call appended it.
        """
        source_type, key = classify_url(url, connectors=self._providers)
        with self._lock(self._path, exclusive=True):
            rows = self._read()
            existing = next((row for row in rows if row.key == key), None)
            if existing is not None:
                return existing, False
            entry = SourceQueueEntry(key=key, url=url, source_type=source_type, status=SourceQueueStatus.REQUESTED, found_via=found_via, found_at=found_at)
            self._write([*rows, entry])
            return entry, True

    def add_bounded(
        self, url: str, *, found_via: str, cap: int, reason: str | None = None, found_at: datetime | None = None
    ) -> tuple[SourceQueueEntry | None, bool]:
        """`add_once`, refusing a NEW row once `cap` rows on the queue already name `found_via`.

        A URL already on the queue is answered like `add_once` (its row, `False`) whatever the
        count. Only an appending call is bounded, inside the same one hold.

        Args:
            url: The source URL, classified into a source type and a key.
            found_via: Where the URL came from. The rows counted against `cap` share it.
            cap: How many rows one `found_via` may put on the queue, in total.
            reason: Why the source it was found via builds on it, kept on the row.
            found_at: When it was found, for a row this call adds.

        Returns:
            The row on the queue for `url` and whether this call appended it, or `(None, False)`
            when the cap refused a new row.
        """
        source_type, key = classify_url(url, connectors=self._providers)
        with self._lock(self._path, exclusive=True):
            rows = self._read()
            existing = next((row for row in rows if row.key == key), None)
            if existing is not None:
                return existing, False
            if sum(row.found_via == found_via for row in rows) >= cap:
                return None, False
            entry = SourceQueueEntry(
                key=key, url=url, source_type=source_type, status=SourceQueueStatus.REQUESTED, reason=reason, found_via=found_via, found_at=found_at
            )
            self._write([*rows, entry])
            return entry, True

    def add_many(self, urls: Iterable[str], *, found_via: str | None = None, found_at: datetime | None = None) -> int:
        """Append a `requested` row for every URL whose key is not on the queue, and answer how many were new.

        One read and one write however many URLs arrive, so a caller landing a whole scan
        rewrites the file once and learns what it added without reading it again. A URL repeated
        within `urls` lands once.

        Args:
            urls: The source URLs, each classified into a source type and a key.
            found_via: Where these URLs came from, recorded on every row this call adds.
            found_at: When they were found, recorded on every row this call adds.

        Returns:
            How many rows this call appended.
        """
        classified = [(url, *classify_url(url, connectors=self._providers)) for url in urls]
        with self._lock(self._path, exclusive=True):
            rows = self._read()
            known = {row.key for row in rows}
            fresh: list[SourceQueueEntry] = []
            for url, source_type, key in classified:
                if key in known:
                    continue
                known.add(key)
                fresh.append(
                    SourceQueueEntry(key=key, url=url, source_type=source_type, status=SourceQueueStatus.REQUESTED, found_via=found_via, found_at=found_at)
                )
            if fresh:
                self._write([*rows, *fresh])
            return len(fresh)

    def chain_of(self, key: str) -> tuple[str, ...]:
        """`key` and each queue row that found it, upward, stopping at the first `found_via` that names no row on this queue.

        A hop is a link between two rows. A `found_via` naming a connector's own item key, a
        watch entry label, or the owner is not on this queue, so the row that carries it is the
        chain's root, never counted as a hop away from anything.

        Args:
            key: The row key to start from.

        Returns:
            The keys from `key` up to the chain's root.
        """
        chain: list[str] = [key]
        current = key
        while True:
            row = self.get(current)
            if row is None or row.found_via is None:
                return tuple(chain)
            parent = row.found_via
            if parent in chain or self.get(parent) is None:
                return tuple(chain)
            chain.append(parent)
            current = parent

    def depth_of(self, key: str) -> int:
        """How many queue hops `key` sits from its chain's root.

        Args:
            key: The row key.

        Returns:
            The hop count, 0 for a name that is no row or one found via a name no row answers to.
        """
        return len(self.chain_of(key)) - 1

    def count_of(self, found_via: str) -> int:
        """How many rows on the queue name `found_via`, whatever their status.

        Args:
            found_via: The name to count.

        Returns:
            The row count.
        """
        return sum(1 for row in self.entries() if row.found_via == found_via)

    def add_seen(self, url: str, *, found_via: str, reason: str, found_at: datetime | None = None) -> tuple[SourceQueueEntry, bool]:
        """Record `url` as `seen`, never fetched, with `reason`. A URL already on the queue is answered as it stands.

        Args:
            url: The source URL, classified into a source type and a key.
            found_via: Where the URL came from.
            reason: Why it is held back, kept on the row.
            found_at: When it was found, for a row this call adds.

        Returns:
            The row for `url` and whether this call appended it.
        """
        source_type, key = classify_url(url, connectors=self._providers)
        with self._lock(self._path, exclusive=True):
            rows = self._read()
            existing = next((row for row in rows if row.key == key), None)
            if existing is not None:
                return existing, False
            entry = SourceQueueEntry(
                key=key, url=url, source_type=source_type, status=SourceQueueStatus.SEEN, reason=reason, found_via=found_via, found_at=found_at
            )
            self._write([*rows, entry])
            return entry, True

    def remove(self, url_or_key: str, *, reason: str, found_at: datetime) -> SourceQueueEntry:
        """Mark a row `removed`, appending one when the URL was never requested.

        `removed` is terminal: no decision, retry or sync revives the row. The row carries
        `reason`, so a later request for the same URL answers with it.

        Args:
            url_or_key: The queue key or the raw URL.
            reason: Why the source was removed.
            found_at: When, for a row this call appends.

        Returns:
            The removed row.
        """
        source_type, key = classify_url(url_or_key, connectors=self._providers)
        with self._lock(self._path, exclusive=True):
            rows = self._read()
            existing = next((row for row in rows if row.key in (url_or_key, key)), None)
            if existing is not None:
                removed = existing.model_copy(update={"status": SourceQueueStatus.REMOVED, "reason": reason})
                self._write([removed if candidate.key == existing.key else candidate for candidate in rows])
                return removed
            entry = SourceQueueEntry(
                key=key, url=url_or_key, source_type=source_type, status=SourceQueueStatus.REMOVED, reason=reason, found_via="removal", found_at=found_at
            )
            self._write([*rows, entry])
            return entry

    def decide(self, key: str, action: SourceQueueDecision, *, reason: str | None = None) -> SourceQueueEntry | None:
        """Move `key`'s row on a decision: `fetch` makes a seen, failed or skipped row requested, `drop` makes a requested or seen row skipped.

        A `drop` with no `reason` keeps the row's existing reason. Only a given `reason` replaces it.

        Args:
            key: The row key.
            action: The decision.
            reason: Why the row is dropped.

        Returns:
            The row as it now reads, or `None` when it is absent or its status admits no such move.
        """
        with self._lock(self._path, exclusive=True):
            rows = self._read()
            row = next((candidate for candidate in rows if candidate.key == key), None)
            if row is None:
                return None
            if action == "fetch" and row.status in (SourceQueueStatus.SEEN, SourceQueueStatus.FAILED, SourceQueueStatus.SKIPPED):
                moved = row.model_copy(update={"status": SourceQueueStatus.REQUESTED, "reason": None})
            elif action == "drop" and row.status in (SourceQueueStatus.REQUESTED, SourceQueueStatus.SEEN):
                moved = row.model_copy(update={"status": SourceQueueStatus.SKIPPED, "reason": reason if reason is not None else row.reason})
            else:
                return None
            self._write([moved if candidate.key == key else candidate for candidate in rows])
            return moved

    def mark(self, key: str, status: SourceQueueStatus, *, reason: str | None = None) -> None:
        """Set `key`'s row to `status` and `reason`. It does nothing when the row is not on the queue.

        Args:
            key: The row key.
            status: The new status.
            reason: The new reason, cleared when omitted.
        """
        with self._lock(self._path, exclusive=True):
            rows = self._read()
            self._write([row.model_copy(update={"status": status, "reason": reason}) if row.key == key else row for row in rows])

    def retry(self, key: str) -> SourceQueueEntry | None:
        """Set a `failed` row back to `requested` with no reason.

        Args:
            key: The row key.

        Returns:
            The retried row, or `None` when the row is absent or did not fail.
        """
        with self._lock(self._path, exclusive=True):
            rows = self._read()
            row = next((candidate for candidate in rows if candidate.key == key), None)
            if row is None or row.status is not SourceQueueStatus.FAILED:
                return None
            retried = row.model_copy(update={"status": SourceQueueStatus.REQUESTED, "reason": None})
            self._write([retried if candidate.key == key else candidate for candidate in rows])
            return retried

    def _read(self) -> list[SourceQueueEntry]:
        if not self._path.is_file():
            return []
        raw = yaml.safe_load(self._path.read_text(encoding="utf-8"))
        if not isinstance(raw, list):
            return []
        return [SourceQueueEntry.model_validate(row) for row in raw]

    def _write(self, rows: list[SourceQueueEntry]) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        payload = yaml.safe_dump([row.model_dump(mode="json") for row in rows], sort_keys=False, allow_unicode=True)
        atomic_write(self._path, payload)
