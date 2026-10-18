"""The YouTube connector, which resolves each YouTube row of the source queue through the transcript fetcher.

It serves only the rows it claims. A failed, skipped, seen or removed row contributes no item, and every other row
is fetched again on each call. A successful fetch writes the segmented transcript markdown to `<row key>.md` in the
cache directory and marks the row resolved. A failed fetch marks the row failed with the error's message and
contributes no item. `SourceItem.pdf_path` points at the cache file, whose bytes are the mirror body.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import yaml

from core.cqrs.contracts import utcnow
from core.files import atomic_write
from engram.sources.contracts import NOT_SERVED, SourceItem, SourceQueue, SourceQueueStatus, SourceQueueUnreadableError, SourceType, url_key
from pydantic import ValidationError

from engram_connectors.youtube.domain.segmenter import transcript_markdown
from engram_connectors.youtube.domain.video import VideoRef
from engram_connectors.youtube.exceptions import TranscriptUnavailableError, VideoFetchFailedError, VideoUrlUnrecognizedError
from engram_connectors.youtube.infrastructure.fetcher import YouTubeTranscriptFetcher

if TYPE_CHECKING:
    from collections.abc import Callable
    from datetime import datetime
    from pathlib import Path

    from engram.config import SourceResources
    from engram.sources.contracts import Connector, SourceQueueEntry

    from engram_connectors.youtube.internal_contracts import TranscriptFetcher


class YouTubeConnector:
    """The connector over the source queue that resolves each YouTube row through the transcript fetcher."""

    name = "youtube"

    def __init__(self, queue: SourceQueue, fetcher: TranscriptFetcher, cache_dir: Path, *, clock: Callable[[], datetime] = utcnow) -> None:
        """Keep the queue, the fetcher and the directory rendered transcripts are cached in.

        Args:
            queue: The knowledge base's source queue. The connector only reads and marks its rows.
            fetcher: The transcript fetcher.
            cache_dir: The directory each resolved video's segmented transcript is written to. It is created on the
                first write.
            clock: The timezone-aware clock stamped into a resolved item's `date_added` and `date_modified`.
        """
        self._queue = queue
        self._fetcher = fetcher
        self._cache_dir = cache_dir
        self._clock = clock

    @staticmethod
    def claims(url: str) -> SourceType | None:
        """Claim a YouTube watch, short, shorts, embed or live URL as a video.

        Args:
            url: The URL to classify.

        Returns:
            `SourceType.VIDEO` for a YouTube video URL, or `None` for any other URL.
        """
        try:
            VideoRef.parse(url)
        except VideoUrlUnrecognizedError:
            return None
        return SourceType.VIDEO

    @staticmethod
    def key_for(url: str) -> str:
        """The key the URL's row and mirror share.

        Args:
            url: A URL this connector claims.

        Returns:
            `yt-<video id>`, or the generic hash key when the URL does not parse as a YouTube video.
        """
        try:
            return VideoRef.parse(url).key
        except VideoUrlUnrecognizedError:
            return url_key(url)

    @staticmethod
    def source_type_of(item_type: str) -> SourceType:
        """Answer video for every row this connector resolves.

        Args:
            item_type: The row's item type, which is ignored.

        Returns:
            `SourceType.VIDEO`.
        """
        del item_type
        return SourceType.VIDEO

    @classmethod
    def build(cls, resources: SourceResources) -> Connector:
        """Construct the connector over the knowledge base's source queue and transcript cache under `_meta/`.

        Args:
            resources: The knowledge base and settings a connector is built from.

        Returns:
            The connector, reading `_meta/source-queue.yaml` and caching into `_meta/youtube-cache`.
        """
        queue = SourceQueue(resources.knowledge_base.path / "_meta" / "source-queue.yaml", connectors=())
        cache_dir = resources.knowledge_base.path / "_meta" / "youtube-cache"
        return cls(queue, YouTubeTranscriptFetcher(), cache_dir)

    async def list_items_with_pdf(self) -> list[SourceItem]:
        """Resolve every YouTube row that is requested or resolved through the fetcher.

        Rows for other hosts, and failed, skipped, seen and removed rows, contribute no item and stay as they are.

        Returns:
            One item per row whose fetch succeeded, in queue order.

        Raises:
            SourceQueueUnreadableError: The queue file cannot be read or does not parse.
        """
        items: list[SourceItem] = []
        for entry in self._entries():
            if not self._is_served(entry):
                continue
            item = await self._resolve(entry)
            if item is not None:
                items.append(item)
        return items

    async def get(self, provider_key: str) -> SourceItem | None:
        """Resolve the one row for `provider_key` through the fetcher.

        Args:
            provider_key: The row's key, `yt-<video id>` as `key_for` answers it.

        Returns:
            The resolved item, or `None` when the row is absent, is not a YouTube row, is failed, skipped, seen or
            removed, or its fetch fails.

        Raises:
            SourceQueueUnreadableError: The queue file cannot be read or does not parse.
        """
        try:
            entry = self._queue.get(provider_key)
        except (OSError, yaml.YAMLError, ValidationError) as error:
            msg = f"the source queue could not be read: {error}"
            raise SourceQueueUnreadableError(msg) from error
        if entry is None or not self._is_served(entry):
            return None
        return await self._resolve(entry)

    def _entries(self) -> list[SourceQueueEntry]:
        """The queue's rows, turning an unreadable file into one sentence naming it."""
        try:
            return self._queue.entries()
        except (OSError, yaml.YAMLError, ValidationError) as error:
            msg = f"the source queue could not be read: {error}"
            raise SourceQueueUnreadableError(msg) from error

    def _is_served(self, entry: SourceQueueEntry) -> bool:
        """Whether `entry` is a YouTube row whose status lets it be fetched."""
        return entry.status not in NOT_SERVED and self.claims(entry.url) is SourceType.VIDEO

    async def _resolve(self, entry: SourceQueueEntry) -> SourceItem | None:
        """Fetch `entry`'s transcript, mark the row, and map a success onto a `SourceItem`, or `None` on failure."""
        try:
            fetched = await self._fetcher.fetch(VideoRef.parse(entry.url).video_id)
        except (TranscriptUnavailableError, VideoFetchFailedError) as error:
            self._queue.mark(entry.key, SourceQueueStatus.FAILED, reason=str(error))
            return None

        self._cache_dir.mkdir(parents=True, exist_ok=True)
        cache_path = self._cache_dir / f"{entry.key}.md"
        atomic_write(cache_path, transcript_markdown(fetched.doc))
        self._queue.mark(entry.key, SourceQueueStatus.RESOLVED)

        now = self._clock()
        doc = fetched.doc
        return SourceItem(
            provider_key=entry.key,
            source_id=entry.key,
            version=1,
            item_type=SourceType.VIDEO.value,
            title=doc.title,
            year=doc.published.year if doc.published is not None else None,
            url=fetched.url,
            channel=doc.channel,
            published=doc.published,
            date_added=entry.found_at or now,
            date_modified=now,
            pdf_path=cache_path,
        )
