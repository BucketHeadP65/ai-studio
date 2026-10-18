"""The chat agent's output type: plain text, with a deferred marker beside it on every plugin.

`output_type=[str, DeferredToolRequests]` is unconditional -- not gated on whether `follow_me` is
mounted. `ConsoleApprovals` wraps every tool on every build, so any plugin can meet a call that must
ask the person first, and an approval reaches the person only through a turn ending deferred.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from core.ai.workflows.contracts import StepRegistry, WorkflowContract
from core.ai.workflows.infrastructure.folder import FOLDER, folder_toolset
from pydantic_ai.capabilities import AbstractCapability
from pydantic_ai.models.test import TestModel
from pydantic_ai.tools import DeferredToolRequests

from harness.features.authoring.agent import build_agent
from harness.features.authoring.process import WorkflowProcess
from harness.features.running.spend_report import SpendReporter
from harness.features.scheduling.contracts import Scheduler
from harness.session import Runtime, SessionPool
from harness.settings import RunSettings, WebSearchSettings
from tests.support.fakes.broadcaster import FakeBroadcaster
from tests.support.fakes.request_parameters import FakeRequestParametersSink
from tests.support.fakes.schedules import FakeSchedules
from tests.support.fakes.sessions import APool
from tests.support.fakes.telemetry import FakeTelemetry
from tests.support.runtime import a_runtime
from tests.support.state import a_state, a_transcript, records_under

if TYPE_CHECKING:
    from pathlib import Path


@dataclass
class _NoResearch(AbstractCapability[Runtime]):
    """Fills `build_agent`'s required research slot; the output mode is what is under test."""


def _port(tmp_path: Path) -> WorkflowContract[Runtime]:
    return WorkflowContract[Runtime](
        source=folder_toolset(tmp_path / "workflows"), vocabulary=FOLDER, expose=(), registry=StepRegistry(), saved_by="test"
    )


def test_the_door_answers_plain_text_with_a_deferred_marker_beside_it(tmp_path: Path) -> None:
    agent = build_agent(
        TestModel(),
        WorkflowProcess(port=_port(tmp_path), records=records_under(tmp_path), reporter=SpendReporter(FakeTelemetry()), scheduler=Scheduler(FakeSchedules(), FakeSchedules(), FakeBroadcaster()), sessions=APool(SessionPool(a_runtime())), topics=FakeBroadcaster(), run=RunSettings()),
        _NoResearch(),
        state=a_state(tmp_path),
        custom=tmp_path / "custom",
        web_search=WebSearchSettings(),
        store=a_transcript(tmp_path),
        parameters=FakeRequestParametersSink(),
        external_agents=dict,
    )

    outputs = agent.output_type
    assert isinstance(outputs, list)
    assert outputs == [str, DeferredToolRequests]


def test_the_marker_stays_even_with_the_follow_me_door_not_mounted(tmp_path: Path) -> None:
    """Unconditional: no `follow_me` argument still carries the same output type as one that has it.

    `_reply_output_mode.py`'s premise before this test was that the SPA plugin and an ACP entry
    answered in two different shapes. There is one shape now, on every build.
    """
    without = build_agent(
        TestModel(),
        WorkflowProcess(port=_port(tmp_path), records=records_under(tmp_path), reporter=SpendReporter(FakeTelemetry()), scheduler=Scheduler(FakeSchedules(), FakeSchedules(), FakeBroadcaster()), sessions=APool(SessionPool(a_runtime())), topics=FakeBroadcaster(), run=RunSettings()),
        _NoResearch(),
        state=a_state(tmp_path),
        custom=tmp_path / "custom",
        web_search=WebSearchSettings(),
        store=a_transcript(tmp_path),
        parameters=FakeRequestParametersSink(),
        external_agents=dict,
    )
    with_follow_me = build_agent(
        TestModel(),
        WorkflowProcess(port=_port(tmp_path), records=records_under(tmp_path), reporter=SpendReporter(FakeTelemetry()), scheduler=Scheduler(FakeSchedules(), FakeSchedules(), FakeBroadcaster()), sessions=APool(SessionPool(a_runtime())), topics=FakeBroadcaster(), run=RunSettings()),
        _NoResearch(),
        state=a_state(tmp_path),
        custom=tmp_path / "custom",
        web_search=WebSearchSettings(),
        store=a_transcript(tmp_path),
        parameters=FakeRequestParametersSink(),
        external_agents=dict,
        follow_me=_NoResearch(),
    )

    assert without.output_type == with_follow_me.output_type == [str, DeferredToolRequests]
