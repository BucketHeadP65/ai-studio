"""The MultiHop-RAG connector, which serves the benchmark's news articles as source items and writes its questions as judgments.

The first listing downloads the dataset's two files into the knowledge base's `_meta/multihop-rag-cache/raw/`,
writes one `docs/<source id>.md` file per article (its title as a heading, then its body) and the neutral
`judgments.jsonl` file. A later listing reads the cache, downloads nothing and rewrites no document file, so sync
sees every unchanged article as unchanged. `SourceItem.pdf_path` points at the article's cache file, whose bytes
are the mirror body.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING

from core.files import atomic_write
from core.net import HttpxPageFetcher
from engram.sources.contracts import SourceItem, SourceType, url_key

from engram_connectors.multihop_rag.domain.dataset import (
    ARTICLES_FILE,
    QUESTIONS_FILE,
    Article,
    judged_queries,
    parse_articles,
    parse_questions,
)
from engram_connectors.multihop_rag.exceptions import MultihopRagDatasetError
from engram_connectors.multihop_rag.infrastructure.download import PINNED_HASHES, ensure_dataset

if TYPE_CHECKING:
    from pathlib import Path

    from core.net import ByteFetcher
    from engram.config import SourceResources
    from engram.sources.contracts import Connector

    from engram_connectors.multihop_rag.domain.dataset import DatasetHashes


class MultihopRagConnector:
    """The connector over the MultiHop-RAG benchmark's articles, at one pinned dataset revision."""

    name = "multihop_rag"

    def __init__(self, *, cache_dir: Path, fetcher: ByteFetcher, hashes: DatasetHashes = PINNED_HASHES) -> None:
        """Keep the cache root, the download and the hashes the downloaded files must match.

        Args:
            cache_dir: The cache root. The dataset files, the document files and the judgments file land under it.
            fetcher: The guarded download the dataset files come through.
            hashes: The SHA-256 prefix each downloaded file must start with.
        """
        self.cache_dir = cache_dir
        self._fetcher = fetcher
        self._hashes = hashes

    @staticmethod
    def claims(url: str) -> SourceType | None:
        """Claim no URL: a benchmark dataset is never added one link at a time.

        Args:
            url: The URL to classify, which is ignored.

        Returns:
            `None`.
        """
        del url
        return None

    @staticmethod
    def key_for(url: str) -> str:
        """The generic key for a URL, which this connector never claims.

        Args:
            url: The URL.

        Returns:
            The generic hash key.
        """
        return url_key(url)

    @staticmethod
    def source_type_of(item_type: str) -> SourceType:
        """Answer article for every item.

        Args:
            item_type: The item type, which is ignored.

        Returns:
            `SourceType.ARTICLE`.
        """
        del item_type
        return SourceType.ARTICLE

    @classmethod
    def build(cls, resources: SourceResources) -> Connector:
        """Construct the connector over the pinned revision, caching into the knowledge base's `_meta/multihop-rag-cache`.

        Args:
            resources: The knowledge base and settings a connector is built from.

        Returns:
            The connector.
        """
        return cls(cache_dir=resources.knowledge_base.path / "_meta" / "multihop-rag-cache", fetcher=HttpxPageFetcher(timeout=600.0))

    async def list_items_with_pdf(self) -> list[SourceItem]:
        """Every article as a source item, downloading and caching the dataset on the first call.

        Returns:
            One item per article, in file order.

        Raises:
            MultihopRagDatasetError: When a dataset file does not match its hash, does not parse, or a question cites
                an article the dataset lacks.
            FetchError: When a dataset file cannot be downloaded.
        """
        articles = await self._articles()
        stamp = self._stamp()
        return [self._item(article, stamp) for article in articles]

    async def get(self, provider_key: str) -> SourceItem | None:
        """The one article whose source id is `provider_key`.

        Args:
            provider_key: The article's source id.

        Returns:
            The item, or `None` when the dataset holds no such article.

        Raises:
            MultihopRagDatasetError: When a dataset file does not match its hash, does not parse, or a question cites
                an article the dataset lacks.
            FetchError: When a dataset file cannot be downloaded.
        """
        articles = await self._articles()
        stamp = self._stamp()
        return next((self._item(article, stamp) for article in articles if article.source_id == provider_key), None)

    async def _articles(self) -> list[Article]:
        """The dataset's articles, with every document file and the judgments file in place."""
        raw = await ensure_dataset(self._fetcher, self.cache_dir, self._hashes)
        articles = parse_articles(_read(raw / ARTICLES_FILE))
        docs_dir = self.cache_dir / "docs"
        docs_dir.mkdir(parents=True, exist_ok=True)
        for article in articles:
            path = docs_dir / f"{article.source_id}.md"
            if not path.is_file():
                atomic_write(path, article.markdown())
        self._write_judgments(raw, articles)
        return articles

    def _write_judgments(self, raw: Path, articles: list[Article]) -> None:
        """Write `judgments.jsonl` from the questions, rewriting it only when its text changes."""
        queries = judged_queries(articles, parse_questions(_read(raw / QUESTIONS_FILE)))
        text = "".join(query.model_dump_json() + "\n" for query in queries)
        target = self.cache_dir / "judgments.jsonl"
        if not target.is_file() or target.read_text(encoding="utf-8") != text:
            atomic_write(target, text)

    def _stamp(self) -> datetime:
        """The time every item carries as added and modified: when the articles file was downloaded."""
        return datetime.fromtimestamp((self.cache_dir / "raw" / ARTICLES_FILE).stat().st_mtime, UTC)

    def _item(self, article: Article, stamp: datetime) -> SourceItem:
        """One article as a source item over its cache file."""
        fields = {
            "outlet": article.outlet,
            "author": article.author,
            "published_at": article.published_at,
            "published_on": article.published.isoformat(),
            "category": article.category,
            "url": article.url,
        }
        return SourceItem(
            provider_key=article.source_id,
            source_id=article.source_id,
            version=1,
            item_type=SourceType.ARTICLE.value,
            title=article.title.strip(),
            published=article.published,
            url=article.url,
            date_added=stamp,
            date_modified=stamp,
            pdf_path=self.cache_dir / "docs" / f"{article.source_id}.md",
            meta={key: value for key, value in fields.items() if value is not None},
        )


def _read(path: Path) -> bytes:
    """The bytes of one dataset file, refused by name when it is missing."""
    try:
        return path.read_bytes()
    except OSError as error:
        msg = f"the dataset cache has no {path.name}: {error}"
        raise MultihopRagDatasetError(msg) from error
