"""Unit tests for the sync slice handler (`src/engram/sources/service/sync.py`).

A table-test over the pure `decide` function (all five branches), plus full-flow
`SyncMirrorsHandler` scenarios driven by the in-memory fakes and the polyfactory
`SourceItemFactory`. The flow tests write real PDF bytes under `tmp_path` so
`pdf_sha256` computes genuine, comparable hashes. Both a reconversion and a metadata
refresh count as `SyncReport.updated` (the distinction stays observable through the
converter's call count and the stored body), and a missing PDF is collected into
`unavailable` rather than `errors`.
"""

from __future__ import annotations

from datetime import UTC, date, datetime
from typing import TYPE_CHECKING

from core.cqrs.contracts import ImmediateWake, MemoryJobs, MemoryOutbox, MessageBus, Worker

from engram.sources.contracts import ConversionError, MirrorStatus, SourceType, SyncMirrors, SyncMirrorsHandler, SyncReport, UnresolvedItem
from engram.sources.domain.sync import PlannedAction, SyncAction, SyncCompleted, SyncStarted, build_frontmatter, decide
from engram.sources.service.sync import pdf_sha256
from engram.testing.factories.mirror import MirrorFileFactory
from engram.testing.factories.source_item import SourceItemFactory
from engram.testing.fakes.bus_spy import BusSpy
from engram.testing.fakes.connector import FakeConnector
from engram.testing.fakes.conversion import FakeDocumentConverter
from engram.testing.fakes.mirror import FakeMirrorStore

if TYPE_CHECKING:
    from pathlib import Path

    from core.cqrs.contracts import Event

    from engram.sources.contracts import SourceItem

_NOW = datetime(2026, 6, 12, tzinfo=UTC)
_PROVIDER = "example-library"  # MirrorFileFactory's own pinned provider name, matched explicitly on the fake below


def _write_pdf(tmp_path: Path, name: str, contents: bytes) -> Path:
    pdf = tmp_path / name
    pdf.write_bytes(contents)
    return pdf


def _item(tmp_path: Path, *, key: str, version: int = 1, contents: bytes = b"%PDF data") -> SourceItem:
    """A SourceItem whose `pdf_path` is a real file under `tmp_path` (so hashing is real)."""
    return SourceItemFactory.build(provider_key=key, source_id=key, version=version, pdf_path=_write_pdf(tmp_path, f"{key}.pdf", contents))


def _item_without_pdf(tmp_path: Path, *, key: str, version: int = 1) -> SourceItem:
    """A SourceItem whose `pdf_path` points at a file that does not exist (hashing raises OSError)."""
    return SourceItemFactory.build(provider_key=key, source_id=key, version=version, pdf_path=tmp_path / f"{key}-missing.pdf")


def _handler(items: list[SourceItem], store: FakeMirrorStore, converter: FakeDocumentConverter | None = None) -> SyncMirrorsHandler:
    """A handler over the fakes with a pinned clock (every write is stamped `_NOW`)."""
    return SyncMirrorsHandler(
        FakeConnector(items=items, name=_PROVIDER), converter or FakeDocumentConverter(), store, clock=lambda: _NOW
    )


class TestDecide:
    def test_no_existing_mirror_is_create(self):
        item = SourceItemFactory.build(version=1)
        assert decide(item, None, "hash-a") is SyncAction.CREATE

    def test_removed_mirror_is_recreated(self):
        item = SourceItemFactory.build(version=1)
        existing = MirrorFileFactory.build(source_version=1, source_hash="hash-a", status=MirrorStatus.REMOVED)
        assert decide(item, existing, "hash-a") is SyncAction.CREATE

    def test_changed_pdf_hash_is_reconvert(self):
        item = SourceItemFactory.build(version=1)
        existing = MirrorFileFactory.build(source_version=1, source_hash="hash-a")
        assert decide(item, existing, "hash-b") is SyncAction.RECONVERT

    def test_version_bump_with_same_hash_is_refresh(self):
        item = SourceItemFactory.build(version=2)
        existing = MirrorFileFactory.build(source_version=1, source_hash="hash-a")
        assert decide(item, existing, "hash-a") is SyncAction.REFRESH

    def test_unchanged_version_and_hash_is_skip(self):
        item = SourceItemFactory.build(version=1)
        existing = MirrorFileFactory.build(source_version=1, source_hash="hash-a")
        assert decide(item, existing, "hash-a") is SyncAction.SKIP


class TestBuildFrontmatter:
    def test_maps_source_item_onto_frontmatter(self):
        item = SourceItemFactory.build(
            provider_key="WXYZ7890", source_id="WXYZ7890", version=4, source_link="zotero://select/library/items/WXYZ7890"
        )

        frontmatter = build_frontmatter(
            item, provider="zotero", source_hash="hash-z", status=MirrorStatus.ACTIVE, synced=_NOW, converter="some/conv"
        )

        assert frontmatter.source_id == "WXYZ7890"
        assert frontmatter.provider == "zotero"
        assert frontmatter.provider_key == "WXYZ7890"
        assert frontmatter.source_key == "WXYZ7890"
        assert frontmatter.source_version == 4
        assert frontmatter.source_hash == "hash-z"
        assert frontmatter.converter == "some/conv"
        assert frontmatter.status is MirrorStatus.ACTIVE
        assert frontmatter.synced == _NOW
        assert frontmatter.pdf_path == str(item.pdf_path)
        assert frontmatter.source_link == "zotero://select/library/items/WXYZ7890"
        assert frontmatter.keywords == item.tags

    def test_source_link_passes_through_from_the_item_regardless_of_provider(self):
        """The link is the item's own (`SourceItem.source_link`, filled by whichever provider mirrored it), never derived here."""
        item = SourceItemFactory.build(provider_key="WXYZ7890", source_id="WXYZ7890", version=4, source_link="")

        frontmatter = build_frontmatter(
            item, provider="zotero", source_hash="hash-z", status=MirrorStatus.ACTIVE, synced=_NOW, converter="some/conv"
        )

        assert frontmatter.source_link == ""
        assert frontmatter.provider == "zotero"

    def test_a_youtube_items_channel_and_published_date_land_on_the_frontmatter(self):
        item = SourceItemFactory.build(
            provider_key="abc123XYZ_-", source_id="abc123XYZ_-", version=1, channel="Some Channel", published=date(2024, 3, 5)
        )

        frontmatter = build_frontmatter(
            item, provider="youtube", source_hash="hash-y", status=MirrorStatus.ACTIVE, synced=_NOW, converter="youtube-transcript"
        )

        assert frontmatter.channel == "Some Channel"
        assert frontmatter.published == date(2024, 3, 5)

    def test_authors_are_last_comma_first(self):
        item = SourceItemFactory.build()
        creator = item.creators[0]

        frontmatter = build_frontmatter(
            item, provider="zotero", source_hash="h", status=MirrorStatus.ACTIVE, synced=_NOW, converter="some/conv"
        )

        assert frontmatter.authors[0] == f"{creator.last_name}, {creator.first_name}"


class TestPdfSha256:
    def test_hashes_file_bytes(self, tmp_path: Path):
        pdf = _write_pdf(tmp_path, "a.pdf", b"%PDF-1.7 alpha")

        assert pdf_sha256(pdf) == pdf_sha256(pdf)

    def test_different_bytes_hash_differently(self, tmp_path: Path):
        alpha = _write_pdf(tmp_path, "a.pdf", b"%PDF alpha")
        beta = _write_pdf(tmp_path, "b.pdf", b"%PDF beta")

        assert pdf_sha256(alpha) != pdf_sha256(beta)


class TestApply:
    async def test_empty_knowledge_base_creates_every_item(self, tmp_path: Path):
        items = [_item(tmp_path, key="ITEM0001", contents=b"%PDF one"), _item(tmp_path, key="ITEM0002", contents=b"%PDF two")]
        store = FakeMirrorStore()
        converter = FakeDocumentConverter()

        report = await _handler(items, store, converter).handle(SyncMirrors())

        assert report.created == 2
        assert converter.call_count == 2
        assert len(store.written) == 2

    async def test_rerun_unchanged_skips_all_and_never_converts(self, tmp_path: Path):
        items = [_item(tmp_path, key="ITEM0001", contents=b"%PDF one"), _item(tmp_path, key="ITEM0002", contents=b"%PDF two")]
        store = FakeMirrorStore()
        await _handler(items, store).handle(SyncMirrors())

        rerun_converter = FakeDocumentConverter()
        report = await _handler(items, store, rerun_converter).handle(SyncMirrors())

        assert report.skipped == 2
        assert report.created == 0
        assert rerun_converter.call_count == 0

    async def test_create_stamps_the_converters_provenance_label(self, tmp_path: Path):
        item = _item(tmp_path, key="ITEM0001", contents=b"%PDF one")
        store = FakeMirrorStore()
        converter = FakeDocumentConverter(converter="azure-di/prebuilt-layout+formulas")

        await _handler([item], store, converter).handle(SyncMirrors())

        assert store.written[0].converter == "azure-di/prebuilt-layout+formulas"
        assert store.index["ITEM0001"].converter == "azure-di/prebuilt-layout+formulas"

    async def test_an_items_metadata_is_written_into_its_mirror_frontmatter(self, tmp_path: Path):
        pdf = _write_pdf(tmp_path, "ITEM0001.pdf", b"%PDF one")
        item = SourceItemFactory.build(provider_key="ITEM0001", source_id="ITEM0001", pdf_path=pdf, meta={"region": "north", "issued_on": "2024-01-02"})
        store = FakeMirrorStore()

        await _handler([item], store).handle(SyncMirrors())

        assert store.written[0].meta == {"region": "north", "issued_on": "2024-01-02"}
        assert store.written[0].to_disk_fields()["meta"] == {"region": "north", "issued_on": "2024-01-02"}

    async def test_an_item_without_metadata_writes_no_meta_key(self, tmp_path: Path):
        item = _item(tmp_path, key="ITEM0001")
        store = FakeMirrorStore()

        await _handler([item], store).handle(SyncMirrors())

        assert item.meta == {}, "the factory builds an item with no metadata"
        assert "meta" not in store.written[0].to_disk_fields()

    async def test_version_bump_only_refreshes_without_converting_and_keeps_body(self, tmp_path: Path):
        item = _item(tmp_path, key="ITEM0001", contents=b"%PDF one")
        store = FakeMirrorStore()
        await _handler([item], store).handle(SyncMirrors())
        original_body = store.bodies["ITEM0001"]

        bumped = SourceItemFactory.build(provider_key="ITEM0001", source_id="ITEM0001", version=2, pdf_path=item.pdf_path)
        refresh_converter = FakeDocumentConverter()
        report = await _handler([bumped], store, refresh_converter).handle(SyncMirrors())

        assert report.updated == 1
        assert refresh_converter.call_count == 0
        assert store.bodies["ITEM0001"] == original_body

    async def test_refresh_preserves_the_existing_converter_label_without_converting(self, tmp_path: Path):
        item = _item(tmp_path, key="ITEM0001", contents=b"%PDF one")
        store = FakeMirrorStore()
        await _handler([item], store, FakeDocumentConverter(converter="arxiv-html/pandoc")).handle(SyncMirrors())

        bumped = SourceItemFactory.build(provider_key="ITEM0001", source_id="ITEM0001", version=2, pdf_path=item.pdf_path)
        refresh_converter = FakeDocumentConverter(converter="should-not-be-used")
        report = await _handler([bumped], store, refresh_converter).handle(SyncMirrors())

        assert report.updated == 1
        assert refresh_converter.call_count == 0
        assert store.written[-1].converter == "arxiv-html/pandoc"
        assert store.index["ITEM0001"].converter == "arxiv-html/pandoc"

    async def test_refresh_with_missing_body_reconverts_and_restamps_the_label(self, tmp_path: Path):
        item = _item(tmp_path, key="ITEM0001", contents=b"%PDF one")
        store = FakeMirrorStore(
            index={
                "ITEM0001": MirrorFileFactory.build(
                    source_key="ITEM0001", source_version=1, source_hash=pdf_sha256(item.pdf_path), converter="arxiv-html/pandoc"
                )
            }
        )

        bumped = SourceItemFactory.build(provider_key="ITEM0001", source_id="ITEM0001", version=2, pdf_path=item.pdf_path)
        refresh_converter = FakeDocumentConverter(converter="azure-di/prebuilt-layout+formulas")
        report = await _handler([bumped], store, refresh_converter).handle(SyncMirrors())

        assert report.updated == 1
        assert refresh_converter.call_count == 1
        assert store.written[-1].converter == "azure-di/prebuilt-layout+formulas"

    async def test_create_passes_item_url_as_source_url(self, tmp_path: Path):
        item = SourceItemFactory.build(
            provider_key="ITEM0001",
            source_id="ITEM0001",
            version=1,
            url="https://arxiv.org/abs/1234.5678",
            pdf_path=_write_pdf(tmp_path, "ITEM0001.pdf", b"%PDF one"),
        )
        converter = FakeDocumentConverter()

        await _handler([item], FakeMirrorStore(), converter).handle(SyncMirrors())

        assert converter.last_source_url == "https://arxiv.org/abs/1234.5678"

    async def test_changed_pdf_bytes_reconverts_once(self, tmp_path: Path):
        item = _item(tmp_path, key="ITEM0001", contents=b"%PDF original")
        store = FakeMirrorStore()
        await _handler([item], store).handle(SyncMirrors())

        item.pdf_path.write_bytes(b"%PDF rewritten differently")
        reconvert_converter = FakeDocumentConverter()
        report = await _handler([item], store, reconvert_converter).handle(SyncMirrors())

        assert report.updated == 1
        assert reconvert_converter.call_count == 1

    async def test_item_absent_from_repo_is_archived(self, tmp_path: Path):
        keep = _item(tmp_path, key="ITEM0001", contents=b"%PDF keep")
        gone = _item(tmp_path, key="ITEM0002", contents=b"%PDF gone")
        store = FakeMirrorStore()
        await _handler([keep, gone], store).handle(SyncMirrors())

        report = await _handler([keep], store).handle(SyncMirrors())

        assert report.archived == 1
        assert store.deleted == ["ITEM0002"]
        assert report.skipped == 1
        assert store.index["ITEM0002"].status is MirrorStatus.REMOVED
        assert store.index["ITEM0001"].status is MirrorStatus.ACTIVE

    async def test_limited_run_keeps_absent_mirror_active(self, tmp_path: Path):
        keep = _item(tmp_path, key="ITEM0001", contents=b"%PDF keep")
        gone = _item(tmp_path, key="ITEM0002", contents=b"%PDF gone")
        store = FakeMirrorStore()
        await _handler([keep, gone], store).handle(SyncMirrors())

        report = await _handler([keep], store).handle(SyncMirrors(limit=1))

        assert report.archived == 0
        assert store.deleted == []
        assert store.index["ITEM0002"].status is MirrorStatus.ACTIVE

    async def test_limit_processes_first_items_only(self, tmp_path: Path):
        items = [_item(tmp_path, key="ITEM0001", contents=b"%PDF one"), _item(tmp_path, key="ITEM0002", contents=b"%PDF two")]
        store = FakeMirrorStore()
        converter = FakeDocumentConverter()

        report = await _handler(items, store, converter).handle(SyncMirrors(limit=1))

        assert report.created == 1
        assert [written.source_key for written in store.written] == ["ITEM0001"]
        assert converter.call_count == 1

    async def test_converter_error_on_one_item_is_recorded_and_run_continues(self, tmp_path: Path):
        bad = _item(tmp_path, key="ITEM0001", contents=b"%PDF bad")
        good = _item(tmp_path, key="ITEM0002", contents=b"%PDF good")
        store = FakeMirrorStore()
        converter = FakeDocumentConverter(error_on={bad.pdf_path}, exc=ConversionError("boom"))

        report = await _handler([bad, good], store, converter).handle(SyncMirrors())

        assert report.created == 1
        assert len(report.errors) == 1
        assert report.errors[0].key == "ITEM0001"
        assert "ITEM0002" in store.bodies

    async def test_soft_delete_error_is_recorded_and_run_continues(self, tmp_path: Path):
        keep = _item(tmp_path, key="ITEM0001", contents=b"%PDF keep")
        gone = _item(tmp_path, key="ITEM0002", contents=b"%PDF gone")
        store = _SoftDeleteRaisingStore()
        await _handler([keep, gone], store).handle(SyncMirrors())

        report = await _handler([keep], store).handle(SyncMirrors())

        assert report.archived == 0
        assert [error.key for error in report.errors] == ["ITEM0002"]
        assert report.skipped == 1

    async def test_missing_pdf_is_unavailable_and_run_continues(self, tmp_path: Path):
        readable = _item(tmp_path, key="ITEM0001", contents=b"%PDF one")
        missing = _item_without_pdf(tmp_path, key="GONEPDF1")
        store = FakeMirrorStore()
        converter = FakeDocumentConverter()

        report = await _handler([readable, missing], store, converter).handle(SyncMirrors())

        assert report.unavailable == ("GONEPDF1",)
        assert report.errors == ()
        assert report.created == 1
        assert converter.call_count == 1

    async def test_missing_pdf_does_not_archive_the_items_mirror(self, tmp_path: Path):
        item = _item(tmp_path, key="ITEM0001", contents=b"%PDF one")
        store = FakeMirrorStore()
        await _handler([item], store).handle(SyncMirrors())

        item.pdf_path.unlink()
        report = await _handler([item], store).handle(SyncMirrors())

        assert report.unavailable == ("ITEM0001",)
        assert report.archived == 0
        assert store.index["ITEM0001"].status is MirrorStatus.ACTIVE

    async def test_returns_a_sync_report(self, tmp_path: Path):
        item = _item(tmp_path, key="ITEM0001", contents=b"%PDF one")

        report = await _handler([item], FakeMirrorStore()).handle(SyncMirrors())

        assert isinstance(report, SyncReport)
        assert report.dry_run is False

    async def test_create_threads_converter_assets_to_the_store(self, tmp_path: Path):
        item = _item(tmp_path, key="ITEM0001", contents=b"%PDF one")
        store = FakeMirrorStore()
        converter = FakeDocumentConverter(assets={"_assets/2412.15605/x1.png": b"\x89PNG"})

        await _handler([item], store, converter).handle(SyncMirrors())

        assert store.assets["ITEM0001"] == {"_assets/2412.15605/x1.png": b"\x89PNG"}

    async def test_refresh_with_reusable_body_writes_no_assets(self, tmp_path: Path):
        item = _item(tmp_path, key="ITEM0001", contents=b"%PDF one")
        store = FakeMirrorStore()
        await _handler([item], store, FakeDocumentConverter(assets={"_assets/a.png": b"v1"})).handle(SyncMirrors())

        bumped = SourceItemFactory.build(provider_key="ITEM0001", source_id="ITEM0001", version=2, pdf_path=item.pdf_path)
        refresh_converter = FakeDocumentConverter(assets={"_assets/a.png": b"never"})
        await _handler([bumped], store, refresh_converter).handle(SyncMirrors())

        # The refresh reused the stored body, so no conversion ran and the prior assets stand untouched.
        assert refresh_converter.call_count == 0
        assert store.assets["ITEM0001"] == {"_assets/a.png": b"v1"}

    async def test_written_frontmatter_is_stamped_with_the_injected_clock(self, tmp_path: Path):
        item = _item(tmp_path, key="ITEM0001", contents=b"%PDF one")
        store = FakeMirrorStore()

        await _handler([item], store).handle(SyncMirrors())

        assert store.written[0].synced == _NOW

    async def test_default_clock_stamps_timezone_aware_synced(self, tmp_path: Path):
        item = _item(tmp_path, key="ITEM0001", contents=b"%PDF one")
        store = FakeMirrorStore()
        handler = SyncMirrorsHandler(FakeConnector(items=[item], name=_PROVIDER), FakeDocumentConverter(), store)

        await handler.handle(SyncMirrors())

        assert store.written[0].synced.tzinfo is not None


class TestDryRun:
    async def test_plans_actions_without_writes_or_conversions(self, tmp_path: Path):
        items = [_item(tmp_path, key="ITEM0001", contents=b"%PDF one"), _item(tmp_path, key="ITEM0002", contents=b"%PDF two")]
        store = FakeMirrorStore()
        converter = FakeDocumentConverter()

        report = await _handler(items, store, converter).handle(SyncMirrors(dry_run=True))

        assert report.dry_run is True
        assert store.written == []
        assert store.deleted == []
        assert converter.call_count == 0
        assert report.planned == (
            PlannedAction(key="ITEM0001", title=items[0].title, action=SyncAction.CREATE),
            PlannedAction(key="ITEM0002", title=items[1].title, action=SyncAction.CREATE),
        )

    async def test_plans_a_delete_for_an_orphaned_active_mirror(self, tmp_path: Path):
        store = FakeMirrorStore(index={"GONE0001": MirrorFileFactory.build(source_key="GONE0001")})
        item = _item(tmp_path, key="ITEM0001", contents=b"%PDF one")

        report = await _handler([item], store).handle(SyncMirrors(dry_run=True))

        assert PlannedAction(key="GONE0001", title="(removed at its source)", action=SyncAction.DELETE) in report.planned
        assert store.deleted == []

    async def test_limited_dry_run_plans_first_items_only_and_no_deletes(self, tmp_path: Path):
        items = [_item(tmp_path, key="ITEM0001", contents=b"%PDF one"), _item(tmp_path, key="ITEM0002", contents=b"%PDF two")]
        store = FakeMirrorStore(index={"GONE0001": MirrorFileFactory.build(source_key="GONE0001")})

        report = await _handler(items, store).handle(SyncMirrors(dry_run=True, limit=1))

        assert [row.key for row in report.planned] == ["ITEM0001"]

    async def test_missing_pdf_is_listed_unavailable_and_does_not_abort(self, tmp_path: Path):
        readable = _item(tmp_path, key="ITEM0001", contents=b"%PDF one")
        missing = _item_without_pdf(tmp_path, key="GONEPDF1")

        report = await _handler([readable, missing], FakeMirrorStore()).handle(SyncMirrors(dry_run=True))

        assert report.unavailable == ("GONEPDF1",)
        assert [row.key for row in report.planned] == ["ITEM0001"]


async def _deliver(bus: MessageBus) -> None:
    """Deliver what the bus put in its in-memory outbox, the way the worker does in production."""
    outbox = bus.outbox
    assert isinstance(outbox, MemoryOutbox)
    await Worker(jobs=MemoryJobs(), outbox=outbox, bus=bus, wake=ImmediateWake(), name="test").deliver_once()


class TestDryRunPreview:
    """A dry run lists through the connector's preview, never its real listing."""

    _PENDING = UnresolvedItem(connector="fake", key="VID00001", source_type=SourceType.VIDEO, url="https://video.example/watch/1")

    async def test_the_dry_run_never_lists_and_reports_the_rows_awaiting_resolution(self, tmp_path: Path):
        connector = FakeConnector(items=[_item(tmp_path, key="ITEM0001")], would_resolve=[self._PENDING])
        handler = SyncMirrorsHandler(connector, FakeDocumentConverter(), FakeMirrorStore())

        report = await handler.handle(SyncMirrors(dry_run=True))

        assert connector.listings == 0
        assert [row.key for row in report.planned] == ["ITEM0001"]
        assert report.would_resolve == (self._PENDING,)

    async def test_a_mirror_of_a_row_awaiting_resolution_is_no_orphan(self):
        store = FakeMirrorStore(index={"VID00001": MirrorFileFactory.build(source_key="VID00001", provider="fake")})
        connector = FakeConnector(would_resolve=[self._PENDING])

        report = await SyncMirrorsHandler(connector, FakeDocumentConverter(), store).handle(SyncMirrors(dry_run=True))

        assert report.planned == ()

    async def test_an_applying_run_lists_for_real(self, tmp_path: Path):
        connector = FakeConnector(items=[_item(tmp_path, key="ITEM0001")])

        await SyncMirrorsHandler(connector, FakeDocumentConverter(), FakeMirrorStore()).handle(SyncMirrors())

        assert connector.listings == 1


class TestEvents:
    async def test_started_and_completed_are_drained_through_the_bus(self, tmp_path: Path):
        item = _item(tmp_path, key="ITEM0001", contents=b"%PDF one")
        handler = _handler([item], FakeMirrorStore())
        spy = BusSpy()
        bus = MessageBus({SyncMirrors: handler}, {SyncStarted: [spy], SyncCompleted: [spy]})

        report = await bus.handle(SyncMirrors())
        await _deliver(bus)

        assert [type(event) for event in spy.received] == [SyncStarted, SyncCompleted]
        completed = spy.received[1]
        assert isinstance(completed, SyncCompleted)
        assert completed.report == report
        assert handler.events == []

    async def test_subscriber_observes_the_committed_write(self, tmp_path: Path):
        item = _item(tmp_path, key="ITEM0001", contents=b"%PDF one")
        store = FakeMirrorStore()
        handler = _handler([item], store)
        written_at_event: list[int] = []

        async def probe(event: Event) -> None:
            del event
            written_at_event.append(len(store.written))

        bus = MessageBus({SyncMirrors: handler}, {SyncCompleted: [probe]})

        await bus.handle(SyncMirrors())
        await _deliver(bus)

        # The event arrived after the run finished: the mirror write was already committed.
        assert written_at_event == [1]


class _SoftDeleteRaisingStore(FakeMirrorStore):
    """A mirror store whose `soft_delete` raises, to exercise the archive error path."""

    async def soft_delete(self, key: str) -> bool:
        msg = f"cannot archive {key}"
        raise OSError(msg)


class TestAgeRule:
    """The knowledge base policy's `max_age_years` is enforced at admission: too-old items are refused, reported, never converted."""

    @staticmethod
    def _aged_handler(items: list[SourceItem], store: FakeMirrorStore, *, max_age_years: int | None) -> SyncMirrorsHandler:
        provider = FakeConnector(items=items, name=_PROVIDER)
        return SyncMirrorsHandler(provider, FakeDocumentConverter(), store, clock=lambda: _NOW, max_age_years=max_age_years)

    async def test_an_item_older_than_the_rule_is_refused_and_reported(self, tmp_path: Path) -> None:
        old = SourceItemFactory.build(
            provider_key="OLD1", source_id="OLD1", year=_NOW.year - 3, pdf_path=_write_pdf(tmp_path, "old.pdf", b"%PDF old")
        )
        store = FakeMirrorStore()
        report = await self._aged_handler([old], store, max_age_years=2).handle(SyncMirrors())
        assert report.too_old == ("OLD1",)
        assert report.created == 0
        assert store.written == []

    async def test_an_item_within_the_rule_is_admitted(self, tmp_path: Path) -> None:
        fresh = SourceItemFactory.build(
            provider_key="NEW1", source_id="NEW1", year=_NOW.year - 2, pdf_path=_write_pdf(tmp_path, "new.pdf", b"%PDF new")
        )
        store = FakeMirrorStore()
        report = await self._aged_handler([fresh], store, max_age_years=2).handle(SyncMirrors())
        assert report.too_old == ()
        assert report.created == 1

    async def test_an_unknown_year_passes(self, tmp_path: Path) -> None:
        undated = SourceItemFactory.build(
            provider_key="UND1", source_id="UND1", year=None, pdf_path=_write_pdf(tmp_path, "und.pdf", b"%PDF und")
        )
        report = await self._aged_handler([undated], FakeMirrorStore(), max_age_years=2).handle(SyncMirrors())
        assert report.too_old == ()
        assert report.created == 1

    async def test_an_already_mirrored_old_item_keeps_converging(self, tmp_path: Path) -> None:
        old = SourceItemFactory.build(
            provider_key="OLD2", source_id="OLD2", version=2, year=_NOW.year - 5, pdf_path=_write_pdf(tmp_path, "old2.pdf", b"%PDF old2")
        )
        store = FakeMirrorStore()
        await self._aged_handler([old.model_copy(update={"version": 1})], store, max_age_years=None).handle(SyncMirrors())
        report = await self._aged_handler([old], store, max_age_years=2).handle(SyncMirrors())
        assert report.too_old == ()
        assert report.updated == 1

    async def test_no_rule_admits_everything(self, tmp_path: Path) -> None:
        old = SourceItemFactory.build(
            provider_key="OLD3", source_id="OLD3", year=1999, pdf_path=_write_pdf(tmp_path, "old3.pdf", b"%PDF old3")
        )
        report = await self._aged_handler([old], FakeMirrorStore(), max_age_years=None).handle(SyncMirrors())
        assert report.too_old == ()
        assert report.created == 1

    async def test_the_dry_run_reports_the_refusal_without_planning_it(self, tmp_path: Path) -> None:
        old = SourceItemFactory.build(
            provider_key="OLD4", source_id="OLD4", year=_NOW.year - 3, pdf_path=_write_pdf(tmp_path, "old4.pdf", b"%PDF old4")
        )
        report = await self._aged_handler([old], FakeMirrorStore(), max_age_years=2).handle(SyncMirrors(dry_run=True))
        assert report.too_old == ("OLD4",)
        assert report.planned == ()


class TestOneMirrorPerSource:
    """A sync admits no new mirror for a source another active mirror already holds, matched by URL, arXiv id or DOI."""

    @staticmethod
    def _paper(tmp_path: Path, *, key: str, url: str | None = None, doi: str | None = None, contents: bytes = b"%PDF paper") -> SourceItem:
        return SourceItemFactory.build(
            provider_key=key, source_id=key, version=1, url=url, doi=doi, year=None, pdf_path=_write_pdf(tmp_path, f"{key}.pdf", contents)
        )

    @staticmethod
    def _held(key: str, *, url: str | None = None, doi: str | None = None, status: MirrorStatus = MirrorStatus.ACTIVE) -> FakeMirrorStore:
        return FakeMirrorStore(index={key: MirrorFileFactory.build(source_key=key, provider="other-library", url=url, doi=doi, status=status)})

    async def test_an_item_whose_arxiv_id_another_mirror_holds_is_refused_and_reported(self, tmp_path: Path) -> None:
        store = self._held("LIBRARY1", url="https://arxiv.org/abs/2401.00001")
        converter = FakeDocumentConverter()
        item = self._paper(tmp_path, key="arxiv-2401.00001", url="https://arxiv.org/pdf/2401.00001v3")

        report = await _handler([item], store, converter).handle(SyncMirrors())

        assert report.already_mirrored == {"arxiv-2401.00001": "LIBRARY1"}
        assert report.created == 0
        assert store.written == []
        assert converter.call_count == 0

    async def test_an_item_whose_doi_another_mirror_holds_is_refused_whatever_the_case(self, tmp_path: Path) -> None:
        store = self._held("LIBRARY1", doi="10.1000/Example.42")
        item = self._paper(tmp_path, key="ITEM0001", url="https://example.org/paper", doi="10.1000/example.42")

        report = await _handler([item], store).handle(SyncMirrors())

        assert report.already_mirrored == {"ITEM0001": "LIBRARY1"}

    async def test_a_doi_url_matches_the_doi_another_mirror_holds(self, tmp_path: Path) -> None:
        store = self._held("LIBRARY1", doi="10.1000/example.42")
        item = self._paper(tmp_path, key="ITEM0001", url="https://doi.org/10.1000/example.42")

        report = await _handler([item], store).handle(SyncMirrors())

        assert report.already_mirrored == {"ITEM0001": "LIBRARY1"}

    async def test_an_item_whose_url_another_mirror_holds_is_refused(self, tmp_path: Path) -> None:
        store = self._held("url-0001", url="https://example.org/post")
        item = self._paper(tmp_path, key="ITEM0001", url="https://example.org/post")

        report = await _handler([item], store).handle(SyncMirrors())

        assert report.already_mirrored == {"ITEM0001": "url-0001"}

    async def test_a_removed_mirror_holds_nothing(self, tmp_path: Path) -> None:
        store = self._held("LIBRARY1", url="https://arxiv.org/abs/2401.00001", status=MirrorStatus.REMOVED)
        item = self._paper(tmp_path, key="arxiv-2401.00001", url="https://arxiv.org/abs/2401.00001")

        report = await _handler([item], store).handle(SyncMirrors())

        assert report.already_mirrored == {}
        assert report.created == 1

    async def test_an_item_naming_no_url_and_no_doi_is_admitted(self, tmp_path: Path) -> None:
        store = self._held("LIBRARY1")
        item = self._paper(tmp_path, key="ITEM0001")

        report = await _handler([item], store).handle(SyncMirrors())

        assert report.already_mirrored == {}
        assert report.created == 1

    async def test_two_new_items_of_one_source_in_one_run_write_one_mirror(self, tmp_path: Path) -> None:
        first = self._paper(tmp_path, key="ITEM0001", url="https://arxiv.org/abs/2401.00001", contents=b"%PDF one")
        second = self._paper(tmp_path, key="ITEM0002", url="https://arxiv.org/html/2401.00001v2", contents=b"%PDF two")
        store = FakeMirrorStore()

        report = await _handler([first, second], store).handle(SyncMirrors())

        assert report.created == 1
        assert report.already_mirrored == {"ITEM0002": "ITEM0001"}
        assert [frontmatter.source_key for frontmatter in store.written] == ["ITEM0001"]

    async def test_an_item_with_a_live_mirror_of_its_own_keeps_converging(self, tmp_path: Path) -> None:
        item = self._paper(tmp_path, key="ITEM0001", url="https://arxiv.org/abs/2401.00001")
        store = FakeMirrorStore(
            index={
                "LIBRARY1": MirrorFileFactory.build(source_key="LIBRARY1", provider="other-library", url="https://arxiv.org/abs/2401.00001"),
                "ITEM0001": MirrorFileFactory.build(
                    source_key="ITEM0001", source_version=1, source_hash=pdf_sha256(item.pdf_path), url="https://arxiv.org/abs/2401.00001"
                ),
            }
        )

        report = await _handler([item], store).handle(SyncMirrors())

        assert report.already_mirrored == {}
        assert report.skipped == 1

    async def test_the_dry_run_reports_the_refusal_without_planning_it(self, tmp_path: Path) -> None:
        store = self._held("LIBRARY1", url="https://arxiv.org/abs/2401.00001")
        item = self._paper(tmp_path, key="arxiv-2401.00001", url="https://arxiv.org/abs/2401.00001")

        report = await _handler([item], store).handle(SyncMirrors(dry_run=True))

        assert report.already_mirrored == {"arxiv-2401.00001": "LIBRARY1"}
        assert report.planned == ()

    async def test_the_dry_run_plans_one_create_for_two_new_items_of_one_source(self, tmp_path: Path) -> None:
        first = self._paper(tmp_path, key="ITEM0001", url="https://arxiv.org/abs/2401.00001", contents=b"%PDF one")
        second = self._paper(tmp_path, key="ITEM0002", url="https://arxiv.org/abs/2401.00001", contents=b"%PDF two")

        report = await _handler([first, second], FakeMirrorStore()).handle(SyncMirrors(dry_run=True))

        assert [row.key for row in report.planned] == ["ITEM0001"]
        assert report.already_mirrored == {"ITEM0002": "ITEM0001"}
