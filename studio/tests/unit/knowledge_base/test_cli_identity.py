"""Every knowledge-base-touching command opens by saying which knowledge base, at what root, against which database, last indexed when."""

from __future__ import annotations

import json

from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING

import pytest

from core.cqrs.contracts import JobQueries, MemoryJobs, MessageBus
from engram.identity import disk_content_hashes
from engram.indexing.contracts import IndexKnowledgeBase, IndexReport, KnowledgeBaseReader
from engram.init.contracts import code_head_revision
from engram.retrieval.domain import SqlResult
from engram.retrieval.infrastructure.gateway import ReadOnlySqlGateway
from engram.testing.fakes.bus_spy import RecordingCommandHandler
from engram.testing.fakes.embedding import FakeEmbedding
from engram.testing.fakes.gateway import FakeSqlGateway
from engram.testing.fakes.knowledge_base_reader import FakeKnowledgeBaseReader
from engram.testing.knowledge_base_builder import KnowledgeBaseBuilder
from engram.testing.specs import minimal_spec
from sqlalchemy.ext.asyncio import create_async_engine
from typer.testing import CliRunner

from studio.knowledge_base.cli import _read_surfaces, app
from studio.knowledge_base.cli_support import announce_knowledge_base
from studio.knowledge_base.settings import fetch_application_settings
from studio.knowledge_base.wiring.contracts import AdminSqlGateway
from studio.knowledge_base.wiring.indexing import ingestion_indexer_version

if TYPE_CHECKING:
    from collections.abc import Iterator

    from engram.indexing.contracts import RawDocument
    from engram.kb.contracts import KnowledgeBaseSchema

runner = CliRunner()
INDEXED = datetime(2026, 8, 26, 23, 50, tzinfo=UTC)
HEAD = code_head_revision()


def _current_indexer_version() -> str:
    """The indexer version an index under the test's settings stores, the one the command compares against."""
    settings = fetch_application_settings()
    return ingestion_indexer_version(settings.infrastructure.azure_openai, settings.ingestion)


def _local(when: datetime) -> str:
    """The local rendering of `when`, the same call `_db_line` makes, never a literal hour."""
    return when.astimezone().strftime("%Y-%m-%d %H:%M %Z")


@pytest.fixture(autouse=True)
def _schema_at_head(monkeypatch: pytest.MonkeyPatch) -> None:
    """Default every test's `index` preflight to an at-head DB. A real check needs a live database."""
    from studio.knowledge_base import cli as cli_module

    monkeypatch.setattr(cli_module, "assert_schema_at_head", lambda _db_url: None)


def _gateway(
    *,
    revision: str | None = HEAD,
    indexed_at: datetime | None = INDEXED,
    indexer_version: str | None = None,
    manifest: dict[str, str] | None = None,
) -> FakeSqlGateway:
    present = revision is not None
    fingerprint = {"indexed_at": indexed_at, "indexer_version": indexer_version if indexer_version is not None else _current_indexer_version()}
    return FakeSqlGateway(
        routed={
            "to_regclass": SqlResult(rows=({"present": present},), columns=("present",)),
            "alembic_version": SqlResult(rows=({"version_num": revision},) if present else (), columns=("version_num",)),
            "index_fingerprint": SqlResult(
                rows=(fingerprint,) if indexed_at is not None else (), columns=("indexed_at", "indexer_version")
            ),
            "index_manifest": SqlResult(
                rows=tuple({"path": path, "content_hash": digest} for path, digest in (manifest or {}).items()),
                columns=("path", "content_hash"),
            ),
        }
    )


class FakeContainer:
    def __init__(self, bus: MessageBus, gateway: FakeSqlGateway, reader: KnowledgeBaseReader) -> None:
        self._bus = bus
        self._gateway = gateway
        self._reader = reader
        self._jobs = MemoryJobs()

    async def get(self, key: type) -> object:
        if key is MessageBus:
            return self._bus
        if key is AdminSqlGateway:
            return self._gateway
        if key is KnowledgeBaseReader:
            return self._reader
        if key is JobQueries:
            return self._jobs
        msg = f"FakeContainer has no registration for {key!r}"
        raise KeyError(msg)


class FakeContainerFactory:
    def __init__(self, gateway: FakeSqlGateway | None = None, reader: KnowledgeBaseReader | None = None) -> None:
        self.handler = RecordingCommandHandler(result=IndexReport(pages=3, sections=7, claims=5, edges=4))
        self._bus = MessageBus({IndexKnowledgeBase: self.handler}, {})
        self._gateway = gateway or _gateway()
        self._reader = reader if reader is not None else FakeKnowledgeBaseReader()

    def __call__(self) -> FakeContainer:
        return FakeContainer(self._bus, self._gateway, self._reader)


def _knowledge_base(root: Path, slug: str) -> Path:
    root.mkdir(parents=True, exist_ok=True)
    (root / "knowledge-base-schema.json").write_text(json.dumps({"slug": slug, "page_types": [{"page_type": "note"}]}), encoding="utf-8")
    return root


class TestIndexAnnounces:
    def test_the_first_two_lines_name_the_knowledge_base_and_the_database_before_any_count(self, tmp_path: Path):
        root = _knowledge_base(tmp_path / "alpha", "alpha")

        result = runner.invoke(app, ["index", "--knowledge-base", str(root)], obj=FakeContainerFactory())

        lines = result.stdout.splitlines()
        assert result.exit_code == 0
        assert lines[0] == f"knowledge base alpha at {root}"
        assert lines[1] == f"db db/knowledge_bases at {HEAD} (code expects {HEAD}), last indexed {_local(INDEXED)}, ok, nothing changed since"
        assert "3 pages" in lines[2]

    def test_a_never_indexed_knowledge_base_says_never(self, tmp_path: Path):
        root = _knowledge_base(tmp_path / "alpha", "alpha")

        result = runner.invoke(app, ["index", "--knowledge-base", str(root)], obj=FakeContainerFactory(_gateway(indexed_at=None)))

        assert (
            result.stdout.splitlines()[1]
            == f"db db/knowledge_bases at {HEAD} (code expects {HEAD}), last indexed never, 0 files awaiting index"
        )

    def test_a_database_with_no_revision_says_so(self, tmp_path: Path):
        root = _knowledge_base(tmp_path / "alpha", "alpha")

        result = runner.invoke(app, ["index", "--knowledge-base", str(root)], obj=FakeContainerFactory(_gateway(revision=None)))

        assert f"at no revision (code expects {HEAD})" in result.stdout.splitlines()[1]


class TestStatusAnnounces:
    def test_status_opens_with_the_identity(self, tmp_path: Path):
        root = _knowledge_base(tmp_path / "alpha", "alpha")

        result = runner.invoke(app, ["status", "--knowledge-base", str(root)], obj=FakeContainerFactory())

        lines = result.stdout.splitlines()
        assert lines[0] == f"knowledge base alpha at {root}"
        assert lines[1].startswith(f"db db/knowledge_bases at {HEAD}")


class TestFileOnlyCommandsAnnounceTheKnowledgeBaseOnly:
    def test_feeds_list_prints_the_knowledge_base_line_and_no_database_line(self, tmp_path: Path):
        root = _knowledge_base(tmp_path / "alpha", "alpha")

        result = runner.invoke(app, ["watch-list", "list", "--knowledge-base", str(root)])

        lines = result.stdout.splitlines()
        assert lines[0] == f"knowledge base alpha at {root}"
        assert not any(line.startswith("db ") for line in lines)


class TestAnnounceKnowledgeBaseResolvesTheRoot:
    def test_a_relative_root_prints_the_resolved_absolute_path(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        monkeypatch.chdir(tmp_path)

        announce_knowledge_base(Path())

        line = capsys.readouterr().out.strip()
        assert line.endswith(str(tmp_path))


class TestTheTimeCarriesItsZone:
    """`_db_line` renders `last_indexed_at` in local time with its zone abbreviation, never a literal hour."""

    def test_the_local_time_and_zone_abbreviation_appear(self, tmp_path: Path):
        root = _knowledge_base(tmp_path / "alpha", "alpha")

        result = runner.invoke(app, ["status", "--knowledge-base", str(root)], obj=FakeContainerFactory())

        assert f"last indexed {_local(INDEXED)}," in result.stdout.splitlines()[1]

    def test_a_naive_index_time_is_taken_as_utc(self, tmp_path: Path):
        root = _knowledge_base(tmp_path / "alpha", "alpha")
        naive = INDEXED.replace(tzinfo=None)

        result = runner.invoke(app, ["status", "--knowledge-base", str(root)], obj=FakeContainerFactory(_gateway(indexed_at=naive)))

        assert f"last indexed {_local(INDEXED)}," in result.stdout.splitlines()[1]


class TestTheChangedCountTail:
    """The db line's tail names how many files moved since the manifest, in the wording the state calls for."""

    def test_two_files_changed_since_the_last_index(self, tmp_path: Path):
        root = _knowledge_base(tmp_path / "alpha", "alpha")
        before = FakeKnowledgeBaseReader(units=[("Units/a.md", "alpha"), ("Units/b.md", "beta")])
        after = FakeKnowledgeBaseReader(units=[("Units/a.md", "alpha edited"), ("Units/b.md", "beta"), ("Units/new.md", "new")])
        gateway = _gateway(manifest=disk_content_hashes(before))

        result = runner.invoke(app, ["status", "--knowledge-base", str(root)], obj=FakeContainerFactory(gateway, reader=after))

        assert result.stdout.splitlines()[1].endswith("2 files changed since")

    def test_one_file_changed_since_the_last_index(self, tmp_path: Path):
        root = _knowledge_base(tmp_path / "alpha", "alpha")
        before = FakeKnowledgeBaseReader(units=[("Units/a.md", "alpha")])
        after = FakeKnowledgeBaseReader(units=[("Units/a.md", "alpha edited")])
        gateway = _gateway(manifest=disk_content_hashes(before))

        result = runner.invoke(app, ["status", "--knowledge-base", str(root)], obj=FakeContainerFactory(gateway, reader=after))

        assert result.stdout.splitlines()[1].endswith("1 file changed since")

    def test_nothing_changed_since_the_last_index(self, tmp_path: Path):
        root = _knowledge_base(tmp_path / "alpha", "alpha")
        reader = FakeKnowledgeBaseReader(units=[("Units/a.md", "alpha")])
        gateway = _gateway(manifest=disk_content_hashes(reader))

        result = runner.invoke(app, ["status", "--knowledge-base", str(root)], obj=FakeContainerFactory(gateway, reader=reader))

        assert result.stdout.splitlines()[1].endswith("nothing changed since")

    def test_never_indexed_reports_files_awaiting_index(self, tmp_path: Path):
        root = _knowledge_base(tmp_path / "alpha", "alpha")
        reader = FakeKnowledgeBaseReader(units=[("Units/a.md", "alpha"), ("Units/b.md", "beta")])
        gateway = _gateway(indexed_at=None, manifest={})

        result = runner.invoke(app, ["status", "--knowledge-base", str(root)], obj=FakeContainerFactory(gateway, reader=reader))

        assert result.stdout.splitlines()[1].endswith("2 files awaiting index")

    def test_never_indexed_with_one_file_uses_the_singular(self, tmp_path: Path):
        root = _knowledge_base(tmp_path / "alpha", "alpha")
        reader = FakeKnowledgeBaseReader(units=[("Units/a.md", "alpha")])
        gateway = _gateway(indexed_at=None, manifest={})

        result = runner.invoke(app, ["status", "--knowledge-base", str(root)], obj=FakeContainerFactory(gateway, reader=reader))

        assert result.stdout.splitlines()[1].endswith("1 file awaiting index")




class TestTheTailSaysWhenTheNextIndexRebuildsEverything:
    """A stored indexer version that differs from the one the code and settings produce now makes the next index a full rebuild."""

    def test_nothing_changed_when_the_stored_indexer_version_is_the_current_one(self, tmp_path: Path):
        root = _knowledge_base(tmp_path / "alpha", "alpha")
        reader = FakeKnowledgeBaseReader(units=[("Units/a.md", "alpha")])
        gateway = _gateway(manifest=disk_content_hashes(reader), indexer_version=_current_indexer_version())

        result = runner.invoke(app, ["status", "--knowledge-base", str(root)], obj=FakeContainerFactory(gateway, reader=reader))

        assert result.stdout.splitlines()[1].endswith(", nothing changed since")

    def test_changed_files_under_the_current_indexer_version_name_only_the_files(self, tmp_path: Path):
        root = _knowledge_base(tmp_path / "alpha", "alpha")
        before = FakeKnowledgeBaseReader(units=[("Units/a.md", "alpha")])
        after = FakeKnowledgeBaseReader(units=[("Units/a.md", "alpha edited")])
        gateway = _gateway(manifest=disk_content_hashes(before), indexer_version=_current_indexer_version())

        result = runner.invoke(app, ["status", "--knowledge-base", str(root)], obj=FakeContainerFactory(gateway, reader=after))

        assert result.stdout.splitlines()[1].endswith(", 1 file changed since")

    def test_a_changed_indexer_version_says_the_next_index_rebuilds_everything(self, tmp_path: Path):
        root = _knowledge_base(tmp_path / "alpha", "alpha")
        reader = FakeKnowledgeBaseReader(units=[("Units/a.md", "alpha")])
        gateway = _gateway(manifest=disk_content_hashes(reader), indexer_version="0+1-2")

        result = runner.invoke(app, ["status", "--knowledge-base", str(root)], obj=FakeContainerFactory(gateway, reader=reader))

        assert result.stdout.splitlines()[1].endswith(", the indexer or its chunk settings changed since, the next index rebuilds everything")

    def test_changed_files_and_a_changed_indexer_version_are_both_stated(self, tmp_path: Path):
        root = _knowledge_base(tmp_path / "alpha", "alpha")
        before = FakeKnowledgeBaseReader(units=[("Units/a.md", "alpha"), ("Units/b.md", "beta")])
        after = FakeKnowledgeBaseReader(units=[("Units/a.md", "alpha edited"), ("Units/b.md", "beta edited")])
        gateway = _gateway(manifest=disk_content_hashes(before), indexer_version="0+1-2")

        result = runner.invoke(app, ["status", "--knowledge-base", str(root)], obj=FakeContainerFactory(gateway, reader=after))

        assert result.stdout.splitlines()[1].endswith(
            ", 2 files and the indexer or its chunk settings changed since, the next index rebuilds everything"
        )


class _UnreadableReader(FakeKnowledgeBaseReader):
    """A reader whose walk fails on one note, the disk's own error and never the database's."""

    def unit_documents(self) -> Iterator[RawDocument]:
        raise PermissionError(13, "Permission denied", "Units/secret.md")


class TestAFileErrorIsNotADatabaseError:
    """The disk walk behind the changed count runs outside the unreachable guard."""

    def test_an_unreadable_note_is_never_reported_as_an_unreachable_database(self, tmp_path: Path):
        root = _knowledge_base(tmp_path / "alpha", "alpha")

        result = runner.invoke(app, ["status", "--knowledge-base", str(root)], obj=FakeContainerFactory(reader=_UnreadableReader()))

        assert result.exit_code != 0
        assert "unreachable" not in result.stdout
        assert isinstance(result.exception, PermissionError)


def _registered_knowledge_base(root: Path, *, pages: int) -> KnowledgeBaseSchema:
    """A knowledge base on disk whose slug and collection are its folder name, holding `pages` notes."""
    spec = minimal_spec()
    spec = spec.model_copy(update={"slug": root.name, "retrieval": spec.retrieval.model_copy(update={"collection": root.name})})
    builder = KnowledgeBaseBuilder(root)
    builder.add_knowledge_base_schema(spec)
    for index in range(pages):
        builder.add_unit_document("Notes", f"note-{index}", f"# {root.name} {index}\n")
    return spec


def _private_knowledge_base(root: Path, *, collection: str) -> KnowledgeBaseSchema:
    """A knowledge base on disk whose OWN schema keeps it out of a read over several."""
    spec = minimal_spec()
    spec = spec.model_copy(
        update={"slug": root.name, "retrieval": spec.retrieval.model_copy(update={"collection": collection, "private": True})}
    )
    builder = KnowledgeBaseBuilder(root)
    builder.add_knowledge_base_schema(spec)
    builder.add_unit_document("Notes", "note-0", f"# {root.name}\n")
    return spec


def _shared_knowledge_base(root: Path, *, collection: str) -> KnowledgeBaseSchema:
    """A knowledge base on disk declaring `collection`, which may be another one's."""
    spec = minimal_spec()
    spec = spec.model_copy(update={"slug": root.name, "retrieval": spec.retrieval.model_copy(update={"collection": collection})})
    builder = KnowledgeBaseBuilder(root)
    builder.add_knowledge_base_schema(spec)
    builder.add_unit_document("Notes", "note-0", f"# {root.name}\n")
    return spec


def _registry(settings_path: Path, **roots: Path) -> None:
    """Write a settings file registering `roots` by name, the shape `read_registry` parses."""
    settings_path.write_text(json.dumps({"knowledge_bases": {name: {"path": str(root)} for name, root in roots.items()}}), encoding="utf-8")


class TestEveryRegisteredKnowledgeBaseReadsItsOwnFiles:
    """`_read_surfaces` binds one route per knowledge base: its own schema, its own collection, its own source tree.

    The freshness count is the tell. Each root here holds a different number of notes and the stored
    manifest is empty, so a stamp that counted another knowledge base's files would report the wrong
    number — the failure mode a single shared reader would produce silently.
    """

    async def test_each_route_measures_the_root_it_was_registered_with(self, tmp_path: Path):
        active = tmp_path / "repo-docs"
        other = tmp_path / "research"
        spec = _registered_knowledge_base(active, pages=1)
        _registered_knowledge_base(other, pages=2)
        settings_path = tmp_path / "settings.json"
        _registry(settings_path, **{"repo-docs": active, "research": other})
        notes: list[str] = []
        engine = create_async_engine("postgresql+asyncpg://reader:secret@localhost:5432/unused")

        try:
            surfaces = _read_surfaces(
                active,
                spec,
                settings_path,
                ReadOnlySqlGateway(engine),
                _gateway(manifest={}),
                FakeEmbedding(),
                active_hybrid=False,
                notes=notes,
                rerank=None,
            )

            assert [surface.name for surface in surfaces] == ["repo-docs", "research"]
            assert [surface.collection for surface in surfaces] == ["repo-docs", "research"]
            assert notes == []
            stamps = [await surface.stamp() for surface in surfaces if surface.stamp is not None]
            assert [(stamp.slug, stamp.changed_since) for stamp in stamps] == [("repo-docs", 1), ("research", 2)]
        finally:
            await engine.dispose()

    async def test_a_broken_registry_row_is_reported_and_never_searched(self, tmp_path: Path):
        active = tmp_path / "repo-docs"
        spec = _registered_knowledge_base(active, pages=1)
        settings_path = tmp_path / "settings.json"
        _registry(settings_path, **{"repo-docs": active, "research": tmp_path / "moved-away"})
        notes: list[str] = []
        engine = create_async_engine("postgresql+asyncpg://reader:secret@localhost:5432/unused")

        try:
            surfaces = _read_surfaces(
                active,
                spec,
                settings_path,
                ReadOnlySqlGateway(engine),
                _gateway(manifest={}),
                FakeEmbedding(),
                active_hybrid=False,
                notes=notes,
                rerank=None,
            )
        finally:
            await engine.dispose()

        assert [surface.name for surface in surfaces] == ["repo-docs"]
        assert len(notes) == 1
        assert "research" in notes[0]

    async def test_a_private_registered_knowledge_base_is_left_out_without_a_word(self, tmp_path: Path):
        active = tmp_path / "repo-docs"
        private = tmp_path / "vault"
        spec = _registered_knowledge_base(active, pages=1)
        _registered_knowledge_base(private, pages=1)
        settings_path = tmp_path / "settings.json"
        settings_path.write_text(
            json.dumps({"knowledge_bases": {"repo-docs": {"path": str(active)}, "vault": {"path": str(private), "private": True}}}),
            encoding="utf-8",
        )
        notes: list[str] = []
        engine = create_async_engine("postgresql+asyncpg://reader:secret@localhost:5432/unused")

        try:
            surfaces = _read_surfaces(
                active,
                spec,
                settings_path,
                ReadOnlySqlGateway(engine),
                _gateway(manifest={}),
                FakeEmbedding(),
                active_hybrid=False,
                notes=notes,
                rerank=None,
            )
        finally:
            await engine.dispose()

        assert [surface.name for surface in surfaces] == ["repo-docs"]
        assert notes == [], "a knowledge base kept private is not a fault to report"

    async def test_a_knowledge_base_whose_own_schema_declares_private_is_left_out(self, tmp_path: Path):
        """A knowledge base's own schema keeps it out of a shared read, whatever this deployment's registry says."""
        active = tmp_path / "repo-docs"
        withheld = tmp_path / "vault"
        spec = _registered_knowledge_base(active, pages=1)
        _private_knowledge_base(withheld, collection="vault")
        settings_path = tmp_path / "settings.json"
        _registry(settings_path, **{"repo-docs": active, "vault": withheld})
        notes: list[str] = []
        engine = create_async_engine("postgresql+asyncpg://reader:secret@localhost:5432/unused")

        try:
            surfaces = _read_surfaces(
                active,
                spec,
                settings_path,
                ReadOnlySqlGateway(engine),
                _gateway(manifest={}),
                FakeEmbedding(),
                active_hybrid=False,
                notes=notes,
                rerank=None,
            )
        finally:
            await engine.dispose()

        assert [surface.name for surface in surfaces] == ["repo-docs"]
        assert notes == [], "a knowledge base that keeps itself out is not a fault to report"

    @pytest.mark.parametrize("private_by", ["registry", "schema"])
    async def test_a_row_claiming_a_withheld_collection_is_refused_rather_than_reading_it(self, tmp_path: Path, private_by: str):
        """A collection kept out of the read is claimed: admitting a row that names it would read it anyway."""
        active = tmp_path / "repo-docs"
        withheld = tmp_path / "vault"
        borrower = tmp_path / "mirror"
        spec = _registered_knowledge_base(active, pages=1)
        if private_by == "schema":
            _private_knowledge_base(withheld, collection="vault")
        else:
            _shared_knowledge_base(withheld, collection="vault")
        _shared_knowledge_base(borrower, collection="vault")
        settings_path = tmp_path / "settings.json"
        rows: dict[str, dict[str, object]] = {
            "repo-docs": {"path": str(active)},
            "vault": {"path": str(withheld), "private": private_by == "registry"},
            "mirror": {"path": str(borrower)},
        }
        settings_path.write_text(json.dumps({"knowledge_bases": rows}), encoding="utf-8")
        notes: list[str] = []
        engine = create_async_engine("postgresql+asyncpg://reader:secret@localhost:5432/unused")

        try:
            surfaces = _read_surfaces(
                active,
                spec,
                settings_path,
                ReadOnlySqlGateway(engine),
                _gateway(manifest={}),
                FakeEmbedding(),
                active_hybrid=False,
                notes=notes,
                rerank=None,
            )
        finally:
            await engine.dispose()

        assert [surface.collection for surface in surfaces] == ["repo-docs"], "the withheld collection is reachable through no route"
        assert len(notes) == 1
        assert "mirror" in notes[0]
        assert "vault" not in notes[0], "the refusal names the row refused, never the knowledge base kept out"

    async def test_no_registry_file_at_all_leaves_the_active_knowledge_base_alone_and_silent(self, tmp_path: Path):
        active = tmp_path / "repo-docs"
        spec = _registered_knowledge_base(active, pages=1)
        notes: list[str] = []
        engine = create_async_engine("postgresql+asyncpg://reader:secret@localhost:5432/unused")

        try:
            surfaces = _read_surfaces(
                active,
                spec,
                tmp_path / "absent.json",
                ReadOnlySqlGateway(engine),
                _gateway(manifest={}),
                FakeEmbedding(),
                active_hybrid=False,
                notes=notes,
                rerank=None,
            )
        finally:
            await engine.dispose()

        assert [surface.name for surface in surfaces] == ["repo-docs"]
        assert notes == []
