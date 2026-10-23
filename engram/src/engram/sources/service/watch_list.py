"""The watch-list scan use case: every watch entry asks its scanner what is new since its stamp, and new URLs become queue rows."""

from __future__ import annotations

from typing import TYPE_CHECKING

from core.cqrs.contracts import Command, narrow_command, utcnow

from engram.sources.domain.watch_list import ScanReport, ScanWatchList

if TYPE_CHECKING:
    from collections.abc import Callable, Mapping
    from datetime import datetime

    from core.cqrs.contracts import Event

    from engram.sources.infrastructure.source_queue import SourceQueue
    from engram.sources.infrastructure.watch_list import WatchListFile
    from engram.sources.internal_contracts import Scanner


async def scan_watch_list(watch_list: WatchListFile, scanners: Mapping[str, Scanner], queue: SourceQueue, *, now: datetime) -> ScanReport:
    """Scan every entry, add unseen URLs to the queue, stamp the entries that scanned, report the ones that did not.

    An entry's `scanner` is resolved against `scanners`, the names the host actually built. A name
    nothing there serves is that entry's own reported failure, naming what is available. Whatever
    a scanner raises is that entry's failure alone, so one unscannable entry never aborts the run,
    the entries after it still scan and the sync that follows still starts.

    Args:
        watch_list: The knowledge base's watch-list file.
        scanners: The scanners the host built, by registered name.
        queue: The source queue new URLs land on.
        now: The scan's time, stamped on rows and entries.

    Returns:
        How many sources were found, how many rows were added in all and by each entry, and the entries that failed.
    """
    found = requested = 0
    requested_by_entry: dict[str, int] = {}
    failed: list[tuple[str, str]] = []
    for entry in watch_list.entries():
        scanner = scanners.get(entry.scanner)
        if scanner is None:
            failed.append((entry.label, f"no scanner named {entry.scanner!r} (scanners: {', '.join(sorted(scanners)) or 'none'})"))
            continue
        try:
            sources = await scanner.scan(entry, since=entry.last_scanned)
        except Exception as error:  # noqa: BLE001 (any scanner failure is this entry's reported reason, never the run's end)
            failed.append((entry.label, str(error) or type(error).__name__))
            continue
        found += len(sources)
        added = queue.add_many([source.url for source in sources], found_via=entry.label, found_at=now)
        requested += added
        if added:
            requested_by_entry[entry.label] = requested_by_entry.get(entry.label, 0) + added
        watch_list.stamp(entry.scanner, entry.ref, now)
    return ScanReport(found=found, requested=requested, failed=tuple(failed), requested_by_entry=requested_by_entry)


class ScanWatchListHandler:
    """Handles `ScanWatchList` over the knowledge base's watch-list file, scanner table (by scanner name) and source queue."""

    def __init__(
        self, watch_list: WatchListFile, scanners: Mapping[str, Scanner], queue: SourceQueue, *, clock: Callable[[], datetime] = utcnow
    ) -> None:
        """Store the collaborators and the clock.

        Args:
            watch_list: The knowledge base's watch-list file.
            scanners: The scanners the host built, by registered name.
            queue: The source queue new URLs land on.
            clock: Timezone-aware "now" source for each scan's time.
        """
        self._watch_list = watch_list
        self._scanners = scanners
        self._queue = queue
        self._clock = clock
        self.events: list[Event] = []

    @property
    def served_scanners(self) -> frozenset[str]:
        """The scanner names this handler can scan with. A row naming any other fails at scan time."""
        return frozenset(self._scanners)

    async def __call__(self, command: Command) -> ScanReport:
        """Bus entry point, narrowing `command` to the `ScanWatchList` this handler is registered for, so a misregistration fails loudly.

        Args:
            command: The command to handle, which must be a `ScanWatchList`.

        Returns:
            What `handle` returns.
        """
        command = narrow_command(command, ScanWatchList, handler="ScanWatchListHandler")  # pragma: no cover (the bus routes by type)
        return await self.handle(command)

    async def handle(self, command: ScanWatchList) -> ScanReport:
        """Run one scan.

        Args:
            command: The scan command.

        Returns:
            The scan's report.
        """
        del command
        return await scan_watch_list(self._watch_list, self._scanners, self._queue, now=self._clock())
