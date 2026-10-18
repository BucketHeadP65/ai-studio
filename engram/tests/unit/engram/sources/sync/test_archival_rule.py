"""Unit tests pinning the consolidated orphan-archival rule (`src/engram/sources/domain/sync.py`).

The per-mirror half of the archival precondition lives in one named predicate,
`should_archive`; the full-run half is threaded through a single `full_run_archives`
flag, so "when is a mirror archived?" is answerable at a glance.

These tests pin the predicate directly AND the handler end-to-end, so an off-by-one in
the `limit is None` linkage — which would archive the whole library on a partial
backfill — stays caught.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING

from engram.sources.contracts import MirrorStatus, SyncMirrors, SyncMirrorsHandler
from engram.sources.domain.sync import should_archive
from engram.testing.factories.mirror import MirrorFileFactory
from engram.testing.fakes.connector import FakeConnector
from engram.testing.fakes.conversion import FakeDocumentConverter
from engram.testing.fakes.mirror import FakeMirrorStore

if TYPE_CHECKING:
    from pathlib import Path

    from engram.sources.contracts import SourceItem

_NOW = datetime(2026, 6, 12, tzinfo=UTC)
_ZOTERO = "zotero"  # MirrorFileFactory's own default provider name, matched explicitly on the fake below


def _write_pdf(tmp_path: Path, name: str, contents: bytes) -> Path:
    pdf = tmp_path / name
    pdf.write_bytes(contents)
    return pdf


def _item(tmp_path: Path, *, key: str, contents: bytes = b"%PDF data") -> SourceItem:
    from engram.testing.factories.source_item import SourceItemFactory

    return SourceItemFactory.build(provider_key=key, source_id=key, version=1, pdf_path=_write_pdf(tmp_path, f"{key}.pdf", contents))


def _handler(items: list[SourceItem], store: FakeMirrorStore) -> SyncMirrorsHandler:
    return SyncMirrorsHandler(FakeConnector(items=items, name=_ZOTERO), FakeDocumentConverter(), store, clock=lambda: _NOW)


class TestShouldArchive:
    """The per-mirror archival predicate: an ACTIVE mirror whose key left Zotero."""

    def test_active_mirror_absent_from_zotero_is_archived(self):
        mirror = MirrorFileFactory.build(source_key="GONE0001", status=MirrorStatus.ACTIVE)

        assert should_archive(mirror, present_keys={"KEEP0001"}) is True

    def test_active_mirror_still_present_is_not_archived(self):
        mirror = MirrorFileFactory.build(source_key="KEEP0001", status=MirrorStatus.ACTIVE)

        assert should_archive(mirror, present_keys={"KEEP0001"}) is False

    def test_already_removed_mirror_is_not_archived(self):
        mirror = MirrorFileFactory.build(source_key="GONE0001", status=MirrorStatus.REMOVED)

        assert should_archive(mirror, present_keys={"KEEP0001"}) is False


class TestFullRunGate:
    """The full-run gate: only an unlimited run archives; a partial backfill archives nothing."""

    async def test_full_run_archives_an_orphaned_active_mirror(self, tmp_path: Path):
        keep = _item(tmp_path, key="ITEM0001", contents=b"%PDF keep")
        gone = _item(tmp_path, key="ITEM0002", contents=b"%PDF gone")
        store = FakeMirrorStore()
        await _handler([keep, gone], store).handle(SyncMirrors())

        report = await _handler([keep], store).handle(SyncMirrors())

        assert report.archived == 1
        assert store.deleted == ["ITEM0002"]
        assert store.index["ITEM0002"].status is MirrorStatus.REMOVED

    async def test_partial_backfill_archives_nothing_even_when_orphaned(self, tmp_path: Path):
        keep = _item(tmp_path, key="ITEM0001", contents=b"%PDF keep")
        gone = _item(tmp_path, key="ITEM0002", contents=b"%PDF gone")
        store = FakeMirrorStore()
        await _handler([keep, gone], store).handle(SyncMirrors())

        # A limited run is a partial backfill: the absent mirror must stay ACTIVE (the off-by-one
        # disaster guard — a `limit is None` slip would archive the whole library here).
        report = await _handler([keep], store).handle(SyncMirrors(limit=1))

        assert report.archived == 0
        assert store.deleted == []
        assert store.index["ITEM0002"].status is MirrorStatus.ACTIVE

    async def test_already_archived_mirror_is_not_rearchived_on_a_full_run(self, tmp_path: Path):
        keep = _item(tmp_path, key="ITEM0001", contents=b"%PDF keep")
        store = FakeMirrorStore(index={"GONE0001": MirrorFileFactory.build(source_key="GONE0001", status=MirrorStatus.REMOVED)})

        report = await _handler([keep], store).handle(SyncMirrors())

        assert report.archived == 0
        assert store.deleted == []
