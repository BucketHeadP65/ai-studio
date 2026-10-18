import re

from datetime import UTC, datetime
from pathlib import Path

from engram.sources.contracts import SourceQueue, SourceQueueStatus, SourceType
from engram.testing.fakes.source_queue import FakeQueueLock

WATCH = "https://www.youtube.com/watch?v=abc123XYZ_-"
ARXIV = "https://arxiv.org/abs/2401.01234"
_NOW = datetime(2026, 8, 25, 9, 0, tzinfo=UTC)

_YOUTUBE_ID = re.compile(r"(?:v=|youtu\.be/)([A-Za-z0-9_-]{11})")
_ARXIV_ID = re.compile(r"arxiv\.org/abs/([\d.]+)")


class _FakeYouTubeClaimant:
    """Stands in for the real `YouTubeConnector`'s recognition, so this queue suite needs no provider installed."""

    @staticmethod
    def claims(url: str) -> SourceType | None:
        return SourceType.VIDEO if _YOUTUBE_ID.search(url) else None

    @staticmethod
    def key_for(url: str) -> str:
        match = _YOUTUBE_ID.search(url)
        return f"yt-{match.group(1)}" if match else url


class _FakeArxivClaimant:
    """Stands in for the real `SourceQueueConnector`'s arXiv recognition, the same reason."""

    @staticmethod
    def claims(url: str) -> SourceType | None:
        return SourceType.PAPER if _ARXIV_ID.search(url) else None

    @staticmethod
    def key_for(url: str) -> str:
        match = _ARXIV_ID.search(url)
        return f"arxiv-{match.group(1)}" if match else url


_PROVIDERS = (_FakeYouTubeClaimant, _FakeArxivClaimant)


def test_add_classifies_and_lands_a_requested_row(tmp_path: Path) -> None:
    queue = SourceQueue(tmp_path / "source-queue.yaml", connectors=_PROVIDERS)
    row = queue.add(ARXIV, found_via="rss:https://blog.example.org/feed.xml", found_at=_NOW)
    assert row.key == "arxiv-2401.01234"
    assert row.source_type is SourceType.PAPER
    assert row.status is SourceQueueStatus.REQUESTED
    assert row.found_via == "rss:https://blog.example.org/feed.xml"
    assert queue.entries() == [row]


def test_add_is_idempotent_on_key_and_keeps_the_existing_status(tmp_path: Path) -> None:
    queue = SourceQueue(tmp_path / "source-queue.yaml", connectors=_PROVIDERS)
    first = queue.add(WATCH)
    queue.mark(first.key, SourceQueueStatus.RESOLVED)
    again = queue.add("https://youtu.be/abc123XYZ_-")
    assert again.status is SourceQueueStatus.RESOLVED
    assert len(queue.entries()) == 1


def test_retry_sets_a_failed_row_back_to_requested_and_clears_the_reason(tmp_path: Path) -> None:
    queue = SourceQueue(tmp_path / "source-queue.yaml", connectors=_PROVIDERS)
    row = queue.add(WATCH)
    queue.mark(row.key, SourceQueueStatus.FAILED, reason="no captions")
    retried = queue.retry(row.key)
    assert retried is not None
    assert retried.status is SourceQueueStatus.REQUESTED
    assert retried.reason is None


def test_retry_of_a_row_that_did_not_fail_is_a_no_op(tmp_path: Path) -> None:
    queue = SourceQueue(tmp_path / "source-queue.yaml", connectors=_PROVIDERS)
    row = queue.add(WATCH)
    assert queue.retry(row.key) is None
    assert queue.retry("missing") is None


def test_two_interleaved_adds_both_land(tmp_path: Path) -> None:
    path = tmp_path / "source-queue.yaml"
    other = SourceQueue(path, connectors=_PROVIDERS)

    def land_the_other_row() -> None:
        other.add(ARXIV)

    SourceQueue(path, connectors=_PROVIDERS, lock=FakeQueueLock(before_first=land_the_other_row)).add(WATCH)

    assert sorted(row.key for row in SourceQueue(path, connectors=_PROVIDERS).entries()) == ["arxiv-2401.01234", "yt-abc123XYZ_-"]


def test_a_mutation_is_held_exclusively_and_a_read_takes_no_lock_at_all(tmp_path: Path) -> None:
    lock = FakeQueueLock()
    queue = SourceQueue(tmp_path / "source-queue.yaml", connectors=_PROVIDERS, lock=lock)
    row = queue.add(WATCH)
    queue.mark(row.key, SourceQueueStatus.FAILED, reason="no captions")
    queue.retry(row.key)
    queue.entries()
    queue.get(row.key)
    assert lock.holds == [True, True, True]


def test_the_lock_file_is_a_dotfile_beside_the_ledger(tmp_path: Path) -> None:
    SourceQueue(tmp_path / "source-queue.yaml", connectors=_PROVIDERS).add(WATCH)
    assert sorted(path.name for path in tmp_path.iterdir()) == [".source-queue.yaml.lock", "source-queue.yaml"]


def test_a_read_only_knowledge_base_directory_still_lists(tmp_path: Path) -> None:
    """Reading takes no lock, so nothing tries to create a lock file where nothing may be written."""
    path = tmp_path / "source-queue.yaml"
    SourceQueue(path, connectors=_PROVIDERS).add(WATCH)
    (tmp_path / ".source-queue.yaml.lock").unlink()
    tmp_path.chmod(0o555)
    try:
        assert [row.key for row in SourceQueue(path, connectors=_PROVIDERS).entries()] == ["yt-abc123XYZ_-"]
    finally:
        tmp_path.chmod(0o755)


def test_add_many_lands_only_the_urls_the_ledger_did_not_have(tmp_path: Path) -> None:
    queue = SourceQueue(tmp_path / "source-queue.yaml", connectors=_PROVIDERS)
    queue.add(WATCH)
    added = queue.add_many([WATCH, ARXIV, ARXIV, "https://blog.example.org/post"], found_via="rss:feed", found_at=_NOW)
    assert added == 2, "the known key and the repeat within the batch both land once"
    keys = [row.key for row in queue.entries()]
    assert keys[:2] == ["yt-abc123XYZ_-", "arxiv-2401.01234"], "queue order is arrival order"
    assert keys[2].startswith("url-")
    landed = queue.get("arxiv-2401.01234")
    assert landed is not None
    assert landed.found_via == "rss:feed"
    assert landed.found_at == _NOW


def test_add_many_reads_once_and_writes_once(tmp_path: Path) -> None:
    lock = FakeQueueLock()
    queue = SourceQueue(tmp_path / "source-queue.yaml", connectors=_PROVIDERS, lock=lock)
    assert queue.add_many([WATCH, ARXIV]) == 2
    assert lock.holds == [True], "one hold for the whole batch"


def test_add_many_with_nothing_new_leaves_the_file_alone(tmp_path: Path) -> None:
    path = tmp_path / "source-queue.yaml"
    queue = SourceQueue(path, connectors=_PROVIDERS)
    queue.add(WATCH)
    written = path.read_text(encoding="utf-8")
    assert queue.add_many([WATCH]) == 0
    assert path.read_text(encoding="utf-8") == written


def test_add_once_says_which_call_put_the_row_on_the_ledger(tmp_path: Path) -> None:
    queue = SourceQueue(tmp_path / "source-queue.yaml", connectors=_PROVIDERS)
    row, added = queue.add_once(WATCH)
    again, added_again = queue.add_once("https://youtu.be/abc123XYZ_-")
    assert added
    assert not added_again
    assert again.key == row.key


def test_the_file_round_trips_every_field(tmp_path: Path) -> None:
    path = tmp_path / "source-queue.yaml"
    SourceQueue(path, connectors=_PROVIDERS).add(ARXIV, found_via="x:@someone", found_at=_NOW)
    reread = SourceQueue(path, connectors=_PROVIDERS).get("arxiv-2401.01234")
    assert reread is not None
    assert reread.found_at == _NOW
    assert reread.url == ARXIV


def test_add_bounded_refuses_a_new_row_once_the_source_hit_its_cap(tmp_path: Path) -> None:
    queue = SourceQueue(tmp_path / "source-queue.yaml", connectors=_PROVIDERS)
    first, added = queue.add_bounded("https://arxiv.org/abs/2401.00001", found_via="paper.a", cap=2, found_at=_NOW)
    queue.add_bounded("https://arxiv.org/abs/2401.00002", found_via="paper.a", cap=2, found_at=_NOW)

    refused = queue.add_bounded("https://arxiv.org/abs/2401.00003", found_via="paper.a", cap=2, found_at=_NOW)
    _, other_landed = queue.add_bounded("https://arxiv.org/abs/2401.00003", found_via="paper.b", cap=2, found_at=_NOW)

    assert added
    assert first is not None
    assert refused == (None, False)
    assert other_landed, "the cap is per source: another source still lands its own rows"
    assert queue.add_bounded("https://arxiv.org/abs/2401.00001", found_via="paper.a", cap=2, found_at=_NOW) == (first, False)
    assert [row.key for row in queue.entries()] == ["arxiv-2401.00001", "arxiv-2401.00002", "arxiv-2401.00003"]


def test_depth_counts_ledger_hops_from_a_source_the_owner_added(tmp_path: Path) -> None:
    """A hop is a link between two ROWS: `found_via` naming a Zotero key or the owner is the chain's root, not a hop."""
    queue = SourceQueue(tmp_path / "source-queue.yaml", connectors=_PROVIDERS)

    assert queue.depth_of("MJ389JS8") == 0
    first, _ = queue.add_bounded("https://arxiv.org/abs/2401.00001", found_via="MJ389JS8", cap=5, reason="builds on it", found_at=_NOW)
    assert first is not None
    second, _ = queue.add_bounded(
        "https://arxiv.org/abs/2401.00002", found_via=first.key, cap=5, reason="compares against it", found_at=_NOW
    )
    owner = queue.add("https://blog.example.org/post", found_at=_NOW)

    assert second is not None
    assert first.reason == "builds on it"
    assert queue.chain_of(first.key) == (first.key,), "MJ389JS8 is a Zotero key, not a row on this queue"
    assert queue.depth_of(first.key) == 0
    assert queue.chain_of(second.key) == (second.key, first.key)
    assert queue.depth_of(second.key) == 1
    assert queue.depth_of(owner.key) == 0


def test_a_row_found_via_a_feed_label_is_depth_zero_not_one(tmp_path: Path) -> None:
    """A feed label is not a queue row either, so the row it landed is a root, and what it found is one hop."""
    queue = SourceQueue(tmp_path / "source-queue.yaml", connectors=_PROVIDERS)
    feed_row = queue.add(ARXIV, found_via="arXiv cs.CL new", found_at=_NOW)

    assert queue.chain_of(feed_row.key) == (feed_row.key,)
    assert queue.depth_of(feed_row.key) == 0

    reference, added = queue.add_bounded(
        "https://arxiv.org/abs/2401.99999", found_via=feed_row.key, cap=5, reason="builds on it", found_at=_NOW
    )
    assert added
    assert reference is not None
    assert queue.depth_of(reference.key) == 1


def test_count_of_counts_every_row_for_found_via_whatever_its_status(tmp_path: Path) -> None:
    queue = SourceQueue(tmp_path / "source-queue.yaml", connectors=_PROVIDERS)
    queue.add_bounded("https://arxiv.org/abs/2401.00001", found_via="paper.a", cap=5, found_at=_NOW)
    queue.add_seen("https://arxiv.org/abs/2401.00002", found_via="paper.a", reason="beyond hop depth 1", found_at=_NOW)
    queue.mark("arxiv-2401.00001", SourceQueueStatus.SKIPPED)

    assert queue.count_of("paper.a") == 2
    assert queue.count_of("paper.b") == 0


def test_add_seen_records_a_row_no_sync_fetches_and_is_idempotent(tmp_path: Path) -> None:
    queue = SourceQueue(tmp_path / "source-queue.yaml", connectors=_PROVIDERS)

    row, added = queue.add_seen(ARXIV, found_via="url-abc", reason="beyond hop depth 1 (found via url-abc via MJ389JS8)", found_at=_NOW)
    again, added_again = queue.add_seen(ARXIV, found_via="url-abc", reason="something else", found_at=_NOW)

    assert added is True
    assert row.status is SourceQueueStatus.SEEN
    assert row.reason == "beyond hop depth 1 (found via url-abc via MJ389JS8)"
    assert (again, added_again) == (row, False)


def test_decide_fetch_and_drop_move_a_row_and_refuse_the_rest(tmp_path: Path) -> None:
    queue = SourceQueue(tmp_path / "source-queue.yaml", connectors=_PROVIDERS)
    row, _ = queue.add_seen(ARXIV, found_via="url-abc", reason="beyond hop depth 1", found_at=_NOW)

    fetched = queue.decide(row.key, "fetch")
    assert fetched is not None
    assert fetched.status is SourceQueueStatus.REQUESTED
    assert fetched.reason is None

    dropped = queue.decide(row.key, "drop", reason="the owner said no")
    assert dropped is not None
    assert dropped.status is SourceQueueStatus.SKIPPED
    assert dropped.reason == "the owner said no"

    assert queue.decide(row.key, "drop") is None
    assert queue.decide("nope", "fetch") is None
    assert queue.get(row.key) == dropped


def test_decide_drop_with_no_reason_keeps_the_rows_existing_reason(tmp_path: Path) -> None:
    queue = SourceQueue(tmp_path / "source-queue.yaml", connectors=_PROVIDERS)
    row, _ = queue.add_seen(
        ARXIV, found_via="arxiv-2401.00001", reason="beyond hop depth 1 (found via arxiv-2401.00001 via ZKEY)", found_at=_NOW
    )

    dropped = queue.decide(row.key, "drop")

    assert dropped is not None
    assert dropped.status is SourceQueueStatus.SKIPPED
    assert dropped.reason == "beyond hop depth 1 (found via arxiv-2401.00001 via ZKEY)"


def test_remove_marks_an_existing_row_and_keeps_it_terminal(tmp_path: Path) -> None:
    queue = SourceQueue(tmp_path / "source-queue.yaml", connectors=_PROVIDERS)
    row, _ = queue.add_seen(ARXIV, found_via="url-abc", reason="beyond hop depth 1", found_at=_NOW)

    removed = queue.remove(ARXIV, reason="removed 2026-08-31: off-domain", found_at=_NOW)

    assert removed.status is SourceQueueStatus.REMOVED
    assert removed.reason == "removed 2026-08-31: off-domain"
    assert queue.decide(row.key, "fetch") is None  # a removed row cannot be revived by decide
    assert queue.retry(row.key) is None  # nor by retry


def test_remove_of_an_unknown_url_appends_a_removed_row(tmp_path: Path) -> None:
    queue = SourceQueue(tmp_path / "source-queue.yaml", connectors=_PROVIDERS)

    removed = queue.remove(ARXIV, reason="removed 2026-08-31: owner", found_at=_NOW)

    assert removed.status is SourceQueueStatus.REMOVED
    assert queue.get(removed.key) is not None


def test_a_removed_rows_url_is_not_re_added_by_add_many(tmp_path: Path) -> None:
    queue = SourceQueue(tmp_path / "source-queue.yaml", connectors=_PROVIDERS)
    queue.remove(ARXIV, reason="removed 2026-08-31: owner", found_at=_NOW)

    added = queue.add_many([ARXIV], found_via="feed:arxiv", found_at=_NOW)

    assert added == 0
