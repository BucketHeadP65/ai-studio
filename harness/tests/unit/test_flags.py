"""The friction voice: `flag_friction` records one typed event and hands back a plain line.

The tool itself is proved directly against its toolset; the mount is proved through
`build_agent`, so a future rewrite of the capabilities list is caught the moment it drops the
plugin or forgets the `RuntimeVoiced` wrap; the plugin switch is proved through `Switched` the same
way `test_doors.py` proves every other plugin.
"""

from __future__ import annotations

import uuid

from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any, cast

from core.ai.voice import RuntimeVoiced
from core.ai.workflows.contracts import StepRegistry, WorkflowContract
from core.ai.workflows.infrastructure.folder import FOLDER, folder_toolset
from pydantic_ai.capabilities import AbstractCapability
from pydantic_ai.models.test import TestModel
from pydantic_ai.tools import RunContext
from pydantic_ai.usage import RunUsage

from harness import plugins
from harness.domain.events import FrictionFlagged, Recorded, SessionEvent
from harness.features.authoring.agent import build_agent
from harness.features.authoring.process import WorkflowProcess
from harness.features.running.spend_report import SpendReporter
from harness.features.scheduling.contracts import Scheduler
from harness.features.sessions.flags import FrictionFlagging
from harness.features.sessions.service import SessionProjection
from harness.session import Runtime, SessionPool
from harness.settings import RunSettings, WebSearchSettings
from tests.support.fakes.broadcaster import FakeBroadcaster
from tests.support.fakes.pools import FakePool
from tests.support.fakes.request_parameters import FakeRequestParametersSink
from tests.support.fakes.schedules import FakeSchedules
from tests.support.fakes.sessions import APool
from tests.support.fakes.telemetry import FakeTelemetry
from tests.support.runtime import a_runtime
from tests.support.state import a_state, a_throwaway_state, a_transcript, records_under

if TYPE_CHECKING:
    from pathlib import Path


@dataclass
class _NoResearch(AbstractCapability[Runtime]):
    """Fills `build_agent`'s required research slot; the mount under test is `friction`."""


@dataclass
class FakeJournal:
    """A hand-written `SessionEvents`: captures every event handed to it, in order."""

    recorded: list[SessionEvent] = field(default_factory=list)

    def record(self, event: SessionEvent) -> Recorded | None:
        """Capture `event` and report nothing back -- no caller here reads the return."""
        self.recorded.append(event)
        return None

    def events(self) -> tuple[Recorded, ...]:
        """Nothing is read back off this log."""
        return ()

    def boot_sweep(self) -> None:
        """Nothing to bring up to date: this log holds only what a test put in it."""


def _ctx(session_log: FakeJournal) -> RunContext[Runtime]:
    runtime = Runtime(
        registry=StepRegistry(), library=cast("Any", None), agent=cast("Any", None), session_log=session_log, state=a_throwaway_state()
    )
    return RunContext(deps=runtime, model=TestModel(), usage=RunUsage())


class TestFlagFriction:
    async def test_the_tool_records_one_event_and_confirms_in_one_line(self) -> None:
        capability = FrictionFlagging()
        toolset = capability.get_toolset()
        session_log = FakeJournal()
        ctx = _ctx(session_log)
        tools = await toolset.get_tools(ctx)

        result = await toolset.call_tool(
            "flag_friction",
            {"target_kind": "tool", "target": "shell", "note": "confusing exit codes", "tool_call_id": "call-1"},
            ctx,
            tools["flag_friction"],
        )

        assert result == "flagged tool 'shell'"
        assert [event.kind for event in session_log.recorded] == ["friction_flagged"]
        flagged = session_log.recorded[0]
        assert isinstance(flagged, FrictionFlagged)
        assert flagged.target_kind == "tool"
        assert flagged.target == "shell"
        assert flagged.note == "confusing exit codes"
        assert flagged.tool_call_id == "call-1"
        assert flagged.source == "live"
        assert flagged.subject_session_id is None

    def test_get_instructions_is_a_literal_string(self) -> None:
        instructions = FrictionFlagging().get_instructions()

        assert isinstance(instructions, str)
        assert "flag_friction" in instructions


class TestFrictionMount:
    def test_build_agent_mounts_the_capability_behind_the_friction_door_voiced(self, tmp_path: Path) -> None:
        port = WorkflowContract[Runtime](
            source=folder_toolset(tmp_path / "workflows"), vocabulary=FOLDER, expose=(), registry=StepRegistry(), saved_by="test"
        )
        agent = build_agent(
            TestModel(),
            WorkflowProcess(
                port=port,
                records=records_under(tmp_path),
                reporter=SpendReporter(FakeTelemetry()),
                scheduler=Scheduler(FakeSchedules(), FakeSchedules(), FakeBroadcaster()),
                sessions=APool(SessionPool(a_runtime())),
                topics=FakeBroadcaster(),
                run=RunSettings(),
            ),
            _NoResearch(),
            state=a_state(tmp_path),
            custom=tmp_path / "custom",
            web_search=WebSearchSettings(),
            store=a_transcript(tmp_path),
            parameters=FakeRequestParametersSink(),
            external_agents=dict,
        )

        mounted: list[plugins.Switched[Runtime]] = []

        def _collect(cap: AbstractCapability[Runtime]) -> None:
            if isinstance(cap, plugins.Switched) and cap.plugin == "friction":
                mounted.append(cap)

        agent._root_capability.apply(_collect)

        assert len(mounted) == 1
        wrapped = mounted[0].wrapped
        assert isinstance(wrapped, RuntimeVoiced)
        assert isinstance(wrapped.wrapped, FrictionFlagging)


class TestFlagsProjection:
    async def test_a_projected_row_lists_with_its_source(self) -> None:
        row = {
            "id": uuid.uuid4(),
            "session_id": uuid.uuid4(),
            "target_kind": "workflow",
            "target": "some workflow",
            "note": "ground",
            "tool_call_id": None,
            "source": "live",
            "created_at": datetime.now(UTC),
        }
        projection = SessionProjection(FakePool(rows=[row]))

        flags = await projection.flags(target_kind=None, target=None, session_id=None)

        assert len(flags) == 1
        assert flags[0].source == "live"


class TestFrictionDoor:
    def test_a_closed_friction_door_mounts_nothing(self, tmp_path: Path) -> None:
        switched = plugins.Switched(RuntimeVoiced(FrictionFlagging()), plugin="friction", state=a_state(tmp_path))

        open_capability = switched.capability_func(_ctx(FakeJournal()))
        plugins.set_plugin(a_state(tmp_path), "friction", enabled=False)
        closed_capability = switched.capability_func(_ctx(FakeJournal()))

        assert open_capability is not None
        assert closed_capability is None

    def test_the_friction_door_is_open_by_default(self, tmp_path: Path) -> None:
        assert plugins.plugin_enabled(a_state(tmp_path), "friction")
