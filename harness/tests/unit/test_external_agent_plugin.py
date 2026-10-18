"""The external-agent plugin: one tool that drives another harness over ACP.

The double is a real subprocess (`tests/support/acp_echo_agent.py`), so the plugin is proved against
the wire rather than against a stub of it. The two seams this plugin has are arguments, not patches:
the registry is a callable and the session log and the asker are read off the session.
"""

from __future__ import annotations

import sys

from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Literal

from core.ai.workflows.contracts import StepRegistry
from core.ai.workflows.infrastructure.folder import FolderLibrary
from pydantic_ai import Agent, RunContext
from pydantic_ai.capabilities import Toolset
from pydantic_ai.models.test import TestModel
from pydantic_ai.toolsets import FunctionToolset
from pydantic_ai.usage import RunUsage

from harness.composition import build_environment
from harness.domain.events import ExternalAgentFinished, ExternalAgentStarted, ExternalAgentUpdated
from harness.features.authoring.workers import EXTERNAL_AGENT_TOOL, external_agent_plugin, run_external_agent_step
from harness.session import ActiveRun, NoSessionLog, Runtime
from harness.settings import ExternalAgentEntry, WebSearchSettings
from tests.support.environment import fake_engram
from tests.support.state import a_state, records_under
from tests.support.tiers import tiers_under

if TYPE_CHECKING:
    from collections.abc import Mapping

    from harness.domain.events import Recorded, SessionEvent
    from harness.settings import ExternalAgentRegistry

ECHO_AGENT = Path(__file__).resolve().parents[1] / "support" / "acp_echo_agent.py"


@dataclass
class FakeJournal:
    """A `SessionEvents` that keeps what it was told, in order."""

    recorded: list[SessionEvent] = field(default_factory=list)

    def record(self, event: SessionEvent) -> Recorded | None:
        self.recorded.append(event)
        return None

    def events(self) -> tuple[Recorded, ...]:
        """Nothing is read back off this log."""
        return ()

    def boot_sweep(self) -> None:
        """Nothing to bring up to date: this log holds only what a test put in it."""


@dataclass
class FakeAsker:
    """A `GateAsker`'s one verb, scripted, recording what it was shown."""

    verdicts: list[bool]
    asked: list[tuple[str, str]] = field(default_factory=list)

    async def ask(self, gate_id: str, rendered: str) -> bool:
        self.asked.append((gate_id, rendered))
        return self.verdicts.pop(0)


@dataclass
class StepClock:
    """A clock that advances a fixed amount per reading, so coalescing is deterministic."""

    step: float
    read: float = 0.0

    def __call__(self) -> float:
        now = self.read
        self.read += self.step
        return now


def _entry(permission: Literal["auto", "gate"] = "auto", timeout_seconds: float = 30.0) -> ExternalAgentEntry:
    return ExternalAgentEntry(
        command=sys.executable,
        args=(str(ECHO_AGENT),),
        env={"PYTHONUNBUFFERED": "1"},
        permission=permission,
        timeout_seconds=timeout_seconds,
    )


def _registry(entries: Mapping[str, ExternalAgentEntry]) -> ExternalAgentRegistry:
    """The registry seam as an argument: a callable read per call, never a patched module."""

    def read() -> Mapping[str, ExternalAgentEntry]:
        return entries

    return read


class TestTheStep:
    async def test_an_auto_agent_runs_and_the_answer_names_what_it_touched(self, tmp_path: Path) -> None:
        session_log = FakeJournal()
        step = run_external_agent_step(_registry({"echo": _entry()}), session_log=session_log, ask=None)

        said = await step("echo", "do the thing", str(tmp_path))

        assert "echo finished (end_turn)" in said
        assert "notes.txt" in said
        assert "echo do the thing" in said
        assert "wrote it" in said

    async def test_the_run_is_recorded_start_to_finish(self, tmp_path: Path) -> None:
        session_log = FakeJournal()
        step = run_external_agent_step(_registry({"echo": _entry()}), session_log=session_log, ask=None, clock=StepClock(step=5.0))

        await step("echo", "hello", str(tmp_path))

        started = session_log.recorded[0]
        assert isinstance(started, ExternalAgentStarted)
        assert started.agent == "echo"
        assert started.cwd == str(tmp_path.resolve())
        finished = session_log.recorded[-1]
        assert isinstance(finished, ExternalAgentFinished)
        assert finished.stop_reason == "end_turn"
        assert finished.touched_files == ("notes.txt",)

    async def test_updates_are_coalesced_to_one_a_second(self, tmp_path: Path) -> None:
        """The echo agent sends several updates back to back; a clock that never moves lets one through."""
        session_log = FakeJournal()
        step = run_external_agent_step(_registry({"echo": _entry()}), session_log=session_log, ask=None, clock=StepClock(step=0.0))

        await step("echo", "hello", str(tmp_path))

        updates = [event for event in session_log.recorded if isinstance(event, ExternalAgentUpdated)]
        assert len(updates) == 1
        assert updates[0].agent == "echo"
        assert updates[0].text

    async def test_a_gate_agent_asks_the_asker_and_an_approval_lets_the_write_through(self, tmp_path: Path) -> None:
        asker = FakeAsker(verdicts=[True])
        step = run_external_agent_step(_registry({"echo": _entry(permission="gate")}), session_log=FakeJournal(), ask=asker.ask)

        said = await step("echo", "hello", str(tmp_path))

        assert len(asker.asked) == 1
        gate_id, rendered = asker.asked[0]
        assert gate_id == "external:echo:write-1"
        assert "write notes.txt" in rendered
        assert "notes.txt" in said

    async def test_a_gate_agent_refused_leaves_nothing_touched(self, tmp_path: Path) -> None:
        asker = FakeAsker(verdicts=[False])
        step = run_external_agent_step(_registry({"echo": _entry(permission="gate")}), session_log=FakeJournal(), ask=asker.ask)

        said = await step("echo", "hello", str(tmp_path))

        assert "no files" in said
        assert "skipped it" in said

    async def test_a_gate_agent_with_nobody_to_ask_refuses_and_names_the_fix(self, tmp_path: Path) -> None:
        step = run_external_agent_step(_registry({"echo": _entry(permission="gate")}), session_log=FakeJournal(), ask=None)

        said = await step("echo", "hello", str(tmp_path))

        assert "cannot ask" in said
        assert "auto" in said

    async def test_an_unknown_name_lists_what_is_configured(self, tmp_path: Path) -> None:
        step = run_external_agent_step(_registry({"echo": _entry()}), session_log=FakeJournal(), ask=None)

        said = await step("codex", "hello", str(tmp_path))

        assert "no external agent named codex" in said
        assert "echo" in said

    async def test_a_missing_directory_is_refused_before_anything_spawns(self, tmp_path: Path) -> None:
        session_log = FakeJournal()
        step = run_external_agent_step(_registry({"echo": _entry()}), session_log=session_log, ask=None)

        said = await step("echo", "hello", str(tmp_path / "nowhere"))

        assert "not a directory" in said
        assert session_log.recorded == []

    async def test_no_directory_means_this_checkout(self, tmp_path: Path) -> None:
        session_log = FakeJournal()
        step = run_external_agent_step(_registry({"echo": _entry()}), session_log=session_log, ask=None)

        await step("echo", "hello", None)

        started = session_log.recorded[0]
        assert isinstance(started, ExternalAgentStarted)
        assert started.cwd.endswith("harness")

    async def test_a_registry_that_will_not_read_is_said_not_raised(self, tmp_path: Path) -> None:
        def broken() -> Mapping[str, ExternalAgentEntry]:
            msg = "external-agents.json is not a valid external-agent registry"
            raise ValueError(msg)

        step = run_external_agent_step(broken, session_log=FakeJournal(), ask=None)

        said = await step("echo", "hello", str(tmp_path))

        assert "could not be read" in said


class TestTheDoor:
    def test_the_door_mounts_exactly_one_tool(self) -> None:
        plugin = external_agent_plugin(_registry({}))

        assert isinstance(plugin, Toolset)
        toolset = plugin.toolset
        assert isinstance(toolset, FunctionToolset)
        assert list(toolset.tools) == [EXTERNAL_AGENT_TOOL]

    async def test_the_door_journals_and_asks_through_the_session(self, tmp_path: Path) -> None:
        session_log = FakeJournal()
        asker = FakeAsker(verdicts=[True])
        session = Runtime(
            registry=StepRegistry(),
            library=FolderLibrary(tmp_path / "workflows"),
            agent=Agent(TestModel(), deps_type=Runtime),
            active=ActiveRun(),
            session_log=session_log,
            gate_asker=asker,
            state=a_state(tmp_path),
        )
        plugin = external_agent_plugin(_registry({"echo": _entry(permission="gate")}))
        toolset = plugin.toolset
        assert isinstance(toolset, FunctionToolset)
        tool = toolset.tools[EXTERNAL_AGENT_TOOL]

        said = await tool.function(RunContext(deps=session, model=TestModel(), usage=RunUsage()), "echo", "hello", str(tmp_path))

        assert len(asker.asked) == 1
        assert isinstance(session_log.recorded[0], ExternalAgentStarted)
        assert "notes.txt" in said


class TestTheStepInTheRegistry:
    async def test_build_world_registers_it_with_its_real_contract(self, tmp_path: Path) -> None:
        registry = await build_environment(TestModel(), engram=fake_engram(), external_agents=dict, session_log=NoSessionLog(), custom=tmp_path / "custom", records=records_under(tmp_path), web_search=WebSearchSettings(), tiers=tiers_under(tmp_path))

        step = registry.get("run_external_agent")
        assert step is not None
        assert step.input_types == {"agent": str, "prompt": str, "cwd": str | None}
        assert step.output_type is str
        assert step.required == frozenset({"agent", "prompt"})
        assert step.replay_safe is False

    async def test_the_registered_step_runs_an_auto_agent(self, tmp_path: Path) -> None:
        registry = await build_environment(TestModel(), engram=fake_engram(), external_agents=_registry({"echo": _entry()}), session_log=NoSessionLog(), custom=tmp_path / "custom", records=records_under(tmp_path), web_search=WebSearchSettings(), tiers=tiers_under(tmp_path))

        step = registry.get("run_external_agent")
        assert step is not None
        said = await step.function(agent="echo", prompt="hello", cwd=str(tmp_path))

        assert isinstance(said, str)
        assert "echo finished (end_turn)" in said
