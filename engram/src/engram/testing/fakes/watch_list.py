"""Fakes for the watch-list slice: a scriptable scanner and page fetcher."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from engram.sources.contracts import ScanError

if TYPE_CHECKING:
    from datetime import datetime

    from engram.sources.contracts import FoundSource, WatchEntry


@dataclass
class FakeScanner:
    """Answers by watch-entry ref: scripted sources filtered by `since`, a scripted scan error, or any scripted exception.

    `errors` maps a ref to the message of a `ScanError`. `raises` maps a ref to an exception
    object to raise as-is, for the failures a scanner never declared.
    """

    entries: dict[str, list[FoundSource]] = field(default_factory=dict)
    errors: dict[str, str] = field(default_factory=dict)
    raises: dict[str, Exception] = field(default_factory=dict)
    since: list[datetime | None] = field(default_factory=list)

    async def scan(self, entry: WatchEntry, *, since: datetime | None) -> list[FoundSource]:
        """Record `since`, then raise or return what is scripted for the entry's ref.

        Args:
            entry: The watch entry to scan.
            since: Only sources published after this time, or with no publish time, are returned.

        Returns:
            The scripted sources for the ref that pass the `since` filter.

        Raises:
            ScanError: When `errors` holds the ref.
        """
        self.since.append(since)
        if (scripted := self.raises.get(entry.ref)) is not None:
            raise scripted
        if entry.ref in self.errors:
            raise ScanError(self.errors[entry.ref])
        found = self.entries.get(entry.ref, [])
        return [source for source in found if since is None or source.published is None or source.published > since]


@dataclass
class FakePageFetcher:
    """Answers by URL: a scripted page, or a scripted error. Records every URL it was asked to fetch."""

    pages: dict[str, str] = field(default_factory=dict)
    errors: dict[str, str] = field(default_factory=dict)
    calls: list[str] = field(default_factory=list)

    async def fetch(self, url: str) -> str:
        """Record `url` and return its scripted page.

        Args:
            url: The page to fetch.

        Returns:
            The scripted page.

        Raises:
            RuntimeError: When `errors` holds the URL.
        """
        self.calls.append(url)
        if url in self.errors:
            raise RuntimeError(self.errors[url])
        return self.pages[url]
