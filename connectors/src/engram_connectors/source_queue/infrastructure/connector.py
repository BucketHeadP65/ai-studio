"""The `Connector` over the source queue: requested rows resolve, resolved rows serve from cache."""

from __future__ import annotations

import json
import re

from typing import TYPE_CHECKING
from urllib.parse import urlsplit

import yaml

from core.cqrs.contracts import utcnow
from core.files import atomic_write
from core.net import HttpxPageFetcher
from core.observability import get_logger
from engram.sources.contracts import (
    NOT_SERVED,
    Resolved,
    SourceItem,
    SourceQueue,
    SourceQueueStatus,
    SourceQueueUnreadableError,
    SourceType,
    arxiv_id_of,
    url_key,
)
from pydantic import ValidationError

from engram_connectors.conversion.contracts import ArticleConverter, ArxivHtmlConverter, zotero_converter_factory
from engram_connectors.source_queue.exceptions import ResolveError
from engram_connectors.source_queue.infrastructure.resolvers import (
    ArticleResolver,
    ArxivResolver,
    PaperResolver,
    PdfResolver,
    PostResolver,
    VideoResolver,
    challenge_reason,
    fetch_arxiv_pdf,
)
from engram_connectors.xai import xai_resource
from engram_connectors.youtube.contracts import VideoRef, VideoUrlUnrecognizedError, YouTubeTranscriptFetcher

if TYPE_CHECKING:
    from collections.abc import Callable, Mapping
    from datetime import datetime
    from pathlib import Path

    from engram.config import SourceResources
    from engram.sources.contracts import Connector, DocumentConverter, SourceQueueEntry

    from engram_connectors.source_queue.internal_contracts import Resolver

CONNECTOR = "intake"
"""This connector's registered name, the value every mirror note and `kb.sources_raw` row it writes carries."""

_X_STATUS = re.compile(r"^/[^/]+/status/(\d+)")
_X_HOSTS = frozenset({"x.com", "twitter.com", "mobile.twitter.com"})

_logger = get_logger(__name__)


def _x_status_id_of(url: str) -> str | None:
    """The status id an X or Twitter status URL names, or `None` for any other URL."""
    parts = urlsplit(url)
    host = parts.netloc.lower().removeprefix("www.")
    if host in _X_HOSTS and (match := _X_STATUS.match(parts.path)):
        return match.group(1)
    return None


class SourceQueueConnector:
    """`Connector` adapter over the source queue, one resolver per source type.

    A `requested` row is resolved once. Its markdown is cached under `cache_dir/<key>.md` beside a
    `<key>.meta.json` carrying the title, URL, channel, published date and converter label.
    From then on the row is `resolved` and served from that cache. A cached web page that is a bot check
    is never served: its cache files are deleted, a `requested` row (a retry) is fetched again, and any
    other row is marked `failed` with the reason.
    A row whose source type has no resolver is marked `skipped`.
    A resolver that raises a `ResolveError`, or an `OSError` from a file its converter could not read,
    marks the row `failed` with the reason, and the other rows still serve.
    """

    name = CONNECTOR

    def __init__(
        self, queue: SourceQueue, resolvers: Mapping[SourceType, Resolver], cache_dir: Path, *, clock: Callable[[], datetime] = utcnow
    ) -> None:
        """Store the queue, the resolver table, the cache directory and the clock.

        Args:
            queue: The source queue whose rows this connector serves.
            resolvers: The resolver for each source type. A source type without one is skipped.
            cache_dir: The directory resolved markdown and its metadata are cached under.
            clock: The clock that stamps each item's modified date.
        """
        self._queue = queue
        self._resolvers = resolvers
        self._cache_dir = cache_dir
        self._clock = clock

    @staticmethod
    def claims(url: str) -> SourceType | None:
        """The source type this connector serves for `url`.

        A YouTube watch, short, music or embed URL is a video. An arXiv link or a `.pdf` path is a paper.
        An X or Twitter status is a post. Anything else is an article, so this connector claims every URL
        no more specific connector claims first.

        Args:
            url: The URL to classify.

        Returns:
            The source type, never `None` for this connector.
        """
        try:
            VideoRef.parse(url)
        except VideoUrlUnrecognizedError:
            pass
        else:
            return SourceType.VIDEO
        if arxiv_id_of(url) is not None or urlsplit(url).path.lower().endswith(".pdf"):
            return SourceType.PAPER
        if _x_status_id_of(url) is not None:
            return SourceType.POST
        return SourceType.ARTICLE

    @staticmethod
    def key_for(url: str) -> str:
        """The provider key a row for `url` is stored under.

        Args:
            url: The URL to key.

        Returns:
            `yt-<video id>` for a video, `arxiv-<id>` for an arXiv paper, `x-<status id>` for a post,
            else the generic hash key.
        """
        try:
            return VideoRef.parse(url).key
        except VideoUrlUnrecognizedError:
            pass
        if (arxiv_id := arxiv_id_of(url)) is not None:
            return f"arxiv-{arxiv_id}"
        if (status_id := _x_status_id_of(url)) is not None:
            return f"x-{status_id}"
        return url_key(url)

    @staticmethod
    def source_type_of(item_type: str) -> SourceType:
        """The source type an item of this connector carries as its `item_type`.

        Args:
            item_type: The item type, which this connector writes as a source type value verbatim.

        Returns:
            The source type of that value.
        """
        return SourceType(item_type)

    @classmethod
    def build(cls, resources: SourceResources) -> Connector:
        """Construct the `"intake"` connector with one resolver per source type, cached under `_meta/source-queue-cache`.

        Without `document_intelligence`, papers convert through the arXiv HTML route only. An arXiv item then gets
        whatever that route reports, and a non-arXiv PDF item fails.
        Without an `xai` block that carries an `api_key`, there is no post resolver, so a post row stays `skipped`.

        Args:
            resources: The knowledge base, the optional Document Intelligence endpoint and the settings blocks.

        Returns:
            The connector over the knowledge base's `_meta/source-queue.yaml`.

        Raises:
            ValidationError: The `xai` settings block is present but does not validate.
        """
        meta = resources.knowledge_base.path / "_meta"
        cache_dir = meta / "source-queue-cache"
        paper_converter: DocumentConverter = (
            zotero_converter_factory(resources.document_intelligence) if resources.document_intelligence is not None else ArxivHtmlConverter()
        )
        arxiv_paper = ArxivResolver(paper_converter, cache_dir=cache_dir, fetch_pdf=fetch_arxiv_pdf)
        pdf_paper = PdfResolver(paper_converter, cache_dir=cache_dir)
        resolvers: dict[SourceType, Resolver] = {
            SourceType.VIDEO: VideoResolver(YouTubeTranscriptFetcher()),
            SourceType.PAPER: PaperResolver(arxiv_paper, pdf_paper),
            SourceType.ARTICLE: ArticleResolver(ArticleConverter(HttpxPageFetcher()), cache_dir=cache_dir),
        }
        if (xai := xai_resource(resources)) is not None:
            resolvers[SourceType.POST] = PostResolver(xai)
        return cls(SourceQueue(meta / "source-queue.yaml", connectors=()), resolvers, cache_dir)

    async def list_items_with_pdf(self) -> list[SourceItem]:
        """Every row that resolves, or already did, as a source item.

        Failed, skipped, seen and removed rows contribute none.

        Returns:
            The source items, in queue order.

        Raises:
            SourceQueueUnreadableError: The queue file cannot be read or does not parse.
        """
        items: list[SourceItem] = []
        for entry in self._entries():
            if entry.status in NOT_SERVED:
                continue
            item = await self._item_for(entry)
            if item is not None:
                items.append(item)
        return items

    def _entries(self) -> list[SourceQueueEntry]:
        """The queue's rows, turning an unreadable file into one sentence naming it."""
        try:
            return self._queue.entries()
        except (OSError, yaml.YAMLError, ValidationError) as error:
            msg = f"the source queue could not be read: {error}"
            raise SourceQueueUnreadableError(msg) from error

    async def get(self, provider_key: str) -> SourceItem | None:
        """The single row under `provider_key` as a source item.

        Args:
            provider_key: The row's key.

        Returns:
            The source item, or `None` when the row is absent, failed, skipped, seen or removed.

        Raises:
            SourceQueueUnreadableError: The queue file cannot be read.
        """
        try:
            entry = self._queue.get(provider_key)
        except (OSError, yaml.YAMLError, ValidationError) as error:
            msg = f"the source queue could not be read: {error}"
            raise SourceQueueUnreadableError(msg) from error
        if entry is None or entry.status in NOT_SERVED:
            return None
        return await self._item_for(entry)

    async def _item_for(self, entry: SourceQueueEntry) -> SourceItem | None:
        meta = self._read_meta(entry)
        if meta is not None and entry.source_type is SourceType.ARTICLE and (reason := challenge_reason(meta.title, meta.markdown, url=entry.url)):
            self._forget(entry)
            if entry.status is not SourceQueueStatus.REQUESTED:
                self._queue.mark(entry.key, SourceQueueStatus.FAILED, reason=reason)
                return None
            meta = None
        if meta is not None and entry.status is SourceQueueStatus.REQUESTED:
            self._queue.mark(entry.key, SourceQueueStatus.RESOLVED)
        if meta is None:
            meta = await self._resolve(entry)
            if meta is None:
                return None
        now = self._clock()
        return SourceItem(
            provider_key=entry.key,
            source_id=entry.key,
            version=1,
            item_type=entry.source_type.value,
            title=meta.title,
            year=meta.published.year if meta.published is not None else None,
            url=meta.url,
            channel=meta.channel,
            published=meta.published,
            date_added=entry.found_at or now,
            date_modified=now,
            pdf_path=self._cache_dir / f"{entry.key}.md",
        )

    async def _resolve(self, entry: SourceQueueEntry) -> Resolved | None:
        resolver = self._resolvers.get(entry.source_type)
        if resolver is None:
            self._queue.mark(entry.key, SourceQueueStatus.SKIPPED, reason=f"no resolver for {entry.source_type.value}")
            return None
        try:
            resolved = await resolver.resolve(entry)
        except (ResolveError, OSError) as error:
            self._queue.mark(entry.key, SourceQueueStatus.FAILED, reason=str(error))
            return None
        duplicate = self._mirrored_elsewhere(entry.key, resolved.title) if entry.found_via is not None else None
        if duplicate is not None:
            self._queue.mark(
                entry.key,
                SourceQueueStatus.SKIPPED,
                reason=f"duplicate of {duplicate}: the fetch answered the same title, {resolved.title!r}",
            )
            return None
        self._cache_dir.mkdir(parents=True, exist_ok=True)
        atomic_write(self._cache_dir / f"{entry.key}.md", resolved.markdown)
        atomic_write(self._cache_dir / f"{entry.key}.meta.json", resolved.model_dump_json(exclude={"markdown"}))
        self._queue.mark(entry.key, SourceQueueStatus.RESOLVED)
        return resolved

    def _mirrored_elsewhere(self, key: str, title: str) -> str | None:
        """The key of another cached mirror whose title is exactly `title`, or `None` when there is none."""
        wanted = title.strip()
        if not wanted:
            return None
        for meta_path in sorted(self._cache_dir.glob("*.meta.json")):
            other = meta_path.name.removesuffix(".meta.json")
            if other == key:
                continue
            try:
                cached = Resolved.model_validate({**json.loads(meta_path.read_text(encoding="utf-8")), "markdown": ""})
            except (OSError, TypeError, json.JSONDecodeError, ValidationError):
                continue
            if cached.title.strip() == wanted:
                return other
        return None

    def _forget(self, entry: SourceQueueEntry) -> None:
        """Delete the cached resolution of `entry`, so a retry fetches the page again."""
        for path in (self._cache_dir / f"{entry.key}.md", self._cache_dir / f"{entry.key}.meta.json"):
            path.unlink(missing_ok=True)

    def _read_meta(self, entry: SourceQueueEntry) -> Resolved | None:
        """The cached resolution for `entry`, or `None` when it is absent or unreadable."""
        meta_path = self._cache_dir / f"{entry.key}.meta.json"
        body_path = self._cache_dir / f"{entry.key}.md"
        if not (meta_path.is_file() and body_path.is_file()):
            return None
        try:
            meta = json.loads(meta_path.read_text(encoding="utf-8"))
            return Resolved.model_validate({**meta, "markdown": body_path.read_text(encoding="utf-8")})
        except (OSError, TypeError, json.JSONDecodeError, ValidationError) as error:
            _logger.warning("the source-queue cache for {key} is unreadable, resolving again: {reason}", key=entry.key, reason=str(error))
            return None
