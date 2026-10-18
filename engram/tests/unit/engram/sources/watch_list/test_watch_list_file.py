"""Unit specs for the watch-list file's lock (`engram.sources.infrastructure.watch_list`)."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING

from engram.sources.contracts import WatchListFile
from engram.testing.fakes.source_queue import FakeQueueLock

if TYPE_CHECKING:
    from pathlib import Path

_NOW = datetime(2026, 8, 25, 9, 0, tzinfo=UTC)
_FEED = "https://example.org/feed.xml"
_OTHER = "https://example.org/other.xml"


def test_every_write_is_held_exclusively_and_a_read_takes_no_lock(tmp_path: Path) -> None:
    lock = FakeQueueLock()
    watch_list = WatchListFile(tmp_path / "watch-list.yaml", lock=lock)
    watch_list.add("xml", _FEED)
    watch_list.stamp("xml", _FEED, _NOW)
    watch_list.remove("xml", _FEED)
    watch_list.entries()
    watch_list.scanners()
    assert lock.holds == [True, True, True]


def test_a_row_another_process_lands_first_survives_an_add(tmp_path: Path) -> None:
    path = tmp_path / "watch-list.yaml"

    def land_the_other_row() -> None:
        WatchListFile(path).add("xml", _OTHER)

    WatchListFile(path, lock=FakeQueueLock(before_first=land_the_other_row)).add("xml", _FEED)

    assert sorted(entry.ref for entry in WatchListFile(path).entries()) == [_FEED, _OTHER]


def test_the_lock_file_is_a_dotfile_beside_the_watch_list(tmp_path: Path) -> None:
    WatchListFile(tmp_path / "watch-list.yaml").add("xml", _FEED)
    assert sorted(path.name for path in tmp_path.iterdir()) == [".watch-list.yaml.lock", "watch-list.yaml"]
