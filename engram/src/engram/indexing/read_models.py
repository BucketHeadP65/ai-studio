"""What the indexing slice's reads return: stored page keys, clean pages, rows awaiting a vector, fingerprints and pending summaries."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from pydantic import BaseModel, ConfigDict


@dataclass(frozen=True, slots=True)
class PageKeyRowResult:
    """One committed `kb.pages` key row: the four resolution keys and the page's cited `source_id`.

    It carries no file path. It satisfies `resolver.PageKeyRow`.
    """

    id: str
    slug: str
    title: str
    aliases: tuple[str, ...]
    source_id: str | None


@dataclass(frozen=True, slots=True)
class CleanPage:
    """One unchanged `kb.pages` row the incremental path reads, with its file path.

    It holds the four resolution keys, the file `path` and the cited `source_id`, which is `None` for a page that cites no source.
    It satisfies `resolver.PageKeyRow`, so it passes straight into `build_link_index_from_pages`.
    """

    id: str
    slug: str
    title: str
    aliases: tuple[str, ...]
    path: str
    source_id: str | None = None


@dataclass(frozen=True, slots=True)
class PendingEmbedRow:
    """One stored page, section or claim row whose `embedding IS NULL`."""

    id: str


@dataclass(frozen=True, slots=True)
class PendingEmbedChunk:
    """One stored raw chunk whose `embedding IS NULL`, with the `source_id` of the mirror it belongs to."""

    id: str
    source_id: str


@dataclass(frozen=True, slots=True)
class StoredFingerprint:
    """One stored `kb.index_fingerprint` row: the four inputs of the last index and the knowledge base root that ran it.

    `knowledge_base_root` is empty when no index recorded a root.
    """

    indexer_version: str
    spec_hash: str
    vocabulary_hash: str
    embed_model: str
    knowledge_base_root: str = ""


class PendingSummary(BaseModel):
    """One pending `kb.unit_summaries` row, as the review surfaces present it."""

    model_config = ConfigDict(frozen=True)

    unit_id: str
    unit_type: str
    collection: str
    summary: str
    model: str
    indexed_at: datetime
