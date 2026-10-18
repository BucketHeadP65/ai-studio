"""A command with no `--knowledge-base`, run outside any knowledge base, refuses cleanly and does no work."""

from __future__ import annotations

import json

from typing import TYPE_CHECKING

import pytest

from core.cqrs.contracts import JobQueries, MemoryJobs, MessageBus
from engram.indexing.contracts import IndexKnowledgeBase, IndexReport, KnowledgeBaseReader
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

_REFUSAL = "No knowledge base named. Pass --knowledge-base PATH, or run inside a knowledge base (a folder holding knowledge-base-schema.json)."


@pytest.fixture(autouse=True)
def _schema_at_head(monkeypatch: pytest.MonkeyPatch) -> None:
    """The preflight needs a live database. The same stand-in `test_cli_index.py` already uses."""
    from studio.knowledge_base import cli as cli_module

    monkeypatch.setattr(cli_module, "assert_schema_at_head", lambda _db_url: None)


def _gateway() -> FakeSqlGateway:
    return FakeSqlGateway(
        routed={
            "to_regclass": SqlResult(rows=({"present": False},), columns=("present",)),
            "index_fingerprint": SqlResult(rows=(), columns=("indexed_at",)),
        }
    )


class FakeContainer:
    def __init__(self, bus: MessageBus) -> None:
        self._bus = bus
        self._gateway = _gateway()
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
    def __init__(self) -> None:
        self.handler = RecordingCommandHandler(result=IndexReport())
        self.built = 0
        self._bus = MessageBus({IndexKnowledgeBase: self.handler}, {})

    def __call__(self) -> FakeContainer:
        self.built += 1
        return FakeContainer(self._bus)


def _write_spec(directory: Path, slug: str) -> None:
    (directory / "knowledge-base-schema.json").write_text(json.dumps({"slug": slug, "page_types": [{"page_type": "note"}]}), encoding="utf-8")


class TestRefusal:
    def test_outside_any_knowledge_base_exits_one_names_the_fix_and_builds_nothing(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        factory = FakeContainerFactory()
        monkeypatch.chdir(tmp_path)

        result = runner.invoke(app, ["index"], obj=factory)

        assert result.exit_code == 1
        assert _REFUSAL in result.stdout
        assert factory.built == 0
        assert factory.handler.handled == []

    def test_an_explicit_knowledge_base_flag_is_never_refused(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        factory = FakeContainerFactory()
        named = tmp_path / "named"
        named.mkdir()
        _write_spec(named, "named")
        monkeypatch.chdir(tmp_path)

        result = runner.invoke(app, ["index", "--knowledge-base", str(named)], obj=factory)

        assert result.exit_code == 0
        assert factory.built == 1

    def test_inside_a_knowledge_base_the_working_directory_is_used(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        factory = FakeContainerFactory()
        cwd = tmp_path / "alpha-cwd"
        cwd.mkdir()
        _write_spec(cwd, "alpha")
        monkeypatch.chdir(cwd)

        result = runner.invoke(app, ["index"], obj=factory)

        assert result.exit_code == 0
        assert factory.built == 1


class TestRefusalSurvivesTheNoteCommandsSeam:
    """The refusal reaches `note` commands too, through a resolution path that never touches a container."""

    def test_note_validate_exits_one_and_names_the_fix(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.chdir(tmp_path)

        result = runner.invoke(app, ["note", "validate", str(tmp_path / "missing-note.md")])

        assert result.exit_code == 1
        assert _REFUSAL in result.stdout

    def test_note_validate_with_an_explicit_knowledge_base_is_never_refused(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        named = tmp_path / "named"
        named.mkdir()
        _write_spec(named, "named")
        (named / "_meta" / "vocabulary").mkdir(parents=True)
        note_path = tmp_path / "hello.md"
        note_path.write_text(
            "---\nid: note.hello\ntype: note\ntitle: Hello\nstatus: draft\n---\n\n## Overview\n\nHello world.\n", encoding="utf-8"
        )
        monkeypatch.chdir(tmp_path)

        result = runner.invoke(app, ["note", "validate", str(note_path), "--knowledge-base", str(named)])

        assert _REFUSAL not in result.stdout
        assert result.exit_code == 0
        assert "validate hello.md: OK" in result.stdout
