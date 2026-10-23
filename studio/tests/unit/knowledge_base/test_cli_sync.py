"""Unit tests for the `memory sync` CLI command (`src/studio/knowledge_base/cli.py`).

Covers `memory sync` / `memory sync --dry-run`. Driven by
:class:`typer.testing.CliRunner` with the container *factory* injected through
`ctx.obj` (the established seam — no monkeypatching): the fake factory returns a
fake async container serving a **real** :class:`SyncMirrorsHandler` over the
in-memory fakes, routed on a **real** :class:`~core.cqrs.bus.MessageBus` — so the
command's resolve → dispatch → print → exit-code path runs the genuine reconcile
loop with no Zotero, knowledge base, Azure, or network. Items are seeded with `pdf_path`
pointing at real `tmp_path` PDFs so hashing inside the loop is genuine.
"""

from __future__ import annotations

import json

from pathlib import Path
from typing import TYPE_CHECKING

import pytest

from core.cqrs.contracts import JobQueries, MemoryJobs, MessageBus
from engram.indexing.contracts import KnowledgeBaseReader
from engram.kb.contracts import KnowledgeBaseSchema
from engram.retrieval.domain import SqlResult
from engram.sources.contracts import (
    ConversionError,
    MirrorStatus,
    ScanWatchList,
    ScanWatchListHandler,
    SourceQueue,
    SyncMirrors,
    SyncMirrorsHandler,
    SyncReport,
    WatchListFile,
)
from engram.sources.service.sync import pdf_sha256
from engram.testing.factories.mirror import MirrorFileFactory
from engram.testing.factories.source_item import SourceItemFactory
from engram.testing.fakes.bus_spy import RecordingCommandHandler
from engram.testing.fakes.connector import FakeConnector
from engram.testing.fakes.conversion import FakeDocumentConverter
from engram.testing.fakes.gateway import FakeSqlGateway
from engram.testing.fakes.knowledge_base_reader import FakeKnowledgeBaseReader
from engram.testing.fakes.mirror import FakeMirrorStore
from engram.testing.fakes.watch_list import FakeScanner
from engram.testing.specs import research_spec
from typer.testing import CliRunner

from studio.knowledge_base.cli import app
from studio.knowledge_base.settings import KnowledgeBaseSettings
from studio.knowledge_base.watch_list import WATCH_LIST_PATH
from studio.knowledge_base.wiring.contracts import AdminSqlGateway

if TYPE_CHECKING:
    from engram.sources.contracts import SourceItem

runner = CliRunner()
_CONNECTOR = "example-library"  # MirrorFileFactory's own pinned connector name, matched explicitly on the fake below


@pytest.fixture(autouse=True)
def _ambient_knowledge_base(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Every test in this file runs inside a real knowledge base by cwd, its spec the bundled research default verbatim.

    A test that names its own `--knowledge-base` and chdirs elsewhere is unaffected — its own
    `monkeypatch.chdir` runs after this one.
    """
    root = tmp_path / "ambient-knowledge-base"
    root.mkdir()
    (root / "knowledge-base-schema.json").write_text(research_spec().model_dump_json(), encoding="utf-8")
    monkeypatch.chdir(root)


def _item(tmp_path: Path, *, key: str, version: int = 1, contents: bytes = b"%PDF data") -> SourceItem:
    """A SourceItem whose `pdf_path` is a real file under `tmp_path` (so hashing is real)."""
    pdf = tmp_path / f"{key}.pdf"
    pdf.write_bytes(contents)
    return SourceItemFactory.build(provider_key=key, source_id=key, version=version, pdf_path=pdf)


def _item_without_pdf(tmp_path: Path, *, key: str, version: int = 1) -> SourceItem:
    """A SourceItem whose `pdf_path` points at a file that does not exist (hashing would raise)."""
    return SourceItemFactory.build(provider_key=key, source_id=key, version=version, pdf_path=tmp_path / f"{key}-missing.pdf")


def _identity_gateway() -> FakeSqlGateway:
    """A gateway answering the identity read with no revision and no index history."""
    return FakeSqlGateway(
        routed={
            "to_regclass": SqlResult(rows=({"present": False},), columns=("present",)),
            "index_fingerprint": SqlResult(rows=(), columns=("indexed_at",)),
        }
    )


class FakeContainer:
    """Fake async DI container serving the two keys the sync dispatch resolves.

    `handler` is whatever the dispatch's registration-triggering resolution should
    return — the real handler in the flow tests, a recording one in the plumbing tests.
    """

    def __init__(self, handler: object, bus: MessageBus) -> None:
        self._handler = handler
        self._bus = bus
        self._gateway = _identity_gateway()
        self._reader: KnowledgeBaseReader = FakeKnowledgeBaseReader()
        self._jobs = MemoryJobs()

    async def get(self, key: type) -> object:
        if key is MessageBus:
            return self._bus
        if key is SyncMirrorsHandler:
            return self._handler
        if key is AdminSqlGateway:
            return self._gateway
        if key is KnowledgeBaseReader:
            return self._reader
        if key is JobQueries:
            return self._jobs
        msg = f"FakeContainer has no registration for {key!r}"
        raise KeyError(msg)


class SyncContainerFactory:
    """Zero-arg `ctx.obj` factory: a real handler over fakes on a real, pre-routed bus."""

    def __init__(
        self, *, repo: FakeConnector, converter: FakeDocumentConverter, store: FakeMirrorStore, max_age_years: int | None = None
    ) -> None:
        self.handler = SyncMirrorsHandler(repo, converter, store, max_age_years=max_age_years)
        self._bus = MessageBus({SyncMirrors: self.handler}, {})

    def __call__(self) -> FakeContainer:
        return FakeContainer(self.handler, self._bus)


def _factory(
    items: list[SourceItem], store: FakeMirrorStore, converter: FakeDocumentConverter | None = None, *, max_age_years: int | None = None
) -> SyncContainerFactory:
    return SyncContainerFactory(
        repo=FakeConnector(items=items, name=_CONNECTOR),
        converter=converter or FakeDocumentConverter(),
        store=store,
        max_age_years=max_age_years,
    )


class TestSync:
    def test_creates_mirrors_and_reports_counts(self, tmp_path: Path):
        items = [_item(tmp_path, key="ITEM0001"), _item(tmp_path, key="ITEM0002")]
        converter = FakeDocumentConverter()
        store = FakeMirrorStore()

        result = runner.invoke(app, ["sync"], obj=_factory(items, store, converter))

        assert result.exit_code == 0
        assert "created" in result.stdout.lower()
        assert "2" in result.stdout
        assert len(store.written) == 2
        assert converter.call_count == 2

    def test_skips_unchanged_items_without_converting(self, tmp_path: Path):
        item = _item(tmp_path, key="ITEM0001")
        store = FakeMirrorStore(
            index={
                "ITEM0001": MirrorFileFactory.build(
                    source_key="ITEM0001", source_version=item.version, source_hash=pdf_sha256(item.pdf_path)
                )
            }
        )
        converter = FakeDocumentConverter()

        result = runner.invoke(app, ["sync"], obj=_factory([item], store, converter))

        assert result.exit_code == 0
        assert "skipped" in result.stdout.lower()
        assert converter.call_count == 0
        assert store.written == []

    def test_converter_error_reports_and_exits_one(self, tmp_path: Path):
        bad = _item(tmp_path, key="ITEM0001")
        converter = FakeDocumentConverter(error_on={bad.pdf_path}, exc=ConversionError("boom"))
        store = FakeMirrorStore()

        result = runner.invoke(app, ["sync"], obj=_factory([bad], store, converter))

        assert result.exit_code == 1
        assert "ITEM0001" in result.stdout

    def test_limit_one_processes_first_item_only_and_skips_orphan_archiving(self, tmp_path: Path):
        items = [_item(tmp_path, key="ITEM0001"), _item(tmp_path, key="ITEM0002")]
        converter = FakeDocumentConverter()
        store = FakeMirrorStore(index={"GONE0001": MirrorFileFactory.build(source_key="GONE0001")})

        result = runner.invoke(app, ["sync", "--limit", "1"], obj=_factory(items, store, converter))

        assert result.exit_code == 0
        assert len(store.written) == 1
        assert store.written[0].source_key == "ITEM0001"
        assert converter.call_count == 1
        assert store.deleted == []
        assert store.index["GONE0001"].status is MirrorStatus.ACTIVE

    def test_missing_pdf_is_listed_unavailable_and_exits_zero(self, tmp_path: Path):
        items = [_item(tmp_path, key="ITEM0001"), _item_without_pdf(tmp_path, key="GONEPDF1")]

        result = runner.invoke(app, ["sync"], obj=_factory(items, FakeMirrorStore()))

        assert result.exit_code == 0
        assert result.exception is None
        assert "unavailable" in result.stdout.lower()
        assert "GONEPDF1" in result.stdout

    def test_an_item_older_than_the_policy_is_listed_too_old_and_not_written(self, tmp_path: Path):
        old = _item(tmp_path, key="OLD00001").model_copy(update={"year": 1999})
        store = FakeMirrorStore()

        result = runner.invoke(app, ["sync"], obj=_factory([old], store, max_age_years=2))

        assert result.exit_code == 0
        assert result.exception is None
        assert "max_age_years" in result.stdout
        assert "OLD00001" in result.stdout
        assert store.written == []

    def test_an_item_another_mirror_holds_is_listed_with_that_mirror_and_not_written(self, tmp_path: Path):
        held = MirrorFileFactory.build(source_key="LIBRARY1", provider="other-library", url="https://arxiv.org/abs/2401.00001")
        store = FakeMirrorStore(index={"LIBRARY1": held})
        item = _item(tmp_path, key="ITEM0001").model_copy(update={"url": "https://arxiv.org/abs/2401.00001", "year": None})

        result = runner.invoke(app, ["sync"], obj=_factory([item], store))

        assert result.exit_code == 0
        assert result.exception is None
        assert "Not admitted, already mirrored (1): ITEM0001 as LIBRARY1" in result.stdout
        assert store.written == []


class TestDryRun:
    def test_writes_nothing_and_never_converts(self, tmp_path: Path):
        items = [_item(tmp_path, key="ITEM0001"), _item(tmp_path, key="ITEM0002")]
        converter = FakeDocumentConverter()
        store = FakeMirrorStore()

        result = runner.invoke(app, ["sync", "--dry-run"], obj=_factory(items, store, converter))

        assert result.exit_code == 0
        assert store.written == []
        assert store.deleted == []
        assert converter.call_count == 0

    def test_lists_planned_actions_per_item(self, tmp_path: Path):
        result = runner.invoke(app, ["sync", "--dry-run"], obj=_factory([_item(tmp_path, key="ITEM0001")], FakeMirrorStore()))

        assert result.exit_code == 0
        assert "ITEM0001" in result.stdout
        assert "create" in result.stdout.lower()

    def test_limit_one_renders_a_single_planned_row(self, tmp_path: Path):
        items = [_item(tmp_path, key="ITEM0001"), _item(tmp_path, key="ITEM0002")]
        converter = FakeDocumentConverter()

        result = runner.invoke(app, ["sync", "--dry-run", "--limit", "1"], obj=_factory(items, FakeMirrorStore(), converter))

        assert result.exit_code == 0
        assert "ITEM0001" in result.stdout
        assert "ITEM0002" not in result.stdout
        assert converter.call_count == 0

    def test_shows_planned_delete_for_orphaned_active_mirror(self, tmp_path: Path):
        store = FakeMirrorStore(index={"GONE0001": MirrorFileFactory.build(source_key="GONE0001")})
        converter = FakeDocumentConverter()

        result = runner.invoke(app, ["sync", "--dry-run"], obj=_factory([_item(tmp_path, key="ITEM0001")], store, converter))

        assert result.exit_code == 0
        assert "GONE0001" in result.stdout
        assert "delete" in result.stdout.lower()
        assert store.deleted == []
        assert converter.call_count == 0

    def test_missing_pdf_is_listed_distinctly_and_does_not_abort(self, tmp_path: Path):
        items = [_item(tmp_path, key="ITEM0001"), _item_without_pdf(tmp_path, key="GONEPDF1")]

        result = runner.invoke(app, ["sync", "--dry-run"], obj=_factory(items, FakeMirrorStore()))

        assert result.exit_code == 0
        assert result.exception is None
        assert "GONEPDF1" in result.stdout
        assert "unavailable" in result.stdout.lower()
        assert "ITEM0001" in result.stdout
        assert "create" in result.stdout.lower()


class WatchListContainer(FakeContainer):
    """A fake container that also serves the knowledge base's schema and folder, so the sync reaches its watch list."""

    def __init__(self, handler: object, bus: MessageBus, root: Path) -> None:
        super().__init__(handler, bus)
        self._root = root

    async def get(self, key: type) -> object:
        if key is KnowledgeBaseSchema:
            return research_spec()
        if key is KnowledgeBaseSettings:
            return KnowledgeBaseSettings(path=self._root)
        return await super().get(key)


class TestWatchListOnDryRun:
    """The watch list of a knowledge base whose two rows an `x` scanner and an `xml` scanner read."""

    def _container(self, root: Path, scanner: FakeScanner) -> WatchListContainer:
        watch_list = WatchListFile(root / WATCH_LIST_PATH)
        watch_list.add("x", "@someone", max_items=5)
        watch_list.add("xml", "https://example.org/feed.xml")
        queue = SourceQueue(root / "_meta" / "source-queue.yaml", connectors=())
        scan = ScanWatchListHandler(watch_list, {"x": scanner, "xml": scanner}, queue)
        sync = RecordingCommandHandler(result=SyncReport(dry_run=True))
        return WatchListContainer(sync, MessageBus({SyncMirrors: sync, ScanWatchList: scan}, {}), root)

    def test_a_dry_run_lists_each_row_and_calls_no_scanner(self) -> None:
        root = Path.cwd()
        (root / "_meta").mkdir(exist_ok=True)
        scanner = FakeScanner()
        container = self._container(root, scanner)
        watch_list_text = (root / WATCH_LIST_PATH).read_text(encoding="utf-8")

        result = runner.invoke(app, ["sync", "--dry-run"], obj=lambda: container)

        assert result.exit_code == 0, result.stdout
        assert scanner.since == []
        assert "x @someone" in result.stdout
        assert "xml https://example.org/feed.xml" in result.stdout
        assert (root / WATCH_LIST_PATH).read_text(encoding="utf-8") == watch_list_text
        assert not (root / "_meta" / "source-queue.yaml").exists()

    def test_a_real_sync_scans_each_row(self) -> None:
        root = Path.cwd()
        (root / "_meta").mkdir(exist_ok=True)
        scanner = FakeScanner()
        container = self._container(root, scanner)

        result = runner.invoke(app, ["sync"], obj=lambda: container)

        assert result.exit_code == 0, result.stdout
        assert len(scanner.since) == 2
        assert "Scanned the watch list" in result.stdout


class TestDispatch:
    def test_flags_are_plumbed_to_the_command(self):
        recording = RecordingCommandHandler(result=SyncReport(dry_run=True))
        container = FakeContainer(recording, MessageBus({SyncMirrors: recording}, {}))

        result = runner.invoke(app, ["sync", "--dry-run", "--limit", "3"], obj=lambda: container)

        assert result.exit_code == 0
        assert recording.handled == [SyncMirrors(dry_run=True, limit=3)]

    def test_default_flags_apply_the_full_library(self):
        recording = RecordingCommandHandler(result=SyncReport())
        container = FakeContainer(recording, MessageBus({SyncMirrors: recording}, {}))

        result = runner.invoke(app, ["sync"], obj=lambda: container)

        assert result.exit_code == 0
        assert recording.handled == [SyncMirrors(dry_run=False, limit=None)]

    def test_an_explicit_knowledge_base_flag_is_never_refused_from_inside_another_knowledge_base(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        named = tmp_path / "named"
        (named / "_meta").mkdir(parents=True)
        (named / "knowledge-base-schema.json").write_text(research_spec().model_dump_json(), encoding="utf-8")
        cwd = tmp_path / "alpha-cwd"
        cwd.mkdir()
        (cwd / "knowledge-base-schema.json").write_text(json.dumps({"slug": "alpha", "page_types": [{"page_type": "note"}]}), encoding="utf-8")
        monkeypatch.chdir(cwd)
        recording = RecordingCommandHandler(result=SyncReport())
        container = FakeContainer(recording, MessageBus({SyncMirrors: recording}, {}))

        result = runner.invoke(app, ["sync", "--knowledge-base", str(named)], obj=lambda: container)

        assert result.exit_code == 0, result.stdout
        assert "two knowledge bases in play." not in result.stderr


class TestHelp:
    def test_sync_help_exits_zero_and_names_the_flags(self):
        result = runner.invoke(app, ["sync", "--help"])

        assert result.exit_code == 0
        assert "dry-run" in result.stdout
        assert "limit" in result.stdout
