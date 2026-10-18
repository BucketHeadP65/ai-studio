r"""Unit tests for the `studio export okf` CLI command (`src/studio/knowledge_base/cli.py`).

Driven by :class:`typer.testing.CliRunner` with the container *factory* injected
through `ctx.obj` (no monkeypatching): the fake factory returns a
fake async container whose `get(MessageBus)` backs the direct bus resolution
:func:`~studio.knowledge_base.cli._dispatch_export` performs, with a real :class:`~core.cqrs.bus.MessageBus`
wired to a recording handler that returns a scripted
:class:`~engram.export.contracts.OkfExportReport`. So the command's resolve → dispatch →
print path is exercised with no knowledge base, database, or filesystem.

Pinned behaviour:

* a successful export prints the report and exits 0;
* `--out` / `--collection` are plumbed onto the dispatched `ExportOkf` command;
* a dispatch failure surfaces as a clean exit-1 message, not a traceback.
"""

from __future__ import annotations

import json

from typing import TYPE_CHECKING

import pytest

from core.cqrs.contracts import JobQueries, MemoryJobs, MessageBus
from engram.export.contracts import ExportOkf, OkfExportReport
from engram.indexing.contracts import KnowledgeBaseReader
from engram.retrieval.domain import SqlResult
from engram.testing.fakes.bus_spy import RecordingCommandHandler
from engram.testing.fakes.gateway import FakeSqlGateway
from engram.testing.fakes.knowledge_base_reader import FakeKnowledgeBaseReader
from typer.testing import CliRunner

from studio.knowledge_base.cli import app
from studio.knowledge_base.wiring.contracts import AdminSqlGateway

if TYPE_CHECKING:
    from pathlib import Path

runner = CliRunner()


@pytest.fixture(autouse=True)
def _ambient_knowledge_base(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Every test in this file runs inside a real knowledge base by cwd, the second of the two ways a command finds one.

    A test that names its own `--knowledge-base` and chdirs elsewhere (`TestExportOkf`'s "inside another
    knowledge base" case) is unaffected — its own `monkeypatch.chdir` runs after this one.
    """
    root = tmp_path / "ambient-knowledge-base"
    root.mkdir()
    (root / "knowledge-base-schema.json").write_text(json.dumps({"slug": "ambient", "page_types": [{"page_type": "note"}]}), encoding="utf-8")
    monkeypatch.chdir(root)


def _identity_gateway() -> FakeSqlGateway:
    """A gateway answering the identity read with no revision and no index history."""
    return FakeSqlGateway(
        routed={
            "to_regclass": SqlResult(rows=({"present": False},), columns=("present",)),
            "index_fingerprint": SqlResult(rows=(), columns=("indexed_at",)),
        }
    )


class FakeContainer:
    """Fake async DI container backing the export's direct bus resolution.

    `_dispatch_export` resolves the fully-routed `MessageBus` directly, so this fake returns
    the pre-wired bus for that key, plus the identity announce's `AdminSqlGateway` and `JobQueries`.
    """

    def __init__(self, bus: MessageBus) -> None:
        self._bus = bus
        self._gateway = _identity_gateway()
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
    """Zero-arg `ctx.obj` factory returning a container around a recording export handler."""

    def __init__(self, report: OkfExportReport) -> None:
        self.handler = RecordingCommandHandler(result=report)
        self._bus = MessageBus({ExportOkf: self.handler}, {})

    def __call__(self) -> FakeContainer:
        return FakeContainer(self._bus)


class FailingContainerFactory:
    """Factory whose container raises on resolution — the clean-failure path."""

    def __call__(self) -> object:
        return object()  # no `get` → the direct MessageBus resolution raises → command exits 1 cleanly


def _report() -> OkfExportReport:
    """A successful OKF export report."""
    return OkfExportReport(concepts=4, skipped_meta=2, relations=3, dangling=1, files_written=5, duration_seconds=0.2)


class TestExportOkf:
    def test_successful_export_prints_report_and_exits_zero(self):
        factory = FakeContainerFactory(_report())

        result = runner.invoke(app, ["export", "okf", "--out", "out/bundle"], obj=factory)

        assert result.exit_code == 0
        assert "4 concepts" in result.stdout
        assert "2 meta pages skipped" in result.stdout
        assert "Files written: 5" in result.stdout

    def test_out_and_collection_are_plumbed_to_the_command(self):
        factory = FakeContainerFactory(_report())

        runner.invoke(app, ["export", "okf", "--out", "out/bundle", "--collection", "research"], obj=factory)

        command = factory.handler.handled[0]
        assert isinstance(command, ExportOkf)
        assert command.out_dir == "out/bundle"
        assert command.collection == "research"

    def test_dispatch_failure_is_a_clean_exit_one(self):
        result = runner.invoke(app, ["export", "okf", "--out", "out/bundle"], obj=FailingContainerFactory())

        assert result.exit_code == 1
        assert "export failed" in result.stdout

    def test_an_explicit_knowledge_base_flag_is_never_refused_from_inside_another_knowledge_base(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        named = tmp_path / "named"
        (named / "Units").mkdir(parents=True)
        (named / "_meta").mkdir()
        (named / "knowledge-base-schema.json").write_text(json.dumps({"slug": "named", "page_types": [{"page_type": "note"}]}), encoding="utf-8")
        cwd = tmp_path / "alpha-cwd"
        cwd.mkdir()
        (cwd / "knowledge-base-schema.json").write_text(json.dumps({"slug": "alpha", "page_types": [{"page_type": "note"}]}), encoding="utf-8")
        monkeypatch.chdir(cwd)
        factory = FakeContainerFactory(_report())

        result = runner.invoke(app, ["export", "okf", "--out", "out/bundle", "--knowledge-base", str(named)], obj=factory)

        assert result.exit_code == 0, result.stdout
        assert "two knowledge bases in play." not in result.stderr


class TestHelp:
    def test_export_help_lists_okf(self):
        result = runner.invoke(app, ["export", "--help"])

        assert result.exit_code == 0
        assert "okf" in result.stdout

    def test_okf_help_documents_out(self):
        result = runner.invoke(app, ["export", "okf", "--help"])

        assert result.exit_code == 0
        assert "out" in result.stdout
