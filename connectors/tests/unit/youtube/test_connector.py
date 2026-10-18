"""Unit tests for `YouTubeConnector`.

`_provider` wires a real `SourceQueue` over a `tmp_path` file, a `FakeTranscriptFetcher`, and a
`tmp_path` cache directory, so a resolved item's `pdf_path` is a real, readable file whose bytes
are the segmented transcript (`transcript_markdown`'s output), which is what the sync slice's
`pdf_sha256` hashes as the mirror's `source_hash`.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING

import pytest

from engram.sources.contracts import SourceQueue, SourceQueueStatus, SourceQueueUnreadableError, SourceType, claimants_for

from engram_connectors.testing.factories.youtube import VideoTranscriptFactory
from engram_connectors.testing.fakes.youtube import FakeTranscriptFetcher
from engram_connectors.youtube.contracts import (
    FetchedVideo,
    TranscriptUnavailableError,
    VideoFetchFailedError,
    YouTubeConnector,
    transcript_markdown,
)

if TYPE_CHECKING:
    from pathlib import Path

WATCH_URL = "https://www.youtube.com/watch?v=abc123XYZ_-"
_NOW = datetime(2026, 6, 12, tzinfo=UTC)


def _provider(tmp_path: Path, fetcher: FakeTranscriptFetcher) -> tuple[YouTubeConnector, SourceQueue]:
    ledger = SourceQueue(tmp_path / "source-queue.yaml", connectors=claimants_for(("youtube",)))
    provider = YouTubeConnector(ledger, fetcher, tmp_path / "cache", clock=lambda: _NOW)
    return provider, ledger


class TestProviderIdentity:
    def test_provider_name_is_youtube(self, tmp_path: Path):
        provider, _ledger = _provider(tmp_path, FakeTranscriptFetcher())
        assert provider.name == "youtube"


class TestListItemsWithPdf:
    async def test_empty_ledger_yields_no_items(self, tmp_path: Path):
        provider, _ledger = _provider(tmp_path, FakeTranscriptFetcher())

        assert await provider.list_items_with_pdf() == []

    async def test_a_requested_row_resolves_to_a_source_item(self, tmp_path: Path):
        doc = VideoTranscriptFactory.build()
        fetcher = FakeTranscriptFetcher(videos={doc.video_id: FetchedVideo(doc=doc, url=WATCH_URL)})
        provider, ledger = _provider(tmp_path, fetcher)
        ledger.add(WATCH_URL)

        items = await provider.list_items_with_pdf()

        assert len(items) == 1
        item = items[0]
        assert item.provider_key == f"yt-{doc.video_id}"
        assert item.source_id == f"yt-{doc.video_id}"
        assert item.pdf_path == tmp_path / "cache" / f"yt-{doc.video_id}.md"
        assert item.title == doc.title
        assert item.url == WATCH_URL
        assert item.year == 2024
        assert item.date_added == _NOW
        assert item.date_modified == _NOW

    async def test_a_row_found_earlier_is_dated_when_it_was_found(self, tmp_path: Path):
        doc = VideoTranscriptFactory.build()
        fetcher = FakeTranscriptFetcher(videos={doc.video_id: FetchedVideo(doc=doc, url=WATCH_URL)})
        provider, ledger = _provider(tmp_path, fetcher)
        found = datetime(2026, 6, 1, tzinfo=UTC)
        ledger.add(WATCH_URL, found_at=found)

        (item,) = await provider.list_items_with_pdf()

        assert item.date_added == found
        assert item.date_modified == _NOW
        assert item.item_type == SourceType.VIDEO.value

    async def test_the_items_pdf_path_holds_the_segmented_transcript_body(self, tmp_path: Path):
        doc = VideoTranscriptFactory.build()
        fetcher = FakeTranscriptFetcher(videos={doc.video_id: FetchedVideo(doc=doc, url=WATCH_URL)})
        provider, ledger = _provider(tmp_path, fetcher)
        ledger.add(WATCH_URL)

        items = await provider.list_items_with_pdf()

        assert items[0].pdf_path.read_text(encoding="utf-8") == transcript_markdown(doc)

    async def test_a_successful_resolution_marks_the_ledger_row_resolved(self, tmp_path: Path):
        doc = VideoTranscriptFactory.build()
        fetcher = FakeTranscriptFetcher(videos={doc.video_id: FetchedVideo(doc=doc, url=WATCH_URL)})
        provider, ledger = _provider(tmp_path, fetcher)
        ledger.add(WATCH_URL)

        await provider.list_items_with_pdf()

        assert ledger.entries()[0].status is SourceQueueStatus.RESOLVED

    async def test_the_items_channel_and_published_date_are_carried_from_the_transcript(self, tmp_path: Path):
        doc = VideoTranscriptFactory.build()
        fetcher = FakeTranscriptFetcher(videos={doc.video_id: FetchedVideo(doc=doc, url=WATCH_URL)})
        provider, ledger = _provider(tmp_path, fetcher)
        ledger.add(WATCH_URL)

        items = await provider.list_items_with_pdf()

        assert items[0].channel == doc.channel
        assert items[0].published == doc.published

    async def test_a_video_with_no_published_date_has_no_year(self, tmp_path: Path):
        doc = VideoTranscriptFactory.build(published=None)
        fetcher = FakeTranscriptFetcher(videos={doc.video_id: FetchedVideo(doc=doc, url=WATCH_URL)})
        provider, ledger = _provider(tmp_path, fetcher)
        ledger.add(WATCH_URL)

        items = await provider.list_items_with_pdf()

        assert items[0].year is None

    async def test_a_previously_resolved_row_is_resolved_again(self, tmp_path: Path):
        """`resolved` rows stay in scope so a re-sync can SKIP (unchanged) or RECONVERT (changed)."""
        doc = VideoTranscriptFactory.build()
        fetcher = FakeTranscriptFetcher(videos={doc.video_id: FetchedVideo(doc=doc, url=WATCH_URL)})
        provider, ledger = _provider(tmp_path, fetcher)
        entry = ledger.add(WATCH_URL)
        ledger.mark(entry.key, SourceQueueStatus.RESOLVED)

        items = await provider.list_items_with_pdf()

        assert len(items) == 1
        assert fetcher.fetched == [doc.video_id]

    async def test_a_failed_row_is_never_resolved(self, tmp_path: Path):
        doc = VideoTranscriptFactory.build()
        fetcher = FakeTranscriptFetcher(videos={doc.video_id: FetchedVideo(doc=doc, url=WATCH_URL)})
        provider, ledger = _provider(tmp_path, fetcher)
        entry = ledger.add(WATCH_URL)
        ledger.mark(entry.key, SourceQueueStatus.FAILED, reason="no-transcript")

        items = await provider.list_items_with_pdf()

        assert items == []
        assert fetcher.fetched == []

    async def test_no_caption_track_marks_the_row_failed_with_the_error_message(self, tmp_path: Path):
        video_id = "novideocaps"
        fetcher = FakeTranscriptFetcher(errors={video_id: TranscriptUnavailableError(video_id)})
        provider, ledger = _provider(tmp_path, fetcher)
        ledger.add(f"https://www.youtube.com/watch?v={video_id}")

        items = await provider.list_items_with_pdf()

        assert items == []
        entry = ledger.entries()[0]
        assert entry.status is SourceQueueStatus.FAILED
        assert video_id in (entry.reason or "")

    async def test_a_fetch_http_error_marks_the_row_failed_too(self, tmp_path: Path):
        video_id = "httpfailur1"
        fetcher = FakeTranscriptFetcher(errors={video_id: VideoFetchFailedError("boom")})
        provider, ledger = _provider(tmp_path, fetcher)
        ledger.add(f"https://www.youtube.com/watch?v={video_id}")

        await provider.list_items_with_pdf()

        assert ledger.entries()[0].status is SourceQueueStatus.FAILED

    async def test_one_failure_does_not_block_a_sibling_success(self, tmp_path: Path):
        doc = VideoTranscriptFactory.build(video_id="goodvideo01")
        bad_id = "badvideoid1"
        fetcher = FakeTranscriptFetcher(
            videos={doc.video_id: FetchedVideo(doc=doc, url=WATCH_URL)}, errors={bad_id: TranscriptUnavailableError(bad_id)}
        )
        provider, ledger = _provider(tmp_path, fetcher)
        ledger.add(f"https://www.youtube.com/watch?v={doc.video_id}")
        ledger.add(f"https://www.youtube.com/watch?v={bad_id}")

        items = await provider.list_items_with_pdf()

        assert [item.provider_key for item in items] == [f"yt-{doc.video_id}"]
        statuses = {row.key: row.status for row in ledger.entries()}
        assert statuses[f"yt-{doc.video_id}"] is SourceQueueStatus.RESOLVED
        assert statuses[f"yt-{bad_id}"] is SourceQueueStatus.FAILED

    async def test_a_row_for_another_host_is_not_listed_and_survives_the_mark(self, tmp_path: Path):
        doc = VideoTranscriptFactory.build()
        fetcher = FakeTranscriptFetcher(videos={doc.video_id: FetchedVideo(doc=doc, url=WATCH_URL)})
        provider, ledger = _provider(tmp_path, fetcher)
        intake_queue = SourceQueue(tmp_path / "source-queue.yaml", connectors=claimants_for(("intake",)))
        foreign = intake_queue.add("https://arxiv.org/abs/2401.01234")
        ledger.add(WATCH_URL)

        items = await provider.list_items_with_pdf()

        assert [item.provider_key for item in items] == [f"yt-{doc.video_id}"]
        assert fetcher.fetched == [doc.video_id]
        assert ledger.get(foreign.key) == foreign
        assert ledger.entries()[1].status is SourceQueueStatus.RESOLVED

    async def test_a_seen_row_is_never_fetched(self, tmp_path: Path):
        provider, ledger = _provider(tmp_path, FakeTranscriptFetcher())
        entry = ledger.add(WATCH_URL)
        ledger.mark(entry.key, SourceQueueStatus.SEEN)

        assert await provider.list_items_with_pdf() == []


class TestGet:
    async def test_get_resolves_a_single_row_by_video_id(self, tmp_path: Path):
        doc = VideoTranscriptFactory.build()
        fetcher = FakeTranscriptFetcher(videos={doc.video_id: FetchedVideo(doc=doc, url=WATCH_URL)})
        provider, ledger = _provider(tmp_path, fetcher)
        ledger.add(WATCH_URL)

        item = await provider.get(f"yt-{doc.video_id}")

        assert item is not None
        assert item.provider_key == f"yt-{doc.video_id}"

    async def test_get_finds_the_row_under_the_key_key_for_answers(self, tmp_path: Path):
        doc = VideoTranscriptFactory.build()
        fetcher = FakeTranscriptFetcher(videos={doc.video_id: FetchedVideo(doc=doc, url=WATCH_URL)})
        provider, ledger = _provider(tmp_path, fetcher)
        ledger.add(WATCH_URL)

        item = await provider.get(YouTubeConnector.key_for(WATCH_URL))

        assert item is not None
        assert item.provider_key == YouTubeConnector.key_for(WATCH_URL)

    async def test_get_returns_none_for_a_bare_video_id(self, tmp_path: Path):
        doc = VideoTranscriptFactory.build()
        fetcher = FakeTranscriptFetcher(videos={doc.video_id: FetchedVideo(doc=doc, url=WATCH_URL)})
        provider, ledger = _provider(tmp_path, fetcher)
        ledger.add(WATCH_URL)

        assert await provider.get(doc.video_id) is None

    async def test_get_returns_none_for_a_video_id_absent_from_the_ledger(self, tmp_path: Path):
        provider, _ledger = _provider(tmp_path, FakeTranscriptFetcher())

        assert await provider.get("yt-nosuchvid1") is None

    async def test_get_returns_none_for_a_failed_row(self, tmp_path: Path):
        provider, ledger = _provider(tmp_path, FakeTranscriptFetcher())
        entry = ledger.add(WATCH_URL)
        ledger.mark(entry.key, SourceQueueStatus.FAILED, reason="no-transcript")

        assert await provider.get(entry.key) is None


class TestUnreadableQueue:
    _BAD_SCHEMA = "- key: yt-abc\n  url: https://x\n  kind: not-a-kind\n  status: requested\n"
    _BAD_YAML = "- key: [unclosed\n"

    @pytest.mark.parametrize("content", [_BAD_SCHEMA, _BAD_YAML])
    async def test_listing_an_unreadable_queue_file_raises_the_mapped_error(self, tmp_path: Path, content: str):
        provider, _ledger = _provider(tmp_path, FakeTranscriptFetcher())
        (tmp_path / "source-queue.yaml").write_text(content, encoding="utf-8")

        with pytest.raises(SourceQueueUnreadableError, match="could not be read"):
            await provider.list_items_with_pdf()

    @pytest.mark.parametrize("content", [_BAD_SCHEMA, _BAD_YAML])
    async def test_getting_from_an_unreadable_queue_file_raises_the_mapped_error(self, tmp_path: Path, content: str):
        provider, _ledger = _provider(tmp_path, FakeTranscriptFetcher())
        (tmp_path / "source-queue.yaml").write_text(content, encoding="utf-8")

        with pytest.raises(SourceQueueUnreadableError, match="could not be read"):
            await provider.get("yt-abc123XYZ_-")


class TestClaims:
    def test_a_youtube_url_claims_video(self):
        assert YouTubeConnector.claims(WATCH_URL) is SourceType.VIDEO

    def test_a_non_youtube_url_claims_nothing(self):
        assert YouTubeConnector.claims("https://arxiv.org/abs/2401.01234") is None


class TestKeyFor:
    """`YouTubeConnector.key_for` — `yt-<video id>` for a URL it recognizes, else the generic hash key."""

    def test_a_watch_url_keys_by_its_video_id(self):
        assert YouTubeConnector.key_for(WATCH_URL) == "yt-abc123XYZ_-"

    def test_a_short_url_keys_by_its_video_id_too(self):
        assert YouTubeConnector.key_for("https://youtu.be/abc123XYZ_-") == "yt-abc123XYZ_-"

    def test_a_music_youtube_url_keys_by_its_video_id_too(self):
        assert YouTubeConnector.key_for("https://music.youtube.com/watch?v=abc123XYZ_-") == "yt-abc123XYZ_-"

    def test_a_url_this_provider_does_not_recognize_falls_back_to_the_generic_hash_key(self):
        key = YouTubeConnector.key_for("https://arxiv.org/abs/2401.01234")
        assert key.startswith("url-")
        assert len(key) == len("url-") + 16


class TestMediumOf:
    def test_every_item_type_folds_to_video(self):
        assert YouTubeConnector.source_type_of("video") is SourceType.VIDEO
        assert YouTubeConnector.source_type_of("whatever") is SourceType.VIDEO
