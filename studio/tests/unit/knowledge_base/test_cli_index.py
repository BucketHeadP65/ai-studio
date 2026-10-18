r"""Unit tests for the `memory index` + `memory status` CLI commands (`src/studio/knowledge_base/cli.py`).

Driven by :class:`typer.testing.CliRunner` with the container *factory* injected
through `ctx.obj` (no monkeypatching): the fake factory returns
a fake async container whose `get(MessageBus)` yields a real
:class:`~core.cqrs.bus.MessageBus` wired to a recording handler that returns a scripted
:class:`~engram.indexing.domain.models.IndexReport`. So the command's resolve → dispatch →
print → exit-code path is exercised with no database, knowledge base, or network.

Pinned behaviour:

* a clean index (no validation errors) prints the report and exits 0;
* an index whose report carries validation errors exits 1 (flagged-but-loaded: rows still
  loaded, but a non-clean knowledge base fails CI);
* a bare `index` is incremental by default — it reaches the handler as
  `IndexKnowledgeBase(full=False)`;
* `--full` forces a whole knowledge base rebuild — `IndexKnowledgeBase(full=True)` — and composes with
  `--no-embed`;
* `--no-embed` reaches the handler as `IndexKnowledgeBase(embed=False)`.
"""

from __future__ import annotations

import json

from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Self

import pytest
import typer

from core.cqrs.contracts import JobQueries, MemoryJobs, MessageBus
from engram.indexing.contracts import (
    DEFAULT_CHUNK_SIZES,
    CollectionIsForeignError,
    IndexKnowledgeBase,
    IndexReport,
    KnowledgeBaseReader,
    ValidationIssue,
    ValidationReport,
    indexer_version,
)
from engram.init.contracts import SchemaBehindHeadError
from engram.retrieval.domain import SchemaDescription, SqlGateway, SqlResult
from engram.testing.fakes.bus_spy import RecordingCommandHandler
from engram.testing.fakes.gateway import FakeSqlGateway
from engram.testing.fakes.knowledge_base_reader import FakeKnowledgeBaseReader
from engram.testing.specs import research_spec
from sqlalchemy.ext.asyncio import AsyncEngine
from typer.testing import CliRunner

from studio.knowledge_base import cli as cli_module
from studio.knowledge_base.cli import app
from studio.knowledge_base.cli_support import resolve_knowledge_base_alias
from studio.knowledge_base.settings import AzureOpenAiSettings, IngestionSettings
from studio.knowledge_base.wiring.contracts import AdminSqlGateway

if TYPE_CHECKING:
    from collections.abc import Mapping

    from core.cqrs.contracts import Command, Event
    from engram.kb.contracts import KnowledgeBaseSchema

runner = CliRunner()


@pytest.fixture(autouse=True)
def _ambient_knowledge_base(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Every test in this file runs inside a real knowledge base by cwd, the second of the two ways a command finds one.

    A test that names its own `--knowledge-base` (or replaces `_resolve_settings` outright, as `TestKnowledgeBaseFlag`
    and `TestCollectionDefault` do) is unaffected: this only supplies a fallback for a bare `index`/`status`.
    """
    monkeypatch.chdir(_knowledge_base(tmp_path / "ambient-knowledge-base", "ambient"))


@pytest.fixture(autouse=True)
def _schema_at_head(monkeypatch: pytest.MonkeyPatch) -> None:
    """Default every test's `index` preflight to an at-head DB — a real check needs a live DB.

    `TestSchemaPreflight` overrides this per-case to drive the behind-head failure path.
    """
    from studio.knowledge_base import cli as cli_module

    monkeypatch.setattr(cli_module, "assert_schema_at_head", lambda _db_url: None)


def _identity_gateway() -> FakeSqlGateway:
    """A gateway answering the identity read with no revision and no index history."""
    return FakeSqlGateway(
        routed={
            "to_regclass": SqlResult(rows=({"present": False},), columns=("present",)),
            "index_fingerprint": SqlResult(rows=(), columns=("indexed_at",)),
        }
    )


def _identity_gateway_at(indexed_at: datetime) -> FakeSqlGateway:
    """A gateway answering the identity read with no revision and an index fingerprint timestamped `indexed_at`."""
    return FakeSqlGateway(
        routed={
            "to_regclass": SqlResult(rows=({"present": False},), columns=("present",)),
            "index_fingerprint": SqlResult(rows=({"indexed_at": indexed_at},), columns=("indexed_at",)),
        }
    )


class _RaisingGateway:
    """An `AdminSqlGateway` whose every read raises, standing in for a database with no `memory` schema at all."""

    async def rows(self, sql: str, params: Mapping[str, object] | None = None, *, cap: int = 50) -> SqlResult:
        del sql, params, cap
        msg = "relation kb.index_fingerprint does not exist"
        raise RuntimeError(msg)

    async def api_objects(self) -> SchemaDescription:  # pragma: no cover (the preflight test never calls this)
        raise NotImplementedError


def _knowledge_base(root: Path, slug: str) -> Path:
    root.mkdir(parents=True, exist_ok=True)
    (root / "knowledge-base-schema.json").write_text(json.dumps({"slug": slug, "page_types": [{"page_type": "note"}]}), encoding="utf-8")
    return root


def _write_feedback(root: Path, *created_ats: str) -> None:
    """Write one feedback entry per `created_at` to `root`'s feedback file, the input the unreviewed count reads."""
    (root / "_meta").mkdir(parents=True, exist_ok=True)
    lines = (json.dumps({"created_at": created_at, "category": "bug", "message": "note", "tool": "run_sql"}) for created_at in created_ats)
    (root / "_meta" / "mcp-feedback.jsonl").write_text("\n".join(lines) + "\n", encoding="utf-8")


class FakeContainer:
    """Fake async DI container: `get(MessageBus)` returns the bus it was built with."""

    def __init__(self, bus: MessageBus, gateway: SqlGateway | None = None) -> None:
        self._bus = bus
        self._gateway = gateway if gateway is not None else _identity_gateway()
        self._reader: KnowledgeBaseReader = FakeKnowledgeBaseReader()
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
    """Zero-arg `ctx.obj` factory returning a container around a recording handler.

    The recording handler returns `report` from every `IndexKnowledgeBase` dispatch and
    keeps the commands it handled (so a test can assert the `--no-embed` flag was
    plumbed through to `IndexKnowledgeBase.embed`). `gateway` overrides the default
    no-revision, no-history identity gateway, for a test pinning the last index time.
    """

    def __init__(self, report: IndexReport, gateway: SqlGateway | None = None) -> None:
        self.handler = RecordingCommandHandler(result=report)
        self._bus = MessageBus({IndexKnowledgeBase: self.handler}, {})
        self._gateway = gateway

    def __call__(self) -> FakeContainer:
        return FakeContainer(self._bus, self._gateway)


def _clean_report() -> IndexReport:
    """A successful, validation-clean index report."""
    return IndexReport(pages=3, sections=7, claims=5, edges=4, pruned=1, embedded=12, reused=0)


def _flagged_report() -> IndexReport:
    """An index report carrying one validation error (rows still loaded, but CI must fail)."""
    issue = ValidationIssue(
        severity="error", rule="missing-quote", page_id="paper.x", message="claim is missing the required quote", anchor="^c1"
    )
    return IndexReport(pages=1, sections=1, claims=1, validation=ValidationReport(errors=(issue,)))


class TestIndexExitCode:
    def test_clean_index_exits_zero(self):
        factory = FakeContainerFactory(_clean_report())

        result = runner.invoke(app, ["index"], obj=factory)

        assert result.exit_code == 0
        assert "3 pages" in result.stdout
        assert "0 error(s)" in result.stdout

    def test_index_with_validation_errors_exits_one(self):
        factory = FakeContainerFactory(_flagged_report())

        result = runner.invoke(app, ["index"], obj=factory)

        assert result.exit_code == 1
        assert "1 error(s)" in result.stdout
        assert "missing-quote" in result.stdout

    def test_an_error_standing_on_an_unchanged_page_exits_one(self):
        issue = ValidationIssue(severity="error", rule="dangling-link", page_id="ent.x", message="links to a page that does not exist")
        report = IndexReport(pages=1, mode="incremental", standing_validation=ValidationReport(errors=(issue,)))
        factory = FakeContainerFactory(report)

        result = runner.invoke(app, ["index"], obj=factory)

        assert result.exit_code == 1
        assert "Standing in the knowledge base: 1 error(s), 0 warning(s)" in result.stdout
        assert "ent.x" in result.stdout
        assert "dangling-link" in result.stdout

    def test_an_incremental_run_with_nothing_standing_exits_zero(self):
        report = IndexReport(pages=1, mode="incremental", standing_validation=ValidationReport())
        factory = FakeContainerFactory(report)

        result = runner.invoke(app, ["index"], obj=factory)

        assert result.exit_code == 0

    def test_index_dispatches_a_index_knowledge_base_command(self):
        factory = FakeContainerFactory(_clean_report())

        runner.invoke(app, ["index"], obj=factory)

        assert len(factory.handler.handled) == 1


class _ForeignRefusingHandler:
    """Command handler that refuses every index as aimed at another knowledge base's collection."""

    def __init__(self) -> None:
        self.events: list[Event] = []

    async def __call__(self, command: Command) -> object:
        del command
        raise CollectionIsForeignError(collection="research", stored_root="/their/knowledge_base", knowledge_base_root="/this/knowledge_base")


class _ForeignRefusingFactory:
    """Zero-arg `ctx.obj` factory whose index handler always refuses as foreign."""

    def __call__(self) -> FakeContainer:
        return FakeContainer(MessageBus({IndexKnowledgeBase: _ForeignRefusingHandler()}, {}))


class TestForeignCollectionRefusal:
    def test_a_collection_claimed_by_another_root_refuses_with_exit_2(self):
        result = runner.invoke(app, ["index"], obj=_ForeignRefusingFactory())

        assert result.exit_code == 2
        assert "collection research was indexed from /their/knowledge_base, this knowledge base is at /this/knowledge_base." in result.stdout
        assert "Pass --collection research to take the collection over, or index the knowledge base that owns it." in result.stdout

    def test_the_collection_flag_marks_the_takeover_on_the_command(self):
        factory = FakeContainerFactory(_clean_report())

        runner.invoke(app, ["index", "--collection", "research"], obj=factory)

        [command] = factory.handler.handled
        assert isinstance(command, IndexKnowledgeBase)
        assert command.collection_named is True

    def test_an_unnamed_collection_is_not_a_takeover(self):
        factory = FakeContainerFactory(_clean_report())

        runner.invoke(app, ["index"], obj=factory)

        [command] = factory.handler.handled
        assert isinstance(command, IndexKnowledgeBase)
        assert command.collection_named is False
        assert isinstance(factory.handler.handled[0], IndexKnowledgeBase)

    def test_a_feedback_entry_filed_after_the_last_index_is_counted_on_the_last_line(self, tmp_path: Path):
        root = _knowledge_base(tmp_path / "alpha", "alpha")
        _write_feedback(root, "2026-08-27T00:00:00+00:00")
        factory = FakeContainerFactory(_clean_report(), gateway=_identity_gateway_at(datetime(2026, 8, 26, 0, 0, tzinfo=UTC)))

        result = runner.invoke(app, ["index", "--knowledge-base", str(root)], obj=factory)

        assert result.exit_code == 0
        assert result.stdout.splitlines()[-1] == "1 feedback entry filed since the last index, read it with studio feedback"

    def test_two_feedback_entries_filed_after_the_last_index_are_counted_with_a_plural_pronoun(self, tmp_path: Path):
        root = _knowledge_base(tmp_path / "alpha", "alpha")
        _write_feedback(root, "2026-08-27T00:00:00+00:00", "2026-08-27T01:00:00+00:00")
        factory = FakeContainerFactory(_clean_report(), gateway=_identity_gateway_at(datetime(2026, 8, 26, 0, 0, tzinfo=UTC)))

        result = runner.invoke(app, ["index", "--knowledge-base", str(root)], obj=factory)

        assert result.exit_code == 0
        assert result.stdout.splitlines()[-1] == "2 feedback entries filed since the last index, read them with studio feedback"

    def test_a_feedback_entry_filed_before_the_last_index_is_not_counted(self, tmp_path: Path):
        root = _knowledge_base(tmp_path / "alpha", "alpha")
        _write_feedback(root, "2026-08-01T00:00:00+00:00")
        factory = FakeContainerFactory(_clean_report(), gateway=_identity_gateway_at(datetime(2026, 8, 26, 0, 0, tzinfo=UTC)))

        result = runner.invoke(app, ["index", "--knowledge-base", str(root)], obj=factory)

        assert result.exit_code == 0
        assert "since the last index" not in result.stdout


class TestNoEmbedFlag:
    def test_no_embed_is_plumbed_to_the_command(self):
        factory = FakeContainerFactory(_clean_report())

        result = runner.invoke(app, ["index", "--no-embed"], obj=factory)

        assert result.exit_code == 0
        command = factory.handler.handled[0]
        assert isinstance(command, IndexKnowledgeBase)
        assert command.embed is False

    def test_default_index_embeds(self):
        factory = FakeContainerFactory(_clean_report())

        runner.invoke(app, ["index"], obj=factory)

        command = factory.handler.handled[0]
        assert isinstance(command, IndexKnowledgeBase)
        assert command.embed is True


class TestFullFlag:
    """`--full` forces a whole knowledge base rebuild; a bare `index` is incremental."""

    def test_default_index_is_incremental(self):
        factory = FakeContainerFactory(_clean_report())

        runner.invoke(app, ["index"], obj=factory)

        command = factory.handler.handled[0]
        assert isinstance(command, IndexKnowledgeBase)
        assert command.full is False

    def test_full_forces_a_full_rebuild(self):
        factory = FakeContainerFactory(_clean_report())

        result = runner.invoke(app, ["index", "--full"], obj=factory)

        assert result.exit_code == 0
        command = factory.handler.handled[0]
        assert isinstance(command, IndexKnowledgeBase)
        assert command.full is True

    def test_full_composes_with_no_embed(self):
        factory = FakeContainerFactory(_clean_report())

        result = runner.invoke(app, ["index", "--no-embed", "--full"], obj=factory)

        assert result.exit_code == 0
        command = factory.handler.handled[0]
        assert isinstance(command, IndexKnowledgeBase)
        assert command.full is True
        assert command.embed is False


class TestSummariesFlag:
    def test_summaries_flag_is_plumbed_to_the_command(self):
        factory = FakeContainerFactory(_clean_report())

        result = runner.invoke(app, ["index", "--summaries"], obj=factory)

        assert result.exit_code == 0
        command = factory.handler.handled[0]
        assert isinstance(command, IndexKnowledgeBase)
        assert command.summaries is True

    def test_default_index_does_not_run_summaries(self):
        factory = FakeContainerFactory(_clean_report())

        runner.invoke(app, ["index"], obj=factory)

        command = factory.handler.handled[0]
        assert isinstance(command, IndexKnowledgeBase)
        assert command.summaries is False


class TestCollectionFlag:
    def test_collection_is_plumbed_to_the_command(self):
        factory = FakeContainerFactory(_clean_report())

        runner.invoke(app, ["index", "--collection", "research"], obj=factory)

        command = factory.handler.handled[0]
        assert isinstance(command, IndexKnowledgeBase)
        assert command.collection == "research"


class TestKnowledgeBaseFlag:
    """`--knowledge-base PATH` names the knowledge base, and without it the working directory does."""

    def test_the_flag_names_the_knowledge_base(self, tmp_path: Path):
        (tmp_path / "knowledge-base-schema.json").write_text(research_spec().model_dump_json(), encoding="utf-8")

        active = cli_module._resolve_settings(str(tmp_path), cwd=Path("/"))

        assert active.knowledge_base.path == tmp_path

    def test_the_deprecated_alias_names_the_same_knowledge_base(self, tmp_path: Path):
        (tmp_path / "knowledge-base-schema.json").write_text(research_spec().model_dump_json(), encoding="utf-8")

        assert resolve_knowledge_base_alias(None, str(tmp_path)) == str(tmp_path)
        assert resolve_knowledge_base_alias(str(tmp_path), "/other") == str(tmp_path)

    def test_without_the_flag_the_working_directory_decides(self, tmp_path: Path):
        (tmp_path / "knowledge-base-schema.json").write_text(research_spec().model_dump_json(), encoding="utf-8")
        inside = tmp_path / "Units"
        inside.mkdir()

        active = cli_module._resolve_settings(None, cwd=inside)

        assert active.knowledge_base.path == tmp_path

    def test_a_command_run_where_nothing_names_a_knowledge_base_refuses(self, tmp_path: Path):
        with pytest.raises(typer.Exit) as refusal:
            cli_module._resolve_settings(None, cwd=tmp_path)

        assert refusal.value.exit_code == 1

    def test_the_index_command_runs_with_the_flag(self, tmp_path: Path):
        factory = FakeContainerFactory(_clean_report())
        (tmp_path / "knowledge-base-schema.json").write_text(research_spec().model_dump_json(), encoding="utf-8")

        result = runner.invoke(app, ["index", "--knowledge-base", str(tmp_path)], obj=factory)

        assert result.exit_code == 0


def _kitchen_spec() -> KnowledgeBaseSchema:
    """The research template's schema re-declared into a `kitchen` partition."""
    spec = research_spec()
    return spec.model_copy(update={"retrieval": spec.retrieval.model_copy(update={"collection": "kitchen"})})


class TestCollectionDefault:
    """The partition defaults to the knowledge base spec's own declaration — a `--knowledge-base` never lands in another knowledge base's rows."""

    def test_the_spec_declares_the_partition(self, tmp_path: Path):
        (tmp_path / "knowledge-base-schema.json").write_text(_kitchen_spec().model_dump_json(), encoding="utf-8")
        factory = FakeContainerFactory(_clean_report())

        result = runner.invoke(app, ["index", "--knowledge-base", str(tmp_path)], obj=factory)

        assert result.exit_code == 0
        dispatched = factory.handler.handled[-1]
        assert isinstance(dispatched, IndexKnowledgeBase)
        assert dispatched.collection == "kitchen"

    def test_the_flag_stays_the_deliberate_override(self, tmp_path: Path):
        (tmp_path / "knowledge-base-schema.json").write_text(_kitchen_spec().model_dump_json(), encoding="utf-8")
        factory = FakeContainerFactory(_clean_report())

        result = runner.invoke(app, ["index", "--knowledge-base", str(tmp_path), "--collection", "workdocs"], obj=factory)

        assert result.exit_code == 0
        dispatched = factory.handler.handled[-1]
        assert isinstance(dispatched, IndexKnowledgeBase)
        assert dispatched.collection == "workdocs"


class TestSchemaPreflight:
    """A DB behind the code's alembic head fails `index` cleanly, before any dispatch."""

    def test_schema_behind_head_fails_cleanly_before_dispatch(self, monkeypatch: pytest.MonkeyPatch):
        from studio.knowledge_base import cli as cli_module

        def _raise_behind_head(_db_url: str) -> None:
            msg = "knowledge base DB is at revision 0024, but the code expects head 0025. Run `uv run alembic upgrade head` (or `studio init db`) before indexing."
            raise SchemaBehindHeadError(msg)

        monkeypatch.setattr(cli_module, "assert_schema_at_head", _raise_behind_head)
        factory = FakeContainerFactory(_clean_report())

        result = runner.invoke(app, ["index"], obj=factory)

        assert result.exit_code == 1
        assert "0024" in result.stdout
        assert "0025" in result.stdout
        assert "alembic upgrade head" in result.stdout
        assert factory.handler.handled == []  # the preflight failed before any dispatch ran

    def test_schema_behind_head_fails_cleanly_even_when_the_database_has_no_memory_schema(
        self, monkeypatch: pytest.MonkeyPatch
    ):
        """A database with no `memory` schema at all still gets the clean schema message.

        The preflight runs before the identity announce reads anything, never a raw read error.
        """
        from studio.knowledge_base import cli as cli_module

        def _raise_behind_head(_db_url: str) -> None:
            msg = "knowledge base DB is at revision none, but the code expects head 0025. Run `uv run alembic upgrade head` (or `studio init db`) before indexing."
            raise SchemaBehindHeadError(msg)

        monkeypatch.setattr(cli_module, "assert_schema_at_head", _raise_behind_head)
        factory = FakeContainerFactory(_clean_report(), gateway=_RaisingGateway())

        result = runner.invoke(app, ["index"], obj=factory)

        assert result.exit_code == 1
        assert "revision none" in result.stdout
        assert "0025" in result.stdout
        assert "alembic upgrade head" in result.stdout
        assert factory.handler.handled == []  # the preflight failed before any dispatch ran


class TestSchemaPreflightUnreachableDatabase:
    """A database the preflight cannot even reach is two clean lines and exit 1, never a traceback."""

    def test_connection_refused_gives_two_lines_and_exits_one_with_no_dispatch(self, monkeypatch: pytest.MonkeyPatch):
        from studio.knowledge_base import cli as cli_module

        def _raise_unreachable(_db_url: str) -> None:
            msg = "[Errno 61] Connect call failed ('127.0.0.1', 5434)"
            raise ConnectionRefusedError(msg)

        monkeypatch.setattr(cli_module, "assert_schema_at_head", _raise_unreachable)
        factory = FakeContainerFactory(_clean_report())

        result = runner.invoke(app, ["index"], obj=factory)

        lines = result.stdout.splitlines()
        assert result.exit_code == 1
        assert lines[0].startswith("knowledge base ")
        assert lines[1].startswith("db ")
        assert lines[1].endswith("unreachable, connection refused.")
        assert len(lines) == 2
        assert "Traceback" not in result.stdout
        assert factory.handler.handled == []  # the preflight failed before any dispatch ran


@dataclass(frozen=True, slots=True)
class _Row:
    """A scripted result row, attribute access mirroring a SQLAlchemy `Row` (`.model`, `.spec_hash`, …)."""

    values: Mapping[str, object]

    def __getattr__(self, name: str) -> object:
        try:
            return self.values[name]
        except KeyError as exc:  # pragma: no cover (a test scripts every column it reads)
            raise AttributeError(name) from exc


@dataclass
class _FakeResult:
    """The `.first()` surface of an `execute` result — the single row, or `None`."""

    row: _Row | None

    def first(self) -> _Row | None:
        return self.row


@dataclass
class _FakeConnection:
    """Scripts the two read primitives `_print_status` uses, keyed by an SQL substring.

    `scalar` answers the per-table `count(*)` loop and the manifest count; `execute` answers the
    `embed_meta` and `index_fingerprint` single-row reads. Matching on a substring keeps the fake
    decoupled from the exact SQL text while still proving the status path reads the right tables.
    """

    scalars: Mapping[str, object]
    rows: Mapping[str, _Row | None]

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(self, *_exc: object) -> None:
        return None

    async def scalar(self, statement: object, parameters: object = None) -> object:
        del parameters
        return self._match(self.scalars, str(statement))

    async def execute(self, statement: object, parameters: object = None) -> _FakeResult:
        del parameters
        return _FakeResult(self._match(self.rows, str(statement)))

    @staticmethod
    def _match[T](table: Mapping[str, T], sql: str) -> T:
        for needle, value in table.items():
            if needle in sql:
                return value
        msg = f"_FakeConnection has no scripted answer for {sql!r}"
        raise KeyError(msg)


@dataclass
class _FakeStatusEngine:
    """A fake admin `AsyncEngine` whose `connect()` yields a scripted `_FakeConnection`."""

    connection: _FakeConnection

    def connect(self) -> _FakeConnection:
        return self.connection


@dataclass
class _StatusContainer:
    """Fake async DI container resolving the admin `AsyncEngine` `_print_status` reads through.

    Also serves the identity announce's own `AdminSqlGateway`, `KnowledgeBaseReader`, and `JobQueries` reads.
    """

    engine: _FakeStatusEngine
    gateway: FakeSqlGateway
    jobs: JobQueries
    reader: KnowledgeBaseReader = field(default_factory=FakeKnowledgeBaseReader)
    ingestion: IngestionSettings = field(default_factory=IngestionSettings)
    azure_openai: AzureOpenAiSettings = field(default_factory=AzureOpenAiSettings)

    async def get(self, key: type) -> object:
        if key is AsyncEngine:
            return self.engine
        if key is IngestionSettings:
            return self.ingestion
        if key is AzureOpenAiSettings:
            return self.azure_openai
        if key is AdminSqlGateway:
            return self.gateway
        if key is KnowledgeBaseReader:
            return self.reader
        if key is JobQueries:
            return self.jobs
        msg = f"_StatusContainer has no registration for {key!r}"
        raise KeyError(msg)


@dataclass
class _StatusContainerFactory:
    """Zero-arg `ctx.obj` factory returning a container around a scripted status engine."""

    fingerprint: _Row | None
    files_tracked: int
    embed_meta: _Row | None = None
    ingestion: IngestionSettings = field(default_factory=IngestionSettings)
    azure_openai: AzureOpenAiSettings = field(default_factory=AzureOpenAiSettings)

    def __call__(self) -> _StatusContainer:
        scalars: dict[str, object] = {"kb.index_manifest": self.files_tracked}
        for table in ("pages", "sections", "claims", "edges", "sources_raw", "raw_chunks", "vocab", "query_log"):
            scalars[f"kb.{table}"] = 0
        scalars["max(asked_at)"] = None
        rows: dict[str, _Row | None] = {"kb.embed_meta": self.embed_meta, "kb.index_fingerprint": self.fingerprint}
        return _StatusContainer(
            _FakeStatusEngine(_FakeConnection(scalars=scalars, rows=rows)),
            gateway=_identity_gateway(),
            jobs=MemoryJobs(),
            ingestion=self.ingestion,
            azure_openai=self.azure_openai,
        )


def _fingerprint_row(*, indexer_version: str = indexer_version(DEFAULT_CHUNK_SIZES)) -> _Row:
    """A stored `index_fingerprint` row (current indexer version unless overridden)."""
    return _Row(
        {
            "indexer_version": indexer_version,
            "spec_hash": "spec0123456789abcdef",
            "vocabulary_hash": "reg0123456789abcdef",
            "embed_model": "text-embedding-3-large",
            "indexed_at": datetime(2026, 6, 27, 12, 0, tzinfo=UTC),
        }
    )


class TestStatusIndexDrift:
    """`studio status` reports index mode / files-tracked / fingerprint + the stale hint."""

    def test_reports_mode_files_tracked_and_fingerprint_fields(self):
        factory = _StatusContainerFactory(fingerprint=_fingerprint_row(), files_tracked=42)

        result = runner.invoke(app, ["status"], obj=factory)

        assert result.exit_code == 0
        assert "incremental" in result.stdout
        assert "42" in result.stdout
        assert "text-embedding-3-large" in result.stdout
        assert indexer_version(DEFAULT_CHUNK_SIZES) in result.stdout

    def test_matching_fingerprint_and_nonempty_manifest_shows_no_stale_hint(self):
        factory = _StatusContainerFactory(fingerprint=_fingerprint_row(), files_tracked=42)

        result = runner.invoke(app, ["status"], obj=factory)

        assert result.exit_code == 0
        assert "next run is --full" not in result.stdout

    def test_indexer_version_mismatch_shows_stale_hint(self):
        factory = _StatusContainerFactory(fingerprint=_fingerprint_row(indexer_version="0"), files_tracked=42)

        result = runner.invoke(app, ["status"], obj=factory)

        assert result.exit_code == 0
        assert "next run is --full" in result.stdout

    def test_a_chunk_size_change_shows_stale_hint(self):
        factory = _StatusContainerFactory(
            fingerprint=_fingerprint_row(), files_tracked=42, ingestion=IngestionSettings(chunk_target_tokens=300, chunk_max_tokens=600)
        )

        result = runner.invoke(app, ["status"], obj=factory)

        assert result.exit_code == 0
        assert "next run is --full" in result.stdout

    def test_a_chunk_planner_change_shows_stale_hint(self):
        factory = _StatusContainerFactory(
            fingerprint=_fingerprint_row(),
            files_tracked=42,
            ingestion=IngestionSettings(chunk_planner_model="example-deployment"),
            azure_openai=AzureOpenAiSettings(endpoint="https://example.openai.azure.com"),
        )

        result = runner.invoke(app, ["status"], obj=factory)

        assert result.exit_code == 0
        assert "next run is --full" in result.stdout
        assert "chunk-planner change" in result.stdout

    def test_a_fingerprint_written_with_the_settings_chunk_planner_shows_no_stale_hint(self):
        azure_openai = AzureOpenAiSettings(endpoint="https://example.openai.azure.com")
        ingestion = IngestionSettings(chunk_planner_model="example-deployment")
        stored = indexer_version(DEFAULT_CHUNK_SIZES, planner="example-deployment/prompt-1", planner_ceiling_tokens=ingestion.chunk_planner_ceiling_tokens)
        factory = _StatusContainerFactory(fingerprint=_fingerprint_row(indexer_version=stored), files_tracked=42, ingestion=ingestion, azure_openai=azure_openai)

        result = runner.invoke(app, ["status"], obj=factory)

        assert result.exit_code == 0
        assert "next run is --full" not in result.stdout

    def test_an_azure_endpoint_alone_turns_no_chunk_planner_on(self):
        factory = _StatusContainerFactory(
            fingerprint=_fingerprint_row(), files_tracked=42, azure_openai=AzureOpenAiSettings(endpoint="https://example.openai.azure.com")
        )

        result = runner.invoke(app, ["status"], obj=factory)

        assert result.exit_code == 0
        assert "next run is --full" not in result.stdout

    def test_empty_manifest_shows_stale_hint_and_full_mode(self):
        factory = _StatusContainerFactory(fingerprint=None, files_tracked=0)

        result = runner.invoke(app, ["status"], obj=factory)

        assert result.exit_code == 0
        assert "next run is --full" in result.stdout
        assert "full" in result.stdout


class TestHelp:
    def test_index_help_exits_zero(self):
        result = runner.invoke(app, ["index", "--help"])

        assert result.exit_code == 0

    def test_index_help_lists_knowledge_base_flag(self):
        result = runner.invoke(app, ["index", "--help"])

        assert "--knowledge-base" in result.stdout
        assert "no-embed" in result.stdout

    def test_index_help_hides_vault_flag(self):
        # --vault is a deprecated alias, kept out of --help.
        result = runner.invoke(app, ["index", "--help"])

        assert "--vault" not in result.stdout

    def test_index_help_lists_full_flag(self):
        result = runner.invoke(app, ["index", "--help"])

        assert "--full" in result.stdout

    def test_index_help_lists_summaries_flag(self):
        result = runner.invoke(app, ["index", "--help"])

        assert "--summaries" in result.stdout
