"""The BEIR connector, which serves one benchmark dataset's documents as source items and writes its relevance judgments.

The first listing downloads the dataset's archive into the knowledge base's `_meta/beir-cache/<dataset>/`, writes
one `docs/<id>.md` file per document (its title as a heading, then its text) and the neutral
`judgments.<split>.jsonl` file. A later listing reads the cache, downloads nothing and rewrites no document file,
so sync sees every unchanged document as unchanged. `SourceItem.pdf_path` points at the document's cache file,
whose bytes are the mirror body.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING

import yaml

from core.files import atomic_write
from core.net import HttpxPageFetcher
from engram.sources.contracts import ItemPreview, SourceItem, SourceType, url_key
from pydantic import ValidationError

from engram_connectors.beir.domain.dataset import BeirDocument, file_stems, judged_queries, parse_documents, parse_judgments, parse_queries
from engram_connectors.beir.domain.settings import BeirSettings
from engram_connectors.beir.exceptions import BeirDatasetError, BeirSettingsError
from engram_connectors.beir.infrastructure.download import ensure_dataset

if TYPE_CHECKING:
    from pathlib import Path

    from core.net import ByteFetcher
    from engram.config import SourceResources
    from engram.sources.contracts import Connector

_SETTINGS_FILE = "_meta/beir.yaml"


class BeirConnector:
    """The connector over one BEIR benchmark dataset, named in the knowledge base's `_meta/beir.yaml`."""

    name = "beir"

    def __init__(self, *, dataset: str, split: str, cache_dir: Path, fetcher: ByteFetcher) -> None:
        """Keep the dataset, the split, the cache root and the download.

        Args:
            dataset: The BEIR dataset name, such as `scifact`.
            split: The split whose relevance judgments the judgments file is written from.
            cache_dir: The cache root. The dataset's files land under `cache_dir/<dataset>/`.
            fetcher: The guarded download the archive comes through.
        """
        self.dataset = dataset
        self._split = split
        self._root = cache_dir / dataset
        self._cache_dir = cache_dir
        self._fetcher = fetcher

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
        """Answer article for every document.

        Args:
            item_type: The item type, which is ignored.

        Returns:
            `SourceType.ARTICLE`.
        """
        del item_type
        return SourceType.ARTICLE

    @classmethod
    def build(cls, resources: SourceResources) -> Connector:
        """Construct the connector from the knowledge base's `_meta/beir.yaml`, caching into `_meta/beir-cache`.

        Args:
            resources: The knowledge base and settings a connector is built from.

        Returns:
            The connector over the dataset the file names.

        Raises:
            BeirSettingsError: When `_meta/beir.yaml` is missing, does not parse, or names no valid dataset.
        """
        path = resources.knowledge_base.path / _SETTINGS_FILE
        try:
            settings = BeirSettings.model_validate(yaml.safe_load(path.read_text(encoding="utf-8")))
        except (OSError, yaml.YAMLError, ValidationError) as error:
            msg = f"the beir connector reads {_SETTINGS_FILE}, which must hold `dataset: <name>` and optionally `split: test`: {error}"
            raise BeirSettingsError(msg) from error
        return cls(
            dataset=settings.dataset,
            split=settings.split,
            cache_dir=resources.knowledge_base.path / "_meta" / "beir-cache",
            fetcher=HttpxPageFetcher(timeout=600.0),
        )

    async def list_items_with_pdf(self) -> list[SourceItem]:
        """Every document of the dataset as a source item, downloading and caching the dataset on the first call.

        Returns:
            One item per document, in file order.

        Raises:
            BeirDatasetError: When the dataset's files are missing or a line does not parse.
            FetchError: When the archive cannot be downloaded.
        """
        documents, stems = await self._documents()
        stamp = self._stamp()
        return [self._item(document, stems[document.id], stamp) for document in documents]

    async def preview_items(self) -> ItemPreview:
        """The items `list_items_with_pdf` answers, with no row awaiting resolution.

        That listing calls no model and changes no row. Before the dataset's first download it downloads and unpacks
        the dataset, as a real listing would.

        Returns:
            Those items, with no row awaiting resolution.
        """
        return ItemPreview(items=tuple(await self.list_items_with_pdf()))

    async def get(self, provider_key: str) -> SourceItem | None:
        """The one document whose id is `provider_key`.

        Args:
            provider_key: The document id.

        Returns:
            The item, or `None` when the dataset holds no such document.

        Raises:
            BeirDatasetError: When the dataset's files are missing or a line does not parse.
            FetchError: When the archive cannot be downloaded.
        """
        documents, stems = await self._documents()
        stamp = self._stamp()
        return next((self._item(document, stems[document.id], stamp) for document in documents if document.id == provider_key), None)

    async def _documents(self) -> tuple[list[BeirDocument], dict[str, str]]:
        """The dataset's documents and their cache file stems, with every cache file and the judgments file in place."""
        raw = await ensure_dataset(self._fetcher, self.dataset, self._cache_dir)
        documents = list(parse_documents(_lines(raw / "corpus.jsonl")))
        stems = file_stems(documents)
        docs_dir = self._root / "docs"
        docs_dir.mkdir(parents=True, exist_ok=True)
        for document in documents:
            path = docs_dir / f"{stems[document.id]}.md"
            if not path.is_file():
                atomic_write(path, document.markdown())
        self._write_judgments(raw)
        return documents, stems

    def _write_judgments(self, raw: Path) -> None:
        """Write `judgments.<split>.jsonl` from the dataset's queries and its split's relevance judgments."""
        queries = judged_queries(parse_queries(_lines(raw / "queries.jsonl")), parse_judgments(_lines(raw / "qrels" / f"{self._split}.tsv")))
        text = "".join(query.model_dump_json() + "\n" for query in queries)
        target = self._root / f"judgments.{self._split}.jsonl"
        if not target.is_file() or target.read_text(encoding="utf-8") != text:
            atomic_write(target, text)

    def _stamp(self) -> datetime:
        """The time every item carries as added and modified: when the dataset's documents were extracted."""
        corpus = self._root / "raw" / self.dataset / "corpus.jsonl"
        return datetime.fromtimestamp(corpus.stat().st_mtime, UTC)

    def _item(self, document: BeirDocument, stem: str, stamp: datetime) -> SourceItem:
        """One document as a source item over its cache file."""
        return SourceItem(
            provider_key=document.id,
            source_id=document.id,
            version=1,
            item_type=SourceType.ARTICLE.value,
            title=document.display_title,
            date_added=stamp,
            date_modified=stamp,
            pdf_path=self._root / "docs" / f"{stem}.md",
        )


def _lines(path: Path) -> list[str]:
    """The lines of one dataset file, refused by name when it is missing."""
    try:
        return path.read_text(encoding="utf-8").splitlines()
    except OSError as error:
        msg = f"the dataset has no {path.name}: {error}"
        raise BeirDatasetError(msg) from error
