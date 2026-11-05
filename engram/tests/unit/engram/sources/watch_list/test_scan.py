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
from engram.sources.domain.watch_list import REASON_LIMIT, scan_failure_reason, without_url_secrets
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


async def test_a_failure_spread_over_several_lines_is_recorded_as_its_first_line(tmp_path: Path) -> None:
    watch_list = WatchListFile(tmp_path / "watch-list.yaml")
    watch_list.add(_BLOGS, "https://down.example.org/entry.xml")
    queue = SourceQueue(tmp_path / "source-queue.yaml", connectors=_PROVIDERS)
    message = "Server error '503 Service Unavailable' for url 'https://down.example.org/entry.xml'\nFor more information check: https://developer.mozilla.org/503"
    scanner = FakeScanner(raises={"https://down.example.org/entry.xml": RuntimeError(message)})

    report = await scan_watch_list(watch_list, {_BLOGS: scanner}, queue, now=_NOW)

    assert report.failed == (
        ("blogs:https://down.example.org/entry.xml", "Server error '503 Service Unavailable' for url 'https://down.example.org/entry.xml'"),
    )


def test_a_reason_keeps_no_credential_a_url_in_the_message_carries() -> None:
    message = "Client error '401 Unauthorized' for url 'https://reader:hunter2@feeds.example.org/private.xml?token=s3cr3t&page=2#top'"

    assert scan_failure_reason(message) == "Client error '401 Unauthorized' for url 'https://feeds.example.org/private.xml'"


def test_a_user_part_holding_an_at_sign_is_dropped_whole() -> None:
    assert scan_failure_reason("fetch https://a@b@feeds.example.org/x.xml failed") == "fetch https://feeds.example.org/x.xml failed"


def test_a_host_path_with_no_scheme_loses_its_query_and_its_user_part() -> None:
    assert scan_failure_reason("see feeds.example.org/a.xml?token=abc") == "see feeds.example.org/a.xml"
    assert scan_failure_reason("see 'me:pw@feeds.example.org/a.xml#top'") == "see 'feeds.example.org/a.xml'"
    assert scan_failure_reason("see www.example.org?token=abc now") == "see www.example.org now"


def test_text_with_no_url_secret_is_left_as_it_is() -> None:
    message = "no scanner named 'x' (scanners: blogs, xml). Is example.org? Mail me@example.org, version 1.5."

    assert scan_failure_reason(message) == message


def test_a_watch_entry_label_loses_the_secrets_its_url_carries() -> None:
    label = "xml:https://user:pw@feeds.example.org/me.atom?token=s3cr3t"

    assert without_url_secrets(label) == "xml:https://feeds.example.org/me.atom"
    assert without_url_secrets("xml:feeds.example.org/me.atom?token=s3cr3t") == "xml:feeds.example.org/me.atom"
    assert without_url_secrets("x:someone") == "x:someone"


def test_a_reason_longer_than_the_limit_is_cut_and_says_so() -> None:
    reason = scan_failure_reason("status: " + "x" * 1000)

    assert len(reason) == REASON_LIMIT
    assert reason.endswith("...")


def test_a_message_with_no_text_has_no_reason() -> None:
    assert scan_failure_reason("  \n \n") == ""


async def test_an_exception_with_no_message_is_recorded_by_its_type(tmp_path: Path) -> None:
    watch_list = WatchListFile(tmp_path / "watch-list.yaml")
    watch_list.add(_BLOGS, "https://odd.example.org/entry.xml")
    queue = SourceQueue(tmp_path / "source-queue.yaml", connectors=_PROVIDERS)
    scanner = FakeScanner(raises={"https://odd.example.org/entry.xml": TimeoutError()})

    report = await scan_watch_list(watch_list, {_BLOGS: scanner}, queue, now=_NOW)

    assert report.failed == (("blogs:https://odd.example.org/entry.xml", "TimeoutError"),)
