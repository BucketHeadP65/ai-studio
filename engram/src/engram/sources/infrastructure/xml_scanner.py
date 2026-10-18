"""The built-in scanner: an Atom or RSS document fetched at the watch entry's own URL."""

from __future__ import annotations

from typing import TYPE_CHECKING, Final
from xml.etree import ElementTree as ET

from core.net import HttpxPageFetcher

from engram.sources.exceptions import ScanError
from engram.sources.infrastructure.atom import parse_atom_or_rss
from engram.sources.registry import available_scanners, installed_scanners

if TYPE_CHECKING:
    from datetime import datetime

    from core.net import PageFetcher

    from engram.config import SourceResources
    from engram.sources.domain.watch_list import FoundSource, WatchEntry
    from engram.sources.internal_contracts import Scanner
    from engram.sources.registry import ScannerSource

XML_SCANNER: Final = "xml"
"""This scanner's registered name, the `scanner` a watch entry carries to be read as an Atom or RSS document at its own URL."""


class XmlScanner:
    """The built-in `xml` scanner: an Atom or RSS document fetched at the watch entry's own URL."""

    def __init__(self, fetcher: PageFetcher) -> None:
        """Store the fetcher.

        Args:
            fetcher: Fetches the document at a watch entry's URL.
        """
        self._fetcher = fetcher

    @classmethod
    def build(cls, resources: SourceResources) -> Scanner:
        """Construct the built-in scanner over the guarded httpx fetcher. It needs none of `resources`.

        Args:
            resources: The resources every scanner is built from, ignored here.

        Returns:
            The scanner.
        """
        del resources
        return cls(HttpxPageFetcher())

    async def scan(self, entry: WatchEntry, *, since: datetime | None) -> list[FoundSource]:
        """Fetch the document at `entry.ref` and keep sources newer than `since`, at most `entry.max_items`.

        Args:
            entry: The watch entry, whose `ref` is the document's URL.
            since: The entry's last scan, or `None` to keep every source.

        Returns:
            The sources found.

        Raises:
            ScanError: The fetch failed, or the document is not well-formed XML.
        """
        try:
            text = await self._fetcher.fetch(entry.ref)
        except Exception as error:
            raise ScanError(str(error)) from error
        try:
            sources = parse_atom_or_rss(text)[: entry.max_items]
        except ET.ParseError as error:
            msg = f"{entry.ref}: malformed XML"
            raise ScanError(msg) from error
        if since is None:
            return sources
        return [source for source in sources if source.published is None or source.published > since]


def known_scanners(*, source: ScannerSource = installed_scanners) -> tuple[str, ...]:
    """Every name a watch entry's `scanner` may carry, sorted: the built-in `xml` plus every registered scanner `source` lists.

    A host validates a hand-written or posted scanner name against it before the name reaches the
    watch-list file.

    Args:
        source: The registered scanners, every installed one by default.

    Returns:
        The names.
    """
    return tuple(sorted({XML_SCANNER, *available_scanners(source=source)}))
