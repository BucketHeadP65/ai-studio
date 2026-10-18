r"""Unit tests for the `studio summaries list|approve` CLI commands (`src/studio/knowledge_base/cli.py`).

Driven the same way `test_cli_index.py` drives `index`/`status`: `CliRunner` with the
container *factory* injected through `ctx.obj` (no monkeypatching) — a fake async container whose
`get(AsyncEngine)` yields a fake engine scripting `execute()`'s return, so the resolve →
`SummaryGovernance` statement → print path is exercised with no real Postgres. Pinned:

* `summaries list` reads pending rows and prints one line per row, oldest first;
* an empty pending set prints a clean "no pending summaries" note, not an empty table;
* `summaries list` scopes its SELECT to `--collection` (default the knowledge base spec's own `retrieval.collection`);
* `summaries approve` issues the `status='pending' -> 'active'` UPDATE scoped to `--collection`
  and prints the row count the UPDATE actually touched.
"""

from __future__ import annotations

import json

from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Self

import pytest

from core.cqrs.contracts import JobQueries, MemoryJobs
from engram.indexing.contracts import KnowledgeBaseReader
from engram.retrieval.domain import SqlResult
from engram.testing.fakes.gateway import FakeSqlGateway
from engram.testing.fakes.knowledge_base_reader import FakeKnowledgeBaseReader
from sqlalchemy.ext.asyncio import AsyncEngine
from typer.testing import CliRunner

from studio.knowledge_base.cli import app
from studio.knowledge_base.wiring.contracts import AdminSqlGateway

if TYPE_CHECKING:
    from collections.abc import Mapping
    from pathlib import Path

runner = CliRunner()


@pytest.fixture(autouse=True)
def _ambient_knowledge_base(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Every test in this file runs inside a real knowledge base by cwd, the second of the two ways a command finds one."""
    root = tmp_path / "ambient-knowledge-base"
    root.mkdir()
    (root / "knowledge-base-schema.json").write_text(json.dumps({"slug": "ambient", "page_types": [{"page_type": "note"}]}), encoding="utf-8")
    monkeypatch.chdir(root)


@dataclass(frozen=True, slots=True)
class _SummaryRow:
    """A scripted `kb.unit_summaries` row, attribute access mirroring a SQLAlchemy `Row`."""

    unit_id: str
    unit_type: str
    model: str
    indexed_at: datetime
    collection: str = "workdocs"
    summary: str = "An indexed summary."


@dataclass
class _FakeResult:
    """The `.all()` / `.rowcount` surface `execute()` returns."""

    rows: list[_SummaryRow]
    rowcount: int = 0

    def all(self) -> list[_SummaryRow]:
        return self.rows


@dataclass
class _FakeConnection:
    """Scripts `execute()`: returns the scripted rows/rowcount, records every call for assertions."""

    rows: list[_SummaryRow] = field(default_factory=list)
    rowcount: int = 0
    executed: list[tuple[str, Mapping[str, object]]] = field(default_factory=list)

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(self, *_exc: object) -> None:
        return None

    async def execute(self, statement: object, parameters: Mapping[str, object] | None = None) -> _FakeResult:
        params: dict[str, object] = dict(parameters or {})
        compile_fn = getattr(statement, "compile", None)
        if not params and compile_fn is not None:  # Core statements carry their binds inside the statement
            params = dict(compile_fn().params)
        self.executed.append((str(statement), params))
        return _FakeResult(rows=self.rows, rowcount=self.rowcount)


@dataclass
class _FakeEngine:
    """A fake `AsyncEngine`: `connect()` (reads) and `begin()` (writes) share one scripted connection."""

    connection: _FakeConnection

    def connect(self) -> _FakeConnection:
        return self.connection

    def begin(self) -> _FakeConnection:
        return self.connection


def _identity_gateway() -> FakeSqlGateway:
    """A gateway answering the identity read with no revision and no index history."""
    return FakeSqlGateway(
        routed={
            "to_regclass": SqlResult(rows=({"present": False},), columns=("present",)),
            "index_fingerprint": SqlResult(rows=(), columns=("indexed_at",)),
        }
    )


@dataclass
class _SummariesContainer:
    """Fake async DI container: `get(AsyncEngine)` returns the fake engine it was built with.

    Also serves the identity announce's own `AdminSqlGateway` and `JobQueries` reads.
    """

    connection: _FakeConnection

    async def get(self, key: type) -> object:
        if key is AsyncEngine:
            return _FakeEngine(self.connection)
        if key is AdminSqlGateway:
            return _identity_gateway()
        if key is KnowledgeBaseReader:
            return FakeKnowledgeBaseReader()
        if key is JobQueries:
            return MemoryJobs()
        msg = f"_SummariesContainer has no registration for {key!r}"
        raise KeyError(msg)


@dataclass
class _SummariesContainerFactory:
    """Zero-arg `ctx.obj` factory returning a container around a scripted connection."""

    connection: _FakeConnection

    def __call__(self) -> _SummariesContainer:
        return _SummariesContainer(self.connection)


def _pending_row(unit_id: str = "paper.x", *, unit_type: str = "page", model: str = "gpt-5-mini") -> _SummaryRow:
    return _SummaryRow(unit_id=unit_id, unit_type=unit_type, model=model, indexed_at=datetime(2026, 1, 1, tzinfo=UTC))


class TestSummariesList:
    def test_prints_a_line_per_pending_row(self):
        connection = _FakeConnection(rows=[_pending_row()])
        factory = _SummariesContainerFactory(connection)

        result = runner.invoke(app, ["summaries", "list", "--collection", "workdocs"], obj=factory)

        assert result.exit_code == 0
        assert "paper.x" in result.stdout
        assert "gpt-5-mini" in result.stdout

    def test_empty_pending_set_prints_a_clean_note(self):
        connection = _FakeConnection(rows=[])
        factory = _SummariesContainerFactory(connection)

        result = runner.invoke(app, ["summaries", "list", "--collection", "workdocs"], obj=factory)

        assert result.exit_code == 0
        assert "No pending summaries" in result.stdout

    def test_scopes_the_select_to_the_collection(self):
        connection = _FakeConnection(rows=[])
        factory = _SummariesContainerFactory(connection)

        runner.invoke(app, ["summaries", "list", "--collection", "workdocs"], obj=factory)

        sql, params = connection.executed[0]
        assert "kb.unit_summaries" in sql
        assert "pending" in set(params.values())
        assert "workdocs" in set(params.values())

    def test_default_collection_comes_from_the_knowledge_base_spec(self):
        """No `--collection` falls back to the ambient knowledge base's own `retrieval.collection` (its slug, unset)."""
        connection = _FakeConnection(rows=[])
        factory = _SummariesContainerFactory(connection)

        runner.invoke(app, ["summaries", "list"], obj=factory)

        _sql, params = connection.executed[0]
        assert "ambient" in set(params.values())


class TestSummariesApprove:
    def test_prints_the_approved_count(self):
        connection = _FakeConnection(rows=[], rowcount=3)
        factory = _SummariesContainerFactory(connection)

        result = runner.invoke(app, ["summaries", "approve", "--collection", "workdocs"], obj=factory)

        assert result.exit_code == 0
        assert "3" in result.stdout
        assert "workdocs" in result.stdout

    def test_zero_pending_rows_still_exits_zero(self):
        connection = _FakeConnection(rows=[], rowcount=0)
        factory = _SummariesContainerFactory(connection)

        result = runner.invoke(app, ["summaries", "approve", "--collection", "workdocs"], obj=factory)

        assert result.exit_code == 0
        assert "0" in result.stdout

    def test_issues_the_update_scoped_to_the_collection(self):
        connection = _FakeConnection(rows=[], rowcount=0)
        factory = _SummariesContainerFactory(connection)

        runner.invoke(app, ["summaries", "approve", "--collection", "workdocs"], obj=factory)

        sql, params = connection.executed[0]
        assert "UPDATE kb.unit_summaries" in sql
        assert "active" in set(params.values())
        assert "pending" in set(params.values())
        assert "workdocs" in set(params.values())


class TestSummariesHelp:
    def test_summaries_help_exits_zero(self):
        result = runner.invoke(app, ["summaries", "--help"])

        assert result.exit_code == 0
        assert "list" in result.stdout
        assert "approve" in result.stdout
