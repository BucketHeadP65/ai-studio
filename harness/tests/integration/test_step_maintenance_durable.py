"""One whole step maintenance run on a real Temporal server, under gate mode `auto` as its schedule runs it.

The registered steps run as activities over a fake library, the program runs in the real sandbox, and the planner, the
plan judge, the drafter and the repository judge are scripted. Every value a step hands the next crosses the activity
boundary, so a passing run shows the maintenance types survive the payload converter both ways.

The run never finishes today: the durable engine runs a program that holds no tool in workflow code, where the sandbox's
hand-off to its thread is refused (technical debt TD-84). The test is held as an expected failure on that timeout alone,
so any other failure still fails it, and it fails as an unexpected pass the day the engine runs such a program.
"""

from __future__ import annotations

import asyncio

from typing import TYPE_CHECKING

import pytest

from core.ai.workflows.contracts import ConsumerEntry, DurableWorkflowRun, LiveTrialMarker, RunRequest, StepRegistry
from core.ai.workflows.infrastructure.temporal import bind as bind_registry
from core.ai.workflows.infrastructure.temporal import sandbox_runner, step_activities
from pydantic_monty import AsyncMonty
from temporalio.worker import Worker

from harness.adapters.environment import code_runner
from harness.domain.ports import JUDGE_TOOLS
from harness.features.maintenance.contracts import CHANGED_STEPS, PLAN_GATE, STEP_MAINTENANCE_AGENT, HandOver
from tests.support.grants import app_grants, app_reads
from tests.support.maintenance import (
    CHANGED,
    CHANGED_HASH,
    FEEDBACK,
    MaintenanceWorld,
    ScriptedMaintenanceModels,
    step_maintenance_workflow,
)

if TYPE_CHECKING:
    from core.ai.workflows.contracts import Briefing
    from temporalio.client import Client

pytestmark = pytest.mark.asyncio(loop_scope="module")


async def _registry(world: MaintenanceWorld, models: ScriptedMaintenanceModels, sandbox: AsyncMonty) -> StepRegistry:
    """The app's grants, the real sandbox for the program, the scripted models for every briefing, and the world's steps."""

    async def perform(task: Briefing, output_type: type, /, **inputs: object) -> object:
        answer = models.answer(task.name, output_type, inputs)
        assert answer is not None, f"no model answers {task.name!r} here"
        return answer

    async def call(name: str, args: dict[str, object], /) -> object:
        del args
        msg = f"the program reads nothing, so {name!r} must never be called"
        raise AssertionError(msg)

    grantable = await app_grants()
    registry = StepRegistry(
        perform=perform,
        grants=grantable,
        run_code=code_runner(sandbox, reads=await app_reads(), call=call),
        judge_tools=frozenset(JUDGE_TOOLS) & frozenset(grantable),
    )
    world.steps.register(registry)
    return registry


@pytest.mark.xfail(raises=TimeoutError, strict=True, reason="TD-84: a program that holds no tool never finishes on the durable engine")
async def test_a_durable_run_adopts_one_consumer_on_live_trial_and_keeps_the_other_once_the_judge_approves_the_second_plan(
    temporal_client: Client,
) -> None:
    world = MaintenanceWorld()
    meeting, standup = await world.fill()
    models = ScriptedMaintenanceModels()
    queue = "test-step-maintenance"

    async with AsyncMonty(request_timeout=30.0) as sandbox:
        registry = await _registry(world, models, sandbox)
        bind_registry(queue, registry)
        async with Worker(
            temporal_client, task_queue=queue, workflows=[DurableWorkflowRun], activities=step_activities(registry), workflow_runner=sandbox_runner()
        ):
            handle = await temporal_client.start_workflow(
                DurableWorkflowRun.run,
                RunRequest(spec=step_maintenance_workflow(), params={CHANGED_STEPS: CHANGED}, workflow_id="wf.step-maintenance.v1", gate_mode="auto"),
                id="run-step-maintenance",
                task_queue=queue,
            )
            result = await asyncio.wait_for(handle.result(), timeout=20)

    assert models.performed == ["plan_changes", PLAN_GATE, "plan_changes", PLAN_GATE, "draft_repository_change", "judge_repository_change"]
    assert models.repairs == [[], [(PLAN_GATE, 1, FEEDBACK)]], "the second plan reads the judge's feedback on the first across the boundary"
    handed = HandOver.model_validate(result)
    assert handed.flags == ["mf.change.extract_actions"]
    assert (handed.dropped, handed.problems) == ([], [])
    adopted = "wf.meeting-actions.v2"
    assert f"{adopted} adopts {CHANGED} on live trial in place of {meeting}" in handed.report
    assert await world.library.current(meeting) == adopted
    assert world.store.written(adopted)["saved_by"] == STEP_MAINTENANCE_AGENT
    assert await world.library.fetch_live_trial(adopted) == LiveTrialMarker(previous=meeting, step="extract_actions", step_hash=CHANGED_HASH)
    (kept,) = await world.library.fetch_consumer_record(standup)
    assert isinstance(kept, ConsumerEntry)
    assert (kept.declined, kept.declined_because) == (CHANGED_HASH, "kept")
    assert len(world.notifier.sent) == 2
