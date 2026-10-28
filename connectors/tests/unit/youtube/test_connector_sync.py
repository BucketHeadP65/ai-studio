"""`YouTubeConnector` driven through the real `SyncMirrorsHandler`, which is never modified here (see its own tests).

Exercises the ledger -> provider -> sync reconcile path end to end against the fakes: a
`FakeTranscriptFetcher` stands in for the network, a `FakeMirrorStore` records writes/deletes, and
`FakeDocumentConverter` stands in for the (not-yet-built) YouTube `DocumentConverter` adapter,
configured with the `converter` label that adapter is expected to report.
"""

from __future__ import annotations

import hashlib

from datetime import UTC, datetime
from typing import TYPE_CHECKING

from engram.sources.contracts import (
    SourceQueue,
    SourceQueueStatus,
    SourceType,
    SyncMirrors,
    SyncMirrorsHandler,
    UnresolvedItem,
    claimants_for,
)
from engram.testing.fakes.conversion import FakeDocumentConverter
from engram.testing.fakes.mirror import FakeMirrorStore

from engram_connectors.testing.factories.youtube import VideoTranscriptFactory
from engram_connectors.testing.fakes.youtube import FakeTranscriptFetcher
from engram_connectors.youtube.contracts import FetchedVideo, TranscriptUnavailableError, YouTubeConnector, transcript_markdown

if TYPE_CHECKING:
    from pathlib import Path

    from engram_connectors.youtube.contracts import VideoTranscript

WATCH_URL = "https://www.youtube.com/watch?v=abc123XYZ_-"
_NOW = datetime(2026, 6, 12, tzinfo=UTC)


def _handler(tmp_path: Path, fetcher: FakeTranscriptFetcher, store: FakeMirrorStore) -> tuple[SyncMirrorsHandler, SourceQueue]:
    ledger = SourceQueue(tmp_path / "source-queue.yaml", connectors=claimants_for(("youtube",)))
    provider = YouTubeConnector(ledger, fetcher, tmp_path / "cache", clock=lambda: _NOW)
    converter = FakeDocumentConverter(converter="youtube-transcript")
    handler = SyncMirrorsHandler(provider, converter, store, clock=lambda: _NOW)
    return handler, ledger


def _transcript_hash(doc: VideoTranscript) -> str:
    return hashlib.sha256(transcript_markdown(doc).encode("utf-8")).hexdigest()


class TestCreate:
    async def test_a_requested_row_produces_a_created_mirror(self, tmp_path: Path):
        doc = VideoTranscriptFactory.build()
        fetcher = FakeTranscriptFetcher(videos={doc.video_id: FetchedVideo(doc=doc, url=WATCH_URL)})
        store = FakeMirrorStore()
        handler, ledger = _handler(tmp_path, fetcher, store)
        ledger.add(WATCH_URL)

        report = await handler.handle(SyncMirrors())

        assert report.created == 1
        written = store.written[0]
        assert written.source_id == f"yt-{doc.video_id}"
        assert written.provider == "youtube"
        assert written.provider_key == f"yt-{doc.video_id}"
        assert written.source_hash == _transcript_hash(doc)
        assert written.converter == "youtube-transcript"
        assert store.bodies[f"yt-{doc.video_id}"] == "# Faked\n"  # the fake converter's canned body


class TestResync:
    async def test_unchanged_transcript_is_skipped_on_resync(self, tmp_path: Path):
        doc = VideoTranscriptFactory.build()
        fetcher = FakeTranscriptFetcher(videos={doc.video_id: FetchedVideo(doc=doc, url=WATCH_URL)})
        store = FakeMirrorStore()
        handler, ledger = _handler(tmp_path, fetcher, store)
        ledger.add(WATCH_URL)
        await handler.handle(SyncMirrors())

        resync_report = await handler.handle(SyncMirrors())

        assert resync_report.skipped == 1
        assert resync_report.created == 0
        assert resync_report.updated == 0

    async def test_a_changed_transcript_is_reconverted_on_resync(self, tmp_path: Path):
        doc = VideoTranscriptFactory.build()
        fetcher = FakeTranscriptFetcher(videos={doc.video_id: FetchedVideo(doc=doc, url=WATCH_URL)})
        store = FakeMirrorStore()
        handler, ledger = _handler(tmp_path, fetcher, store)
        ledger.add(WATCH_URL)
        await handler.handle(SyncMirrors())

        changed = VideoTranscriptFactory.build(video_id=doc.video_id, title=doc.title, segments=(*doc.segments, doc.segments[0]))
        fetcher.videos[doc.video_id] = FetchedVideo(doc=changed, url=WATCH_URL)
        report = await handler.handle(SyncMirrors())

        assert report.updated == 1
        assert report.created == 0
        assert store.index[f"yt-{doc.video_id}"].source_hash == _transcript_hash(changed)


class TestFetchFailure:
    async def test_a_fetch_failure_marks_failed_and_writes_no_mirror(self, tmp_path: Path):
        video_id = "novideocaps"
        fetcher = FakeTranscriptFetcher(errors={video_id: TranscriptUnavailableError(video_id)})
        store = FakeMirrorStore()
        handler, ledger = _handler(tmp_path, fetcher, store)
        ledger.add(f"https://www.youtube.com/watch?v={video_id}")

        report = await handler.handle(SyncMirrors())

        assert report.created == 0
        assert store.written == []
        assert store.deleted == []
        assert ledger.entries()[0].status is SourceQueueStatus.FAILED

    async def test_a_failing_video_does_not_block_a_sibling_creation(self, tmp_path: Path):
        good = VideoTranscriptFactory.build(video_id="goodvideo01")
        bad_id = "badvideoid1"
        fetcher = FakeTranscriptFetcher(
            videos={good.video_id: FetchedVideo(doc=good, url=WATCH_URL)}, errors={bad_id: TranscriptUnavailableError(bad_id)}
        )
        store = FakeMirrorStore()
        handler, ledger = _handler(tmp_path, fetcher, store)
        ledger.add(f"https://www.youtube.com/watch?v={good.video_id}")
        ledger.add(f"https://www.youtube.com/watch?v={bad_id}")

        report = await handler.handle(SyncMirrors())

        assert report.created == 1
        assert [written.provider_key for written in store.written] == [f"yt-{good.video_id}"]
        assert ledger.entries()[1].status is SourceQueueStatus.FAILED


class TestDryRun:
    async def test_a_dry_run_fetches_nothing_and_writes_no_queue_or_cache_file(self, tmp_path: Path):
        doc = VideoTranscriptFactory.build()
        fetcher = FakeTranscriptFetcher(videos={doc.video_id: FetchedVideo(doc=doc, url=WATCH_URL)})
        store = FakeMirrorStore()
        handler, ledger = _handler(tmp_path, fetcher, store)
        row = ledger.add(WATCH_URL)
        queue_text = (tmp_path / "source-queue.yaml").read_text(encoding="utf-8")

        report = await handler.handle(SyncMirrors(dry_run=True))

        assert fetcher.fetched == []
        assert (tmp_path / "source-queue.yaml").read_text(encoding="utf-8") == queue_text
        assert not (tmp_path / "cache").exists()
        assert report.planned == ()
        assert report.would_resolve == (UnresolvedItem(connector="youtube", key=row.key, source_type=SourceType.VIDEO, url=WATCH_URL),)
        assert store.written == []
