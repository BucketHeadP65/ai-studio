"""Polyfactory fixtures for the mirror domain models.

Both keep the models' declared defaults (``MirrorStatus.ACTIVE``, empty author, collection and
keyword lists, ``MirrorFile.converter == "unknown"``). The scalar attributes below pin the fields
tests assert on, and polyfactory fills everything else. Override per test.
"""

from __future__ import annotations

from datetime import UTC, datetime

from polyfactory.factories.pydantic_factory import ModelFactory

from engram.sources.contracts import MirrorFile, MirrorFrontmatter, MirrorStatus

_EPOCH = datetime(2024, 1, 1, tzinfo=UTC)


class MirrorFileFactory(ModelFactory[MirrorFile]):
    """A `MirrorFile` with a pinned provider, ACTIVE by default. Override `provider`, `source_version`, `source_hash`, `status`, `url` or `doi`.

    `provider` is always `example-library`, so a sync run's orphan pass narrows the index to a
    known name. `url` and `doi` are `None`, so a built mirror holds no source a sync item could match.
    """

    __use_defaults__ = True
    provider = "example-library"
    status = MirrorStatus.ACTIVE
    url = None
    doi = None


class MirrorFrontmatterFactory(ModelFactory[MirrorFrontmatter]):
    """A deterministic `MirrorFrontmatter` with the fields tests assert on pinned. Override per test."""

    __use_defaults__ = True
    title = "Attention Is All You Need"
    item_type = "journalArticle"
    source_id = "ABCD1234"
    provider = "example-library"
    provider_key = "ABCD1234"
    source_key = "ABCD1234"
    source_version = 7
    source_link = "example-library://items/ABCD1234"
    date_added = _EPOCH
    date_modified = _EPOCH
    pdf_path = "/knowledge-base/library/storage/ABCD1234/paper.pdf"
    source_hash = "deadbeef"
    converter = "azure-di/prebuilt-layout+formulas"
    synced = _EPOCH
