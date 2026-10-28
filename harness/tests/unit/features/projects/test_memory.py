"""The three memory mounts: the adapted toolset's note grammar, the plugin, and the global gate."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, cast
from uuid import uuid4

import pytest

from core.ai.workflows.contracts import StepRegistry
from engram.agent_memory.contracts import EngramMemoryStore
from engram.testing.fakes.agent_memory import FakeCards, FakeIngest, FakeSearchHits
from pydantic_ai import Agent, ModelRetry
from pydantic_ai.capabilities import AbstractCapability, CombinedCapability
from pydantic_ai.messages import ModelMessage, ModelRequest, ModelResponse, TextContent, TextPart, UserPromptPart
from pydantic_ai.models.function import AgentInfo, FunctionModel
from pydantic_ai.models.test import TestModel
from pydantic_ai.tools import DeferredToolRequests, RunContext
from pydantic_ai.usage import RunUsage
from pydantic_ai_harness.memory import InMemoryStore, MemoryFile, MemoryMutation

from harness import plugins
from harness.features.authoring.agent import build_agent
from harness.features.authoring.process import WorkflowProcess
from harness.features.projects import memory as memory_module
from harness.features.running.spend_report import SpendReporter
from harness.features.scheduling.contracts import Scheduler
from harness.features.sessions.project_config import ProjectConfig
from harness.session import NoSessionLog, Runtime, SessionPool
from harness.settings import RunSettings, WebSearchSettings
from tests.support.fakes.broadcaster import FakeBroadcaster
from tests.support.fakes.request_parameters import FakeRequestParametersSink
from tests.support.fakes.schedules import FakeSchedules
from tests.support.fakes.sessions import APool
from tests.support.fakes.telemetry import FakeTelemetry
from tests.support.runtime import a_runtime
from tests.support.state import a_state, a_throwaway_state, a_transcript, records_under

if TYPE_CHECKING:
    from collections.abc import Callable, Sequence
    from pathlib import Path
    from typing import Any

    from core.ai.workflows.contracts import WorkflowContract
    from pydantic_ai_harness.memory import MemoryOperation


def _runtime(*, session_id: str | None = None) -> Runtime:
    """A `Runtime` with everything the memory mounts touch (`session_id`) and nothing else real."""
    return Runtime(registry=StepRegistry(), library=cast("Any", None), agent=cast("Any", None), session_id=session_id, session_log=NoSessionLog(), state=a_throwaway_state())


def _ctx(*, session_id: str | None = None, tool_call_id: str | None = None, run_id: str = "run-1") -> RunContext[Runtime]:
    """A fresh `tool_call_id` per call by default -- distinct tool calls within one run, `_operation`'s key."""
    return RunContext(
        deps=_runtime(session_id=session_id),
        model=TestModel(),
        usage=RunUsage(),
        tool_call_id=tool_call_id if tool_call_id is not None else str(uuid4()),
        run_id=run_id,
    )


def _all_text(result: object) -> str:
    """Every string across a run's messages, instructions and the injected user block both."""
    return _text_of(result.all_messages())  # type: ignore[attr-defined]


def _text_of(messages: Sequence[ModelMessage]) -> str:
    """Every instruction and user-prompt string across `messages`."""
    chunks: list[str] = []
    for message in messages:
        if not isinstance(message, ModelRequest):
            continue
        if message.instructions:
            chunks.append(message.instructions)
        for part in message.parts:
            if not isinstance(part, UserPromptPart):
                continue
            if isinstance(part.content, str):
                chunks.append(part.content)
            else:
                chunks.extend(item.content for item in part.content if isinstance(item, TextContent))
    return "\n".join(chunks)


class _GrammarStore:
    """Mimics `engram.agent_memory.EngramMemoryStore.write`'s one grammar rule under test.

    A dotted path segment (outside the trailing `.md`) is refused with a `ValueError`. Everything
    else behaves like a trivial in-process store -- enough for `write_memory` to reach the refusal.
    """

    def __init__(self) -> None:
        self.written: dict[str, str] = {}

    async def read(self, path: str, *, max_chars: int) -> MemoryFile | None:
        content = self.written.get(path)
        if content is None:
            return None
        return MemoryFile(content=content[:max_chars], version="1", operation_id=None, truncated=len(content) > max_chars)

    async def get_operation(self, operation: MemoryOperation) -> MemoryMutation | None:
        del operation
        return None

    async def write(
        self, path: str, content: str, *, expected_version: str | None, operation: MemoryOperation | None = None
    ) -> MemoryMutation:
        del expected_version, operation
        segments = path.split("/")
        stripped = [*segments[:-1], segments[-1].removesuffix(".md")]
        if any("." in segment for segment in stripped):
            msg = f"memory path segments cannot contain dots: {path!r}"
            raise ValueError(msg)
        existed = path in self.written
        self.written[path] = content
        return MemoryMutation(version="2", replayed=False, existed=existed)

    async def delete(
        self, path: str, *, expected_version: str | None, operation: MemoryOperation | None = None
    ) -> MemoryMutation:
        raise NotImplementedError

    async def list_paths(self, prefix: str = "", *, limit: int) -> list[str]:
        del prefix, limit
        return []


class _NotSearchable:
    """A `MemoryStore` with no `search` -- the M2 "cannot search" path, not returned-empty."""

    async def read(self, path: str, *, max_chars: int) -> MemoryFile | None:
        del path, max_chars
        return None

    async def get_operation(self, operation: MemoryOperation) -> MemoryMutation | None:
        del operation
        return None

    async def write(
        self, path: str, content: str, *, expected_version: str | None, operation: MemoryOperation | None = None
    ) -> MemoryMutation:
        raise NotImplementedError

    async def delete(
        self, path: str, *, expected_version: str | None, operation: MemoryOperation | None = None
    ) -> MemoryMutation:
        raise NotImplementedError

    async def list_paths(self, prefix: str = "", *, limit: int) -> list[str]:
        del prefix, limit
        return []


class _RenderedIndexStore:
    """Mimics `EngramMemoryStore`'s own derived index: `MEMORY.md` answers full scope-prefixed links."""

    def __init__(self, rendered: str) -> None:
        self._rendered = rendered

    async def read(self, path: str, *, max_chars: int) -> MemoryFile | None:
        if path.rsplit("/", 1)[-1] != "MEMORY.md":
            return None
        return MemoryFile(content=self._rendered[:max_chars], version="derived", operation_id=None, truncated=False)

    async def get_operation(self, operation: MemoryOperation) -> MemoryMutation | None:
        raise NotImplementedError

    async def write(
        self, path: str, content: str, *, expected_version: str | None, operation: MemoryOperation | None = None
    ) -> MemoryMutation:
        raise NotImplementedError

    async def delete(
        self, path: str, *, expected_version: str | None, operation: MemoryOperation | None = None
    ) -> MemoryMutation:
        raise NotImplementedError

    async def list_paths(self, prefix: str = "", *, limit: int) -> list[str]:
        raise NotImplementedError


class TestSanitizeAndNamespace:
    def test_sanitize_segment_replaces_dots_and_slashes_with_hyphens(self) -> None:
        sanitized = memory_module.sanitize_segment("abc/../x")

        assert "." not in sanitized
        assert "/" not in sanitized
        assert sanitized == "abc----x"

    def test_session_namespace_sanitizes_a_session_id_carrying_dots_and_slashes(self) -> None:
        namespace = memory_module.session_namespace(_ctx(session_id="abc/../x"))

        assert "." not in namespace
        assert "/" not in namespace
        assert namespace.startswith("session-abc")

    def test_session_namespace_falls_back_to_main_when_unset(self) -> None:
        assert memory_module.session_namespace(_ctx()) == "session-main"


class TestCategory:
    def test_category_mirrors_engram_agent_memory_categories(self) -> None:
        from typing import get_args

        from engram import CATEGORIES

        assert frozenset(get_args(memory_module.Category)) == frozenset(CATEGORIES)


class TestCombinedGuidance:
    def test_names_every_mount_and_its_prefixed_tools_once(self) -> None:
        compose = memory_module.combined_memory_guidance(global_memory_enabled=lambda: True)
        text = compose(_ctx())

        for prefix in ("global", "project", "scratchpad"):
            assert f"{prefix}_write_memory" in text
            assert text.count(f"`{prefix}_write_memory`") == 1, "said once, not once per mount"
        assert "category" in text
        assert text.count("note's KIND") == 1, "the category/slug rule is explained once, not once per mount"
        assert text.count("MEMORY.md") == 1, "the refusal is stated once"
        assert "run_code" in text

    def test_drops_the_global_clause_when_its_gate_is_off(self) -> None:
        compose = memory_module.combined_memory_guidance(global_memory_enabled=lambda: False)
        text = compose(_ctx())

        assert "global_write_memory" not in text
        assert "project_write_memory" in text
        assert "scratchpad_write_memory" in text

    def test_reflects_the_gate_fresh_each_call(self) -> None:
        enabled = True

        def gate() -> bool:
            return enabled

        compose = memory_module.combined_memory_guidance(global_memory_enabled=gate)
        assert "global_write_memory" in compose(_ctx())

        enabled = False
        assert "global_write_memory" not in compose(_ctx())


class TestMemoryMounts:
    def test_returns_three_capabilities(self) -> None:
        mounts = memory_module.memory_mounts(InMemoryStore(), project_namespace="project-abc", global_memory_enabled=lambda: True)

        assert len(mounts) == 3

    async def test_the_global_memory_block_is_injected_only_while_the_gate_says_on(self) -> None:
        """Drive it through the agent: the static heading and the injected user block both toggle.

        The injected listing also names the note by its relative path, not the internal
        `<namespace>/<agent>/...` store path -- the index-leak this mount must not repeat.
        """
        store = InMemoryStore()
        seeding = memory_module.memory_mounts(store, project_namespace="project-abc", global_memory_enabled=lambda: True)
        seed_capability = seeding[0].capability_func(_ctx())  # type: ignore[attr-defined]
        assert isinstance(seed_capability, memory_module.NoteMemory)
        await seed_capability.get_toolset().write_memory(_ctx(), category="world", slug="postgres-port", content="Postgres runs on 5433.")

        async def _seen_by_the_model(*, global_memory_enabled: Callable[[], bool]) -> str:
            seen: list[ModelMessage] = []

            def _answer(messages: list[ModelMessage], _info: AgentInfo) -> ModelResponse:
                seen.extend(messages)
                return ModelResponse(parts=[TextPart("ok")])

            mounts = memory_module.memory_mounts(store, project_namespace="project-abc", global_memory_enabled=global_memory_enabled)
            agent = Agent(FunctionModel(_answer), deps_type=Runtime, output_type=str, capabilities=list(mounts))
            await agent.run("hi", deps=_runtime())
            return _text_of(seen)

        enabled = await _seen_by_the_model(global_memory_enabled=lambda: True)
        disabled = await _seen_by_the_model(global_memory_enabled=lambda: False)

        assert "Global memory" in enabled
        assert "world/postgres-port.md" in enabled
        assert "global/main/world/postgres-port.md" not in enabled
        assert "Global memory" not in disabled
        assert "world/postgres-port.md" not in disabled

    async def test_the_combined_guidance_renders_once_across_all_three_mounts(self) -> None:
        """Three mounts, one paragraph -- not the three near-identical ones this replaces."""
        mounts = memory_module.memory_mounts(InMemoryStore(), project_namespace="project-abc", global_memory_enabled=lambda: True)
        agent = Agent(TestModel(call_tools=[]), deps_type=Runtime, output_type=str, capabilities=list(mounts))

        result = await agent.run("hi", deps=_runtime())

        text = _all_text(result)
        assert text.count("global_write_memory") == 1
        assert text.count("project_write_memory") == 1
        assert text.count("scratchpad_write_memory") == 1
        assert text.count("MEMORY.md") == 1


class TestNoteIndexStore:
    """`_NoteIndexStore`: the injected `MEMORY.md` always links `<category>/<slug>.md`, relative."""

    async def test_relativizes_an_already_rendered_index(self) -> None:
        inner = _RenderedIndexStore("- [postgres-port](global/main/world/postgres-port.md) — Postgres runs on 5433.")
        wrapped = memory_module._NoteIndexStore(inner)

        result = await wrapped.read("global/main/MEMORY.md", max_chars=10_000)

        assert result is not None
        assert "](world/postgres-port.md)" in result.content
        assert "global/main/" not in result.content

    async def test_derives_a_listing_when_the_inner_store_has_no_index(self) -> None:
        inner = InMemoryStore()
        await inner.write("notebook/main/world/postgres-port.md", "Postgres runs on 5433.\n", expected_version=None)
        wrapped = memory_module._NoteIndexStore(inner)

        result = await wrapped.read("notebook/main/MEMORY.md", max_chars=10_000)

        assert result is not None
        assert "world/postgres-port.md" in result.content

    async def test_non_index_reads_pass_straight_through(self) -> None:
        inner = InMemoryStore()
        await inner.write("notebook/main/world/postgres-port.md", "Postgres runs on 5433.\n", expected_version=None)
        wrapped = memory_module._NoteIndexStore(inner)

        result = await wrapped.read("notebook/main/world/postgres-port.md", max_chars=10_000)

        assert result is not None
        assert "5433" in result.content


class TestAdaptedToolset:
    async def test_write_composes_the_category_path_under_the_scope(self) -> None:
        store = InMemoryStore()
        capability = memory_module.NoteMemory(store=store, namespace="notebook")
        toolset = capability.get_toolset()

        result = await toolset.write_memory(_ctx(), category="world", slug="postgres-port", content="Runs on 5433.")

        assert result["file"] == "world/postgres-port.md"
        assert result["status"] == "created"
        stored = await store.read("notebook/main/world/postgres-port.md", max_chars=10_000)
        assert stored is not None
        assert "Runs on 5433." in stored.content

    async def test_write_and_read_accept_a_relative_path_instead_of_category_and_slug(self) -> None:
        toolset = memory_module.NoteMemory(store=InMemoryStore(), namespace="notebook").get_toolset()

        await toolset.write_memory(_ctx(), path="world/postgres-port.md", content="Postgres runs on 5433.")
        content = await toolset.read_memory(_ctx(), path="world/postgres-port.md")

        assert "5433" in content

    async def test_a_search_matchs_file_feeds_straight_into_read_memory_as_path(self) -> None:
        toolset = memory_module.NoteMemory(store=InMemoryStore(), namespace="notebook").get_toolset()
        ctx = _ctx()
        await toolset.write_memory(ctx, category="world", slug="postgres-port", content="Postgres runs on 5433.")

        found = await toolset.search_memory(_ctx(), "postgres")
        content = await toolset.read_memory(_ctx(), path=found["matches"][0]["file"])

        assert "5433" in content

    async def test_passing_both_category_and_path_is_refused(self) -> None:
        toolset = memory_module.NoteMemory(store=InMemoryStore(), namespace="notebook").get_toolset()

        with pytest.raises(ModelRetry):
            await toolset.write_memory(_ctx(), category="world", slug="x", path="world/x.md", content="y")

    async def test_passing_neither_category_slug_nor_path_is_refused(self) -> None:
        toolset = memory_module.NoteMemory(store=InMemoryStore(), namespace="notebook").get_toolset()

        with pytest.raises(ModelRetry):
            await toolset.read_memory(_ctx())

    async def test_write_read_and_delete_round_trip(self) -> None:
        store = InMemoryStore()
        toolset = memory_module.NoteMemory(store=store, namespace="notebook").get_toolset()

        await toolset.write_memory(_ctx(), category="reference", slug="onboarding", content="Read the runbook first.")
        read_back = await toolset.read_memory(_ctx(), category="reference", slug="onboarding")
        deleted = await toolset.delete_memory(_ctx(), category="reference", slug="onboarding")

        assert "Read the runbook first." in read_back
        assert deleted["status"] == "deleted"
        with pytest.raises(ModelRetry):
            await toolset.read_memory(_ctx(), category="reference", slug="onboarding")

    async def test_search_returns_a_match_at_its_real_nested_path(self) -> None:
        toolset = memory_module.NoteMemory(store=InMemoryStore(), namespace="notebook").get_toolset()
        ctx = _ctx()
        await toolset.write_memory(ctx, category="world", slug="postgres-port", content="Postgres runs on 5433.")

        found = await toolset.search_memory(_ctx(), "postgres")

        assert found["matches"]
        assert found["matches"][0]["file"] == "world/postgres-port.md"

    async def test_search_honors_the_capabilitys_own_bounds(self) -> None:
        store = InMemoryStore()
        toolset = memory_module.NoteMemory(store=store, namespace="notebook", max_search_results=1).get_toolset()
        await toolset.write_memory(_ctx(), category="world", slug="a", content="postgres one")
        await toolset.write_memory(_ctx(), category="world", slug="b", content="postgres two")

        found = await toolset.search_memory(_ctx(), "postgres")

        assert len(found["matches"]) == 1

    async def test_search_ranks_notes_from_two_category_folders_within_its_bounds(self) -> None:
        toolset = memory_module.NoteMemory(
            store=InMemoryStore(), namespace="notebook", max_search_results=1, max_search_result_chars=40
        ).get_toolset()
        await toolset.write_memory(_ctx(), category="world", slug="port", content="postgres answers on 5433")
        await toolset.write_memory(_ctx(), category="reference", slug="tuning", content="postgres postgres postgres tuning")

        found = await toolset.search_memory(_ctx(), "postgres")

        assert [match["file"] for match in found["matches"]] == ["reference/tuning.md"]
        assert len(found["matches"][0]["snippet"]) <= 40 - len("notebook/main/reference/tuning.md")
        assert found["truncated"] is True

    async def test_one_ranked_search_answers_a_scope_on_engrams_store(self) -> None:
        hits = FakeSearchHits([("mem.proj.main.world.postgres-port", "Answers on 5433.", 0.9)])
        store = EngramMemoryStore(InMemoryStore(), FakeIngest(), FakeCards(), search_hits=hits)
        project = memory_module.memory_mounts(store, project_namespace="proj", global_memory_enabled=lambda: False)[1]
        assert isinstance(project, memory_module.NoteMemory)

        found = await project.get_toolset().search_memory(_ctx(), "postgres")

        assert [(query, prefix) for query, prefix, _ in hits.calls] == [("postgres", "proj/main/")]
        assert [match["file"] for match in found["matches"]] == ["world/postgres-port.md"]

    async def test_search_on_a_non_searchable_store_says_so_instead_of_returning_empty(self) -> None:
        toolset = memory_module.NoteMemory(store=_NotSearchable(), namespace="notebook").get_toolset()

        with pytest.raises(ModelRetry, match="cannot search"):
            await toolset.search_memory(_ctx(), "postgres")

    async def test_a_grammar_refusal_surfaces_as_model_retry_not_value_error(self) -> None:
        toolset = memory_module.NoteMemory(store=_GrammarStore(), namespace="notebook").get_toolset()

        with pytest.raises(ModelRetry):
            await toolset.write_memory(_ctx(), category="world", slug="postgres.port", content="bad slug")

    async def test_a_model_retry_names_the_tool_the_model_can_actually_call(self) -> None:
        toolset = memory_module.NoteMemory(store=InMemoryStore(), namespace="notebook", tool_prefix="global").get_toolset()

        with pytest.raises(ModelRetry, match="global_search_memory"):
            await toolset.read_memory(_ctx(), category="world", slug="missing")

    def test_the_toolset_id_carries_the_mount_prefix(self) -> None:
        toolset = memory_module.NoteMemory(store=InMemoryStore(), namespace="notebook", tool_prefix="global").get_toolset()

        assert toolset.id == "memory-global"

    async def test_write_refuses_to_append_onto_a_truncated_read(self) -> None:
        store = InMemoryStore()
        await store.write("notebook/main/world/long-note.md", "x" * 100, expected_version=None)
        toolset = memory_module.NoteMemory(store=store, namespace="notebook", max_memory_size=10).get_toolset()

        with pytest.raises(ModelRetry):
            await toolset.write_memory(_ctx(), category="world", slug="long-note", content="more")

    async def test_read_appends_the_truncation_marker_instead_of_a_silent_prefix(self) -> None:
        store = InMemoryStore()
        await store.write("notebook/main/world/long-note.md", "x" * 100, expected_version=None)
        toolset = memory_module.NoteMemory(store=store, namespace="notebook", max_memory_size=10).get_toolset()

        content = await toolset.read_memory(_ctx(), category="world", slug="long-note")

        assert "Truncated" in content

    async def test_a_replayed_tool_call_does_not_double_append(self) -> None:
        store = InMemoryStore()
        toolset = memory_module.NoteMemory(store=store, namespace="notebook").get_toolset()
        ctx = _ctx(tool_call_id="tc-fixed")

        first = await toolset.write_memory(ctx, category="world", slug="postgres-port", content="Postgres runs on 5433.")
        second = await toolset.write_memory(ctx, category="world", slug="postgres-port", content="Postgres runs on 5433.")

        assert first == second
        stored = await store.read("notebook/main/world/postgres-port.md", max_chars=10_000)
        assert stored is not None
        assert stored.content.count("5433") == 1


@dataclass
class _NoResearch(AbstractCapability[Runtime]):
    """Fills `build_agent`'s required research slot; the mount under test is memory."""


def _build(tmp_path: Path, *, memory: tuple[AbstractCapability[Runtime], ...] = ()) -> Agent[Runtime, str | DeferredToolRequests]:
    from core.ai.workflows.contracts import WorkflowContract
    from core.ai.workflows.infrastructure.folder import FOLDER, folder_toolset

    port: WorkflowContract[Runtime] = WorkflowContract[Runtime](
        source=folder_toolset(tmp_path / "workflows"), vocabulary=FOLDER, expose=(), registry=StepRegistry(), saved_by="test"
    )
    return build_agent(
        TestModel(),
        WorkflowProcess(port=port, records=records_under(tmp_path), reporter=SpendReporter(FakeTelemetry()), scheduler=Scheduler(FakeSchedules(), FakeSchedules(), FakeBroadcaster()), sessions=APool(SessionPool(a_runtime())), topics=FakeBroadcaster(), run=RunSettings()),
        _NoResearch(),
        state=a_state(tmp_path),
        custom=tmp_path / "custom",
        web_search=WebSearchSettings(),
        store=a_transcript(tmp_path),
        parameters=FakeRequestParametersSink(),
        external_agents=dict,
        project=ProjectConfig(),
        memory=memory,
    )


def _memory_switched(tmp_path: Path) -> plugins.Switched[Runtime]:
    """One `Switched` around all three mounts, mirroring `composition.py`'s own wiring.

    Three separate `Switched(mount, plugin="memory", state=a_state(tmp_path))` instances would each mint the identical
    `plugin:memory` capability id, which `Agent.__init__` refuses as a duplicate --
    `CombinedCapability` is what bundles the three mounts into the one capability `Switched` wraps.
    """
    mounts = memory_module.memory_mounts(InMemoryStore(), project_namespace="project-abc", global_memory_enabled=lambda: True)
    return plugins.Switched(CombinedCapability[Runtime](capabilities=list(mounts)), plugin="memory", state=a_state(tmp_path))


class TestBuildAgentMemoryDoor:
    def test_build_agent_mounts_all_three_memory_capabilities_behind_the_memory_door(self, tmp_path: Path) -> None:
        agent = _build(tmp_path, memory=(_memory_switched(tmp_path),))

        switched: list[plugins.Switched[Runtime]] = []
        agent._root_capability.apply(
            lambda cap: switched.append(cap) if isinstance(cap, plugins.Switched) and cap.plugin == "memory" else None
        )

        assert len(switched) == 1
        combined = switched[0].wrapped
        assert isinstance(combined, CombinedCapability)
        assert len(combined.capabilities) == 3

    def test_the_door_closed_mounts_none_of_the_three(self, tmp_path: Path) -> None:
        agent = _build(tmp_path, memory=(_memory_switched(tmp_path),))
        switched: list[plugins.Switched[Runtime]] = []
        agent._root_capability.apply(
            lambda cap: switched.append(cap) if isinstance(cap, plugins.Switched) and cap.plugin == "memory" else None
        )
        plugins.set_plugin(a_state(tmp_path), "memory", enabled=False)

        assert switched[0].capability_func(_ctx()) is None
