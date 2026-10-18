"""A run's own responses reach its ledger from every step, on the in-process engine and inside a Temporal activity."""

from __future__ import annotations

import dataclasses

from typing import TYPE_CHECKING, cast

from core.ai.workflows.contracts import Briefing, FromNode, FromParameter, ParameterSpec, RunNode, Shape, Workflow
from pydantic_ai.models.test import TestModel
from temporalio.testing import ActivityEnvironment

from harness.features.running import service
from harness.session import ActiveRun, NoSessionLog, RunIdentity, Runtime, run_identity
from tests.support.environment import world_over
from tests.support.fakes.broadcaster import FakeBroadcaster
from tests.support.state import a_throwaway_state, records_under

if TYPE_CHECKING:
    from pathlib import Path

    from core.ai.workflows.contracts import StepRegistry, WorkflowLibrary
    from pydantic_ai import Agent
    from pydantic_ai.tools import DeferredToolRequests


def _two_step_spec() -> Workflow:
    """Two briefings in a row, each one model request on the test model."""
    draft = Briefing(
        name="draft",
        instructions="Say one word about the topic you were handed, and nothing else at all.",
        inputs={"topic": Shape(json_schema={"type": "string"})},
        output=Shape(json_schema={"type": "string"}),
        tools=(),
    )
    review = Briefing(
        name="review",
        instructions="Say one more word about the text you were handed, and nothing else at all.",
        inputs={"text": Shape(json_schema={"type": "string"})},
        output=Shape(json_schema={"type": "string"}),
        tools=(),
    )
    return Workflow(
        name="two-briefings",
        version=1,
        description="Two briefings in a row, so a run has two steps to bill.",
        parameters={"topic": ParameterSpec(description="what to say a word about", json_schema={"type": "string"})},
        inline=(draft, review),
        nodes=(
            RunNode(id="draft", work="draft", inputs={"topic": FromParameter(parameter="topic")}),
            RunNode(id="review", work="review", inputs={"text": FromNode(node="draft")}),
        ),
        output=FromNode(node="review"),
    )


def _session(registry: StepRegistry) -> Runtime:
    return Runtime(
        registry=registry,
        library=cast("WorkflowLibrary", None),
        agent=cast("Agent[Runtime, str | DeferredToolRequests]", None),
        session_id="s-1",
        session_log=NoSessionLog(),
        state=a_throwaway_state(),
    )


async def test_an_in_process_run_writes_a_header_and_a_row_per_step_under_the_archived_run_id(tmp_path: Path) -> None:
    records = records_under(tmp_path)
    registry: StepRegistry = await world_over(TestModel(custom_output_text="ok"), state=tmp_path)
    session = _session(registry)
    service.launch(session, records=records, topics=FakeBroadcaster(), spec=_two_step_spec(), workflow_id="wf.two", params={"topic": "x"}, runner="app:schedule:sched-9")
    task = session.active.task
    assert task is not None
    await task
    run_id = session.active.run_id
    assert run_id is not None
    spend = records.spend.read(run_id, "wf.two")
    assert spend is not None
    assert spend.runner == "app:schedule:sched-9"
    assert spend.session_id == "s-1"
    assert set(spend.steps) == {"draft", "review"}
    assert spend.totals.input_tokens > 0
    archived = records.archive.get(run_id)
    assert archived is not None, "the archive files the run under the same id the ledger opened"


def test_run_identity_falls_back_to_the_bound_in_process_run() -> None:
    active = ActiveRun()
    assert run_identity(active) is None
    active.run_id, active.workflow_id = "run-1", "wf.two"
    assert run_identity(active) == RunIdentity(run_id="run-1", workflow_id="wf.two")


def test_run_identity_prefers_the_activitys_own_ids() -> None:
    environment = ActivityEnvironment()
    environment.info = dataclasses.replace(environment.info, workflow_run_id="temporal-run", workflow_id="wf.two")
    bound = ActiveRun()
    bound.run_id, bound.workflow_id = "run-1", "wf.other"

    assert environment.run(run_identity, bound) == RunIdentity(run_id="temporal-run", workflow_id="wf.two")
