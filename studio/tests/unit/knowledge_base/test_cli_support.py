"""Unit tests for the command line helpers (`src/studio/knowledge_base/cli_support.py`)."""

from __future__ import annotations

from typing import TYPE_CHECKING, cast

import pytest

from core.cqrs.contracts import MemoryOutbox, MessageBus
from engram.config import KnowledgeBaseSettings
from engram.indexing.contracts import CitingPagesChanged, IndexKnowledgeBase, IndexReport
from engram.testing.fakes.bus_spy import BusSpy

from studio.knowledge_base.cli_support import (
    KnowledgeBaseMissingError,
    _dispatch_index,
    _resolve_knowledge_base_for_authoring,
    knowledge_base_path_origin,
    resolve_knowledge_base_root,
)

if TYPE_CHECKING:
    from pathlib import Path

    from core.cqrs.contracts import Command, Event


class TestKnowledgeBasePathOrigin:
    """Where the active knowledge base root came from: the flag, or the working directory."""

    def test_an_explicit_override_names_the_knowledge_base_flag(self) -> None:
        assert knowledge_base_path_origin("/a") == "--knowledge-base"

    def test_no_override_names_the_working_directory(self) -> None:
        assert knowledge_base_path_origin(None) == "working directory"


class TestResolveKnowledgeBaseRoot:
    """`--knowledge-base` when given, else the knowledge base the working directory sits inside; neither refuses, naming the fix."""

    def test_the_flag_wins_even_inside_another_knowledge_base(self, tmp_path: Path) -> None:
        here = tmp_path / "here"
        here.mkdir()
        (here / "knowledge-base-schema.json").write_text("{}", encoding="utf-8")
        chosen = tmp_path / "chosen"
        chosen.mkdir()

        assert resolve_knowledge_base_root(str(chosen), cwd=here) == chosen.resolve()

    def test_no_flag_inside_a_knowledge_base_is_that_knowledge_base(self, tmp_path: Path) -> None:
        here = tmp_path / "here"
        here.mkdir()
        (here / "knowledge-base-schema.json").write_text("{}", encoding="utf-8")

        assert resolve_knowledge_base_root(None, cwd=here) == here.resolve()

    def test_no_flag_outside_a_knowledge_base_refuses_and_names_the_fix(self, tmp_path: Path) -> None:
        refusal = r"No knowledge base named\. Pass --knowledge-base PATH, or run inside a knowledge base \(a folder holding knowledge-base-schema\.json\)\."
        with pytest.raises(KnowledgeBaseMissingError, match=refusal):
            resolve_knowledge_base_root(None, cwd=tmp_path)

    def test_a_nested_cwd_walks_up_to_the_knowledge_base_root(self, tmp_path: Path) -> None:
        root = tmp_path / "knowledge_base"
        root.mkdir()
        (root / "knowledge-base-schema.json").write_text("{}", encoding="utf-8")
        nested = root / "Units" / "topic"
        nested.mkdir(parents=True)

        assert resolve_knowledge_base_root(None, cwd=nested) == root.resolve()


class TestResolveKnowledgeBaseForAuthoring:
    """`(knowledge_base_root, sources_subdir)` for the `note build`/`verify` authoring commands."""

    def test_the_flag_wins_and_the_subdir_is_read_from_knowledge_base_settings_not_a_literal(self, tmp_path: Path) -> None:
        here = tmp_path / "here"
        here.mkdir()
        chosen = tmp_path / "chosen"
        chosen.mkdir()
        (chosen / "knowledge-base-schema.json").write_text("{}", encoding="utf-8")

        root, sources_subdir = _resolve_knowledge_base_for_authoring(
            str(chosen), settings_for=lambda path: KnowledgeBaseSettings(path=path, sources_subdir="Mirrors")
        )

        assert root == chosen.resolve()
        # "Mirrors" is not `KnowledgeBaseSettings.sources_subdir`'s own default ("Sources"), so this
        # only passes if the function actually reads the injected settings' value.
        assert sources_subdir == "Mirrors"

    def test_no_flag_inside_a_knowledge_base_resolves_that_knowledge_base(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        here = tmp_path / "here"
        here.mkdir()
        (here / "knowledge-base-schema.json").write_text("{}", encoding="utf-8")
        monkeypatch.chdir(here)

        root, sources_subdir = _resolve_knowledge_base_for_authoring(None)

        assert root == here.resolve()
        assert sources_subdir == "Sources"


class _EmittingHandler:
    """A command handler that collects one event, the way the index handler's fallback path does."""

    def __init__(self, report: IndexReport, event: Event) -> None:
        self.events: list[Event] = [event]
        self._report = report

    async def __call__(self, command: Command) -> object:
        del command
        return self._report


class _BusContainer:
    """A resolver that only ever hands back the one bus it was built with."""

    def __init__(self, bus: MessageBus) -> None:
        self._bus = bus

    async def get[T](self, klass: type[T]) -> T:
        if klass is MessageBus:
            return cast("T", self._bus)
        msg = f"_BusContainer has no registration for {klass!r}"
        raise KeyError(msg)


class TestDispatchIndexDelivers:
    """A one-shot command has no worker of its own, so it delivers what it produced before returning."""

    async def test_the_handlers_event_reaches_its_subscriber(self) -> None:
        spy = BusSpy()
        handler = _EmittingHandler(IndexReport(pages=1), CitingPagesChanged(collection="research", page_ids=("paper.a",)))
        outbox = MemoryOutbox()
        bus = MessageBus({IndexKnowledgeBase: handler}, {CitingPagesChanged: [spy]}, outbox=outbox)

        report = await _dispatch_index(_BusContainer(bus), IndexKnowledgeBase(collection="research"))

        assert report.pages == 1
        assert spy.received == [CitingPagesChanged(collection="research", page_ids=("paper.a",))]
        assert outbox.closed == {outbox.appended[0].event_id}

    async def test_an_event_nobody_subscribes_to_is_closed_rather_than_left_behind(self) -> None:
        handler = _EmittingHandler(IndexReport(pages=1), CitingPagesChanged(collection="research", page_ids=("paper.a",)))
        outbox = MemoryOutbox()
        bus = MessageBus({IndexKnowledgeBase: handler}, {}, outbox=outbox)

        await _dispatch_index(_BusContainer(bus), IndexKnowledgeBase(collection="research"))

        assert await outbox.undelivered() == []
