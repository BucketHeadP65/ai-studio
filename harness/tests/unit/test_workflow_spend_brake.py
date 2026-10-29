"""The money brake on a workflow step's agent: the same seam the chat agent's own workers run on.

A step whose model keeps calling tools past what its run may spend must stop rather than run to
completion, the way a runaway chat delegation already does -- `SpendLimits` reaches a step's agent
through the `spend=` parameter `build_environment` and `build_agent` both take from the composition root.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from decimal import Decimal
from typing import TYPE_CHECKING

import pytest

from core.ai.workflows.contracts import StepRegistry, WorkflowContract, prepare
from core.ai.workflows.infrastructure.folder import FOLDER, folder_toolset
from pydantic import BaseModel
from pydantic_ai.capabilities import AbstractCapability
from pydantic_ai.messages import ModelResponse, ToolCallPart
from pydantic_ai.models.test import TestModel
from pydantic_ai_harness.spend import BatchSpendStore, Budget, SpendEntry, SpendLimitExceeded, SpendLimits

from harness.composition import _default_spend_limits
from harness.features.authoring.agent import build_agent
from harness.features.authoring.process import WorkflowProcess
from harness.features.running.spend_report import SpendReporter
from harness.features.scheduling.contracts import Scheduler
from harness.session import Runtime, SessionPool
from harness.settings import RunSettings, WebSearchSettings
from tests.support.environment import defined_step, spec_for, world_over
from tests.support.fakes.broadcaster import FakeBroadcaster
from tests.support.fakes.request_parameters import FakeRequestParametersSink
from tests.support.fakes.schedules import FakeSchedules
from tests.support.fakes.sessions import APool
from tests.support.fakes.telemetry import FakeTelemetry
from tests.support.runtime import a_runtime
from tests.support.spend import free
from tests.support.state import a_state, a_transcript, records_under
from tests.support.streaming import streaming_function_model

if TYPE_CHECKING:
    from pathlib import Path

    from pydantic_ai.messages import ModelMessage
    from pydantic_ai.models.function import AgentInfo


@dataclass
class _NoResearch(AbstractCapability[Runtime]):
    """Fills `build_agent`'s required research slot; the spend mount is what is under test."""


def _port(tmp_path: Path) -> WorkflowContract[Runtime]:
    return WorkflowContract[Runtime](
        source=folder_toolset(tmp_path / "workflows"), vocabulary=FOLDER, expose=(), registry=StepRegistry(), saved_by="test"
    )


def _never_finishes(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
    """A step that keeps running code forever -- the runaway this brake exists to stop."""
    del info
    calls = sum(1 for message in messages for part in message.parts if isinstance(part, ToolCallPart))
    return ModelResponse(parts=[ToolCallPart("run_code", {"code": f"rows = await run_sql(sql='select {calls}')\nrows"})])


def _finishes_at_once(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
    """A step that answers on its first request."""
    del messages
    return ModelResponse(parts=[ToolCallPart(info.output_tools[0].name, {"note": "done"})])


class TestTheRunBudget:
    async def test_a_step_over_its_run_budget_stops_instead_of_running_to_completion(self) -> None:
        spend = SpendLimits[object](budgets=[Budget(usd=Decimal(2), window="run")], price=lambda response: Decimal(1))
        environment = await world_over(streaming_function_model(_never_finishes), spend=spend)
        ready = prepare(spec_for(defined_step(tools=("run_sql",))), environment)
        assert ready.report.ok, ready.report.issues
        implementation = ready.registry.get("dig")
        assert implementation is not None

        with pytest.raises(SpendLimitExceeded):
            await implementation.function(question="a loop that never returns on its own")

    async def test_a_step_under_its_run_budget_is_unaffected(self) -> None:
        spend = SpendLimits[object](budgets=[Budget(usd=Decimal(2), window="run")], price=lambda response: Decimal("0.5"))
        environment = await world_over(streaming_function_model(_finishes_at_once), spend=spend)
        ready = prepare(spec_for(defined_step(tools=())), environment)
        assert ready.report.ok, ready.report.issues
        implementation = ready.registry.get("dig")
        assert implementation is not None

        result = await implementation.function(question="a step that answers at once")

        assert isinstance(result, BaseModel)
        assert result.model_dump() == {"note": "done"}


class TestTheSharedDayBudget:
    async def test_the_same_instance_reaches_both_a_workflow_step_and_the_chat_agent(self, tmp_path: Path) -> None:
        spend = SpendLimits[object](budgets=[Budget(usd=Decimal(2), window="day")], price=lambda response: Decimal("0.5"))

        agent = build_agent(
            TestModel(),
            WorkflowProcess(
                port=_port(tmp_path),
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
            spend=spend,
        )
        mounted: list[SpendLimits[Runtime]] = []
        agent._root_capability.apply(lambda cap: mounted.append(cap) if isinstance(cap, SpendLimits) else None)
        assert mounted == [spend], "the chat agent must mount the exact instance it was given, not a look-alike"

        environment = await world_over(streaming_function_model(_finishes_at_once), spend=spend)
        ready = prepare(spec_for(defined_step(tools=())), environment)
        implementation = ready.registry.get("dig")
        assert implementation is not None

        await implementation.function(question="a step that spends against the shared day ceiling")

        statuses = await spend.status()
        day = next(status for status in statuses if status.budget.window == "day")
        assert day.spent.usd == Decimal("0.5"), "the workflow step's spend must land on the instance the chat agent mounts"


class TestTheDayCeilingSurvivesARestart:
    async def test_a_restarted_process_counts_the_request_a_step_made_before_it(self, tmp_path: Path) -> None:
        state = a_state(tmp_path)
        # The deployment's brake, its store and budgets as they are, pricing the test model at nothing.
        environment = await world_over(streaming_function_model(_finishes_at_once), spend=replace(_default_spend_limits(state), price=free))
        ready = prepare(spec_for(defined_step(tools=())), environment)
        implementation = ready.registry.get("dig")
        assert implementation is not None
        await implementation.function(question="a step that spends before the process restarts")

        restarted = _default_spend_limits(state)

        day = next(status for status in await restarted.status() if status.budget.window == "day")
        assert day.spent.requests == 1, "the brake a restarted process builds must read the day the last one counted"

    async def test_a_restarted_process_refuses_a_step_once_the_day_is_spent(self, tmp_path: Path) -> None:
        state = a_state(tmp_path)
        before = _default_spend_limits(state)
        day = next(status for status in await before.status() if status.budget.window == "day")
        assert day.budget.usd is not None
        store = before.store
        assert isinstance(store, BatchSpendStore)
        await store.add_many([SpendEntry(key=day.key, usd=day.budget.usd, requests=1)])

        environment = await world_over(streaming_function_model(_finishes_at_once), spend=_default_spend_limits(state))
        ready = prepare(spec_for(defined_step(tools=())), environment)
        implementation = ready.registry.get("dig")
        assert implementation is not None

        with pytest.raises(SpendLimitExceeded):
            await implementation.function(question="a step after a restart on a day already spent")
