from __future__ import annotations

from datetime import UTC, datetime, timedelta
from email.utils import format_datetime
from typing import TYPE_CHECKING

import pytest

from engram.config import KnowledgeBaseSettings, SourceResources
from engram.sources.contracts import ScanError, Scanner, WatchEntry, WatchListFile, known_scanners
from engram.sources.infrastructure.xml_scanner import XML_SCANNER, XmlScanner
from engram.testing.fakes.watch_list import FakePageFetcher, FakeScanner
from engram.testing.specs import minimal_spec

if TYPE_CHECKING:
    from pathlib import Path

_NOW = datetime(2026, 8, 25, 9, 0, tzinfo=UTC)
_PODCAST = "podcast"
"""A scanner name this suite registers through the vocabulary's injectable seam, standing in for an installed package's own."""


class _PodcastScanner:
    """A stand-in `ScannerClass`, distinct by identity so a resolved name can be asserted on."""

    @classmethod
    def build(cls, resources: SourceResources) -> Scanner:
        del resources
        return FakeScanner()


def _rss(*items: tuple[str, str, datetime | None]) -> str:
    """A minimal RSS 2.0 document, one `<item>` per `(title, link, published)` triple, in entry order."""
    parts = "".join(f"<item><title>{title}</title><link>{link}</link>{_pubdate(published)}</item>" for title, link, published in items)
    return f'<?xml version="1.0"?><rss version="2.0"><channel>{parts}</channel></rss>'


def _pubdate(published: datetime | None) -> str:
    return f"<pubDate>{format_datetime(published)}</pubDate>" if published is not None else ""


async def test_a_real_rss_feed_with_a_minus_zero_pubdate_scans_against_a_stamp() -> None:
    entry = WatchEntry(scanner=XML_SCANNER, ref="https://blog.example.org/entry.xml")
    xml = (
        '<?xml version="1.0"?><rss version="2.0"><channel>'
        "<item><title>A post</title><link>https://blog.example.org/a</link>"
        "<pubDate>Mon, 25 Aug 2026 08:00:00 -0000</pubDate></item>"
        "</channel></rss>"
    )
    fetcher = FakePageFetcher(pages={entry.ref: xml})

    entries = await XmlScanner(fetcher).scan(entry, since=_NOW - timedelta(hours=3))

    assert [entry.url for entry in entries] == ["https://blog.example.org/a"]


async def test_a_feed_is_fetched_at_its_own_ref() -> None:
    entry = WatchEntry(scanner=XML_SCANNER, ref="https://blog.example.org/entry.xml")
    fetcher = FakePageFetcher(pages={entry.ref: _rss(("A", "https://blog.example.org/a", None))})

    entries = await XmlScanner(fetcher).scan(entry, since=None)

    assert fetcher.calls == [entry.ref]
    assert [e.url for e in entries] == ["https://blog.example.org/a"]


async def test_a_fetch_error_surfaces_as_a_feed_scan_error() -> None:
    entry = WatchEntry(scanner=XML_SCANNER, ref="https://down.example.org/entry.xml")
    fetcher = FakePageFetcher(errors={entry.ref: "connection refused"})

    with pytest.raises(ScanError, match="connection refused"):
        await XmlScanner(fetcher).scan(entry, since=None)


async def test_malformed_feed_xml_surfaces_as_a_feed_scan_error_naming_the_feed() -> None:
    entry = WatchEntry(scanner=XML_SCANNER, ref="https://blog.example.org/entry.xml")
    fetcher = FakePageFetcher(pages={entry.ref: '<rss version="2.0"><channel>'})

    with pytest.raises(ScanError) as excinfo:
        await XmlScanner(fetcher).scan(entry, since=None)

    assert entry.ref in str(excinfo.value)


async def test_a_hand_edited_naive_last_scanned_loads_utc_aware_and_scans_without_raising(tmp_path: Path) -> None:
    # A naive `last_scanned` (no offset) can reach watch-list.yaml by hand-editing rather than through
    # `stamp()`, which always writes an aware value. WatchEntry must still load it UTC-aware, since
    # XmlScanner compares it against FoundSource.published, which is always aware, and a naive
    # vs. aware comparison raises TypeError.
    feeds_path = tmp_path / "watch-list.yaml"
    feeds_path.write_text(
        "- scanner: xml\n  ref: https://blog.example.org/entry.xml\n  max_items: 15\n  last_scanned: '2026-08-25T06:00:00'\n",
        encoding="utf-8",
    )

    entry = WatchListFile(feeds_path).entries()[0]

    assert entry.last_scanned == datetime(2026, 8, 25, 6, 0, tzinfo=UTC)
    xml = _rss(("new", "https://blog.example.org/new", _NOW - timedelta(hours=1)))
    fetcher = FakePageFetcher(pages={entry.ref: xml})

    entries = await XmlScanner(fetcher).scan(entry, since=entry.last_scanned)

    assert [entry.url for entry in entries] == ["https://blog.example.org/new"]


async def test_max_items_caps_from_the_head_before_the_since_filter() -> None:
    since = _NOW - timedelta(hours=3)
    entry = WatchEntry(scanner=XML_SCANNER, ref="https://blog.example.org/entry.xml", max_items=2)
    xml = _rss(
        ("old-1", "https://blog.example.org/1", _NOW - timedelta(hours=10)),
        ("old-2", "https://blog.example.org/2", _NOW - timedelta(hours=9)),
        ("new-3", "https://blog.example.org/3", _NOW - timedelta(hours=1)),
        ("new-4", "https://blog.example.org/4", _NOW - timedelta(minutes=30)),
    )
    fetcher = FakePageFetcher(pages={entry.ref: xml})

    entries = await XmlScanner(fetcher).scan(entry, since=since)

    assert entries == []


def test_known_scanners_is_the_built_in_name_plus_every_registered_one() -> None:
    assert known_scanners(source={_PODCAST: _PodcastScanner}) == (_PODCAST, XML_SCANNER)


def test_known_scanners_with_nothing_registered_is_the_built_in_name_alone() -> None:
    assert known_scanners(source={}) == (XML_SCANNER,)


def test_the_built_in_scanner_builds_from_the_resources_bundle_it_needs_nothing_from(tmp_path: Path) -> None:
    (tmp_path / "knowledge-base-schema.json").write_text(minimal_spec().model_dump_json(), encoding="utf-8")

    scanner = XmlScanner.build(SourceResources(knowledge_base=KnowledgeBaseSettings(path=tmp_path)))

    assert isinstance(scanner, XmlScanner)
