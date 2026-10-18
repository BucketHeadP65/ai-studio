"""What a turn leaves in the record beside its messages: the parameters every request was sent with."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from core.ai.workflows.contracts import StepRegistry, WorkflowContract
from core.ai.workflows.infrastructure.folder import FOLDER, folder_toolset
from pydantic_ai.capabilities import AbstractCapability
from pydantic_ai.models.test import TestModel
from pydantic_ai_harness.step_persistence import InMemoryStepStore

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
from tests.support.state import a_throwaway_state, records_under

if TYPE_CHECKING:
    from collections.abc import Mapping

    from pydantic import JsonValue
    from pydantic_ai import Agent
    from pydantic_ai.tools import DeferredToolRequests


@dataclass
class _NoResearch(AbstractCapability[Runtime]):
    """Fills `build_agent`'s required research slot; the mount under test is the recorder."""


def _request_parameters_of(parameters: Mapping[str, JsonValue]) -> dict[str, JsonValue]:
    """The request-parameters object one recorded set carries."""
    recorded = parameters["request_parameters"]
    assert isinstance(recorded, dict)
    return recorded


def _function_tool_names(parameters: Mapping[str, JsonValue]) -> list[str]:
    """The names of the tools one recorded set says the model could call."""
    tools = _request_parameters_of(parameters)["function_tools"]
    assert isinstance(tools, list)
    names: list[str] = []
    for tool in tools:
        assert isinstance(tool, dict)
        names.append(str(tool["name"]))
    return names


def _build(store: InMemoryStepStore, parameters: FakeRequestParametersSink) -> Agent[Runtime, str | DeferredToolRequests]:
    """The studio agent, built the way `tests/unit/test_agent_mounts.py` builds one for a turn."""
    state = a_throwaway_state()
    port = WorkflowContract[Runtime](
        source=folder_toolset(state.home / "workflows"),
        vocabulary=FOLDER,
        expose=(),
        registry=StepRegistry(),
        saved_by="test",
    )
    return build_agent(
        TestModel(call_tools=[]),
        WorkflowProcess(
            port=port,
            records=records_under(state.home),
            reporter=SpendReporter(FakeTelemetry()),
            scheduler=Scheduler(FakeSchedules(), FakeSchedules(), FakeBroadcaster()),
            sessions=APool(SessionPool(a_runtime())),
            topics=FakeBroadcaster(),
            run=RunSettings(),
        ),
        _NoResearch(),
        state=state,
        custom=state.home / "custom",
        web_search=WebSearchSettings(),
        store=store,
        parameters=parameters,
        external_agents=dict,
    )


async def test_a_turn_records_the_parameters_of_every_request() -> None:
    """One model request, one row: keyed by the run and its step, carrying the tools the model saw."""
    sink = FakeRequestParametersSink()
    store = InMemoryStepStore()
    agent = _build(store, sink)
    runtime = a_runtime(agent=agent, store=store)

    result = await agent.run("hello", deps=runtime, conversation_id=runtime.session_id)

    del result
    (run,) = await store.list_runs(conversation_id=runtime.session_id)
    latest = await store.latest_snapshot(run_id=run.run_id)
    assert latest is not None
    assert sink.recorded, "the recorder ran on the model request"
    assert sink.recorded[-1].run_id == run.run_id
    assert sink.recorded[-1].step_index == latest.step_index, "the parameters are keyed by the same step the snapshot is"
    assert "read_pyai_docs" in _function_tool_names(sink.recorded[-1].parameters), (
        "the pydantic-ai-docs plugin's reader is on by default and never folds into code mode"
    )
