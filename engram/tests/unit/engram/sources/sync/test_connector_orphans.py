"""Unit tests for provider-scoped orphan archival (`src/engram/sources/service/sync.py`).

A `SyncMirrorsHandler` only ever soft-deletes mirrors stamped with its own repository's
`provider`. A mirror belonging to another provider is never that handler's orphan, even
though `load_index` returns every provider's mirrors from the shared `Sources/` folder.
`MirrorFile.provider` is required: a note whose frontmatter names no connector is logged and
skipped by `load_index`, never guessed at.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from pydantic import ValidationError

from engram.sources.contracts import MirrorFile, MirrorFrontmatter, MirrorStatus, SyncMirrors, SyncMirrorsHandler
from engram.testing.fakes.connector import FakeConnector
from engram.testing.fakes.conversion import FakeDocumentConverter
from engram.testing.fakes.mirror import FakeMirrorStore

_NOW = datetime(2026, 8, 25, tzinfo=UTC)


def _mirror(key: str, provider: str) -> MirrorFrontmatter:
    return MirrorFrontmatter(
        title=key,
        item_type="article",
        source_id=key,
        provider=provider,
        provider_key=key,
        source_key=key,
        source_version=1,
        source_link="",
        date_added=_NOW,
        date_modified=_NOW,
        pdf_path="",
        source_hash="deadbeef",
        converter="fake",
        synced=_NOW,
    )


async def test_a_provider_sync_archives_only_its_own_orphans() -> None:
    store = FakeMirrorStore()
    await store.write(frontmatter=_mirror("intake-a", "intake"), body="body", assets={})
    await store.write(frontmatter=_mirror("zotero-b", "zotero"), body="body", assets={})
    empty_queue = FakeConnector(items=[])
    empty_queue.name = "intake"
    handler = SyncMirrorsHandler(empty_queue, FakeDocumentConverter(converter="fake"), store, clock=lambda: _NOW)

    report = await handler.handle(SyncMirrors())

    assert report.archived == 1
    index = await store.load_index()
    assert index["intake-a"].status is MirrorStatus.REMOVED
    assert index["zotero-b"].status is MirrorStatus.ACTIVE


def test_a_mirror_naming_no_provider_cannot_be_built() -> None:
    # The orphan pass narrows the index by `provider`, so there is no default to fall back on: a
    # mirror naming none is refused rather than attributed to whichever provider happened to run.
    with pytest.raises(ValidationError):
        MirrorFile.model_validate(
            {"source_key": "OLD0001", "source_version": 1, "source_hash": "deadbeef", "converter": "fake", "status": "active"}
        )
