r"""Unit specs for the entry-scanner port and one scan (`engram.sources.service.watch_list`).

Every scanner here is a made-up scanner name driven through the port, never the built-in reader's
own: the orchestration knows a `scanner` only as a key into the table the host built for it. The
built-in reader has its own suite, `test_xml.py`.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING

import pytest

from pydantic import ValidationError

from engram.sources.contracts import FoundSource, SourceQueue, WatchEntry, WatchListFile, claimants_for
from engram.sources.service.watch_list import scan_watch_list
from engram.testing.fakes.source_queue import FakeQueueLock
from engram.testing.fakes.watch_list import FakeScanner

if TYPE_CHECKING:
    from pathlib import Path

_NOW = datetime(2026, 8, 25, 9, 0, tzinfo=UTC)
_PROVIDERS = claimants_for(("intake",))
_BLOGS = "blogs"
"""One scanner name a host built a scanner for, as a watch_list file carries it."""
_PODCAST = "podcast"
"""A second one, so a file carrying two kinds and a table serving one are both expressible."""


def _entry(url: str, hours_ago: int) -> FoundSource:
    return FoundSource(url=url, title=url.rsplit("/", 1)[-1], published=_NOW - timedelta(hours=hours_ago))


async def test_a_first_scan_requests_every_entry_and_stamps_the_feed(tmp_path: Path) -> None:
    watch_list = WatchListFile(tmp_path / "watch-list.yaml")
    watch_list.add(_BLOGS, "https://blog.example.org/entry.xml")
    queue = SourceQueue(tmp_path / "source-queue.yaml", connectors=_PROVIDERS)
    scanner = FakeScanner(entries={"https://blog.example.org/entry.xml": [_entry("https://blog.example.org/a", 5), _entry("https://blog.example.org/b", 2)]})

    report = await scan_watch_list(watch_list, {_BLOGS: scanner}, queue, now=_NOW)

    assert (report.found, report.requested, report.failed) == (2, 2, ())
    rows = queue.entries()
    assert [row.url for row in rows] == ["https://blog.example.org/a", "https://blog.example.org/b"]
    assert rows[0].found_via == "blogs:https://blog.example.org/entry.xml"
    assert rows[0].found_at == _NOW
    assert watch_list.entries()[0].last_scanned == _NOW


async def test_a_later_scan_only_requests_entries_newer_than_the_stamp(tmp_path: Path) -> None:
    watch_list = WatchListFile(tmp_path / "watch-list.yaml")
    watch_list.add(_BLOGS, "https://blog.example.org/entry.xml")
    watch_list.stamp(_BLOGS, "https://blog.example.org/entry.xml", _NOW - timedelta(hours=3))
    queue = SourceQueue(tmp_path / "source-queue.yaml", connectors=_PROVIDERS)
    scanner = FakeScanner(entries={"https://blog.example.org/entry.xml": [_entry("https://blog.example.org/old", 5), _entry("https://blog.example.org/new", 1)]})

    report = await scan_watch_list(watch_list, {_BLOGS: scanner}, queue, now=_NOW)

    assert report.requested == 1
    assert [row.url for row in queue.entries()] == ["https://blog.example.org/new"]
    assert scanner.since == [_NOW - timedelta(hours=3)]


async def test_a_feeds_whole_scan_lands_in_one_write_and_counts_only_the_new_rows(tmp_path: Path) -> None:
    watch_list = WatchListFile(tmp_path / "watch-list.yaml")
    watch_list.add(_BLOGS, "https://blog.example.org/entry.xml")
    lock = FakeQueueLock()
    queue = SourceQueue(tmp_path / "source-queue.yaml", connectors=_PROVIDERS, lock=lock)
    queue.add("https://blog.example.org/a")
    scanner = FakeScanner(
        entries={
            "https://blog.example.org/entry.xml": [
                _entry("https://blog.example.org/a", 3),
                _entry("https://blog.example.org/b", 2),
                _entry("https://blog.example.org/c", 1),
            ]
        }
    )

    report = await scan_watch_list(watch_list, {_BLOGS: scanner}, queue, now=_NOW)

    assert (report.found, report.requested) == (3, 2), "the URL already on the queue is found but not requested"
    assert lock.holds == [True, True], "the seeding add, then one hold for the whole entry"
    assert [row.url for row in queue.entries()] == [
        "https://blog.example.org/a",
        "https://blog.example.org/b",
        "https://blog.example.org/c",
    ]


async def test_a_known_url_is_not_requested_twice(tmp_path: Path) -> None:
    watch_list = WatchListFile(tmp_path / "watch-list.yaml")
    watch_list.add(_PODCAST, "someone")
    queue = SourceQueue(tmp_path / "source-queue.yaml", connectors=_PROVIDERS)
    queue.add("https://podcast.example.org/1")
    scanner = FakeScanner(entries={"someone": [_entry("https://podcast.example.org/1", 1)]})

    report = await scan_watch_list(watch_list, {_PODCAST: scanner}, queue, now=_NOW)

    assert (report.found, report.requested) == (1, 0)
    assert len(queue.entries()) == 1


async def test_a_failing_feed_is_reported_and_the_others_still_scan(tmp_path: Path) -> None:
    watch_list = WatchListFile(tmp_path / "watch-list.yaml")
    watch_list.add(_BLOGS, "https://down.example.org/entry.xml")
    watch_list.add(_BLOGS, "https://blog.example.org/entry.xml")
    queue = SourceQueue(tmp_path / "source-queue.yaml", connectors=_PROVIDERS)
    scanner = FakeScanner(entries={"https://blog.example.org/entry.xml": [_entry("https://blog.example.org/a", 1)]}, errors={"https://down.example.org/entry.xml": "503"})

    report = await scan_watch_list(watch_list, {_BLOGS: scanner}, queue, now=_NOW)

    assert report.failed == (("blogs:https://down.example.org/entry.xml", "503"),)
    assert report.requested == 1
    stamps = {entry.ref: entry.last_scanned for entry in watch_list.entries()}
    assert stamps["https://down.example.org/entry.xml"] is None
    assert stamps["https://blog.example.org/entry.xml"] == _NOW


async def test_a_scanner_raising_anything_else_is_reported_and_the_others_still_scan(tmp_path: Path) -> None:
    watch_list = WatchListFile(tmp_path / "watch-list.yaml")
    watch_list.add(_BLOGS, "https://odd.example.org/entry.xml")
    watch_list.add(_BLOGS, "https://blog.example.org/entry.xml")
    queue = SourceQueue(tmp_path / "source-queue.yaml", connectors=_PROVIDERS)
    scanner = FakeScanner(
        entries={"https://blog.example.org/entry.xml": [_entry("https://blog.example.org/a", 1)]},
        raises={"https://odd.example.org/entry.xml": TypeError("can't compare offset-naive and offset-aware datetimes")},
    )

    report = await scan_watch_list(watch_list, {_BLOGS: scanner}, queue, now=_NOW)

    assert report.failed == (("blogs:https://odd.example.org/entry.xml", "can't compare offset-naive and offset-aware datetimes"),)
    assert report.requested == 1
    assert [row.url for row in queue.entries()] == ["https://blog.example.org/a"]


async def test_a_kind_no_scanner_answers_to_is_a_failure_naming_what_there_is(tmp_path: Path) -> None:
    watch_list = WatchListFile(tmp_path / "watch-list.yaml")
    watch_list.add("x", "someone")
    queue = SourceQueue(tmp_path / "source-queue.yaml", connectors=_PROVIDERS)

    report = await scan_watch_list(watch_list, {_BLOGS: FakeScanner()}, queue, now=_NOW)

    assert report.failed == (("x:someone", "no scanner named 'x' (scanners: blogs)"),)


async def test_an_empty_scanner_table_says_so_rather_than_naming_nothing(tmp_path: Path) -> None:
    watch_list = WatchListFile(tmp_path / "watch-list.yaml")
    watch_list.add("x", "someone")

    report = await scan_watch_list(watch_list, {}, SourceQueue(tmp_path / "source-queue.yaml", connectors=_PROVIDERS), now=_NOW)

    assert report.failed == (("x:someone", "no scanner named 'x' (scanners: none)"),)


def test_a_feed_kind_is_a_scanner_name_and_may_never_be_empty() -> None:
    with pytest.raises(ValidationError):
        WatchEntry(scanner="", ref="https://blog.example.org/entry.xml")


def test_the_kinds_a_file_carries_are_listed_once_each_in_first_seen_order(tmp_path: Path) -> None:
    watch_list = WatchListFile(tmp_path / "watch-list.yaml")
    watch_list.add(_PODCAST, "someone")
    watch_list.add(_BLOGS, "https://blog.example.org/entry.xml")
    watch_list.add(_BLOGS, "https://other.example.org/entry.xml")

    assert watch_list.scanners() == (_PODCAST, _BLOGS)


def test_a_file_that_does_not_exist_yet_carries_no_kinds(tmp_path: Path) -> None:
    assert WatchListFile(tmp_path / "watch-list.yaml").scanners() == ()


async def test_the_report_counts_each_entrys_new_rows_under_its_label(tmp_path: Path) -> None:
    watch_list = WatchListFile(tmp_path / "watch-list.yaml")
    watch_list.add(_BLOGS, "https://blog.example.org/entry.xml")
    watch_list.add(_PODCAST, "https://pod.example.org/feed.xml")
    queue = SourceQueue(tmp_path / "source-queue.yaml", connectors=_PROVIDERS)
    queue.add("https://blog.example.org/a")
    scanners = {
        _BLOGS: FakeScanner(
            entries={"https://blog.example.org/entry.xml": [_entry("https://blog.example.org/a", 3), _entry("https://blog.example.org/b", 2)]}
        ),
        _PODCAST: FakeScanner(entries={"https://pod.example.org/feed.xml": []}),
    }

    report = await scan_watch_list(watch_list, scanners, queue, now=_NOW)

    assert report.requested == 1
    assert report.requested_by_entry == {"blogs:https://blog.example.org/entry.xml": 1}, "an entry that added nothing is absent"


async def test_an_entry_whose_scan_fails_is_absent_from_the_counts(tmp_path: Path) -> None:
    watch_list = WatchListFile(tmp_path / "watch-list.yaml")
    watch_list.add(_PODCAST, "https://pod.example.org/feed.xml")
    queue = SourceQueue(tmp_path / "source-queue.yaml", connectors=_PROVIDERS)

    report = await scan_watch_list(watch_list, {}, queue, now=_NOW)

    assert report.requested_by_entry == {}
    assert [label for label, _ in report.failed] == ["podcast:https://pod.example.org/feed.xml"]
