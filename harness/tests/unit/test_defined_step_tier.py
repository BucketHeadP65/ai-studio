"""A defined step runs on the tier its node names, else its own tier, else the worker tier, read fresh.

`build_environment` is handed a `TierModel("worker", tiers)` for the worker tier, and builds the orchestrator and
helper tiers itself from the same tier resolution, so a step's agent answers on whatever tier it resolves to holds
at the moment of the request. Proven with a build seam that labels every model it is asked for, never the
real gateway.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from core.ai.workflows.contracts import Briefing, FromNode, FromParameter, ParameterSpec, RunNode, Shape, Workflow, prepare, run_workflow
from pydantic_ai.messages import ModelMessage, ModelResponse, ToolCallPart

from harness.adapters import model_tiers
from harness.adapters.model_list import ModelList
from harness.adapters.model_tiers import ModelTiers, TierModel, TierSetting
from tests.conftest import ApproveAll
from tests.support.environment import defined_step, spec_for, world_over
from tests.support.fakes.model_listing import FakeModelListing
from tests.support.state import a_state
from tests.support.streaming import streaming_function_model

if TYPE_CHECKING:
    from pathlib import Path

    from core.ai.workflows.contracts import StepRegistry, Tier
    from core.thinking import Thinking
    from pydantic_ai.models import Model
    from pydantic_ai.models.function import AgentInfo, FunctionModel


@pytest.fixture
def tiers(tmp_path: Path) -> ModelTiers:
    """Tier resolution under this test's own home, its list the pinned catalog alone, its models labelled."""
    return ModelTiers(state=a_state(tmp_path), models=ModelList(listing=FakeModelListing()), build=_Factory())


def _answering(label: str) -> FunctionModel:
    def respond(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        del messages
        return ModelResponse(parts=[ToolCallPart(info.output_tools[0].name, {"note": f"answered by {label}"})])

    return streaming_function_model(respond)


class _Factory:
    """A `build` seam that records what it was asked for and answers a labelled model."""

    def __init__(self) -> None:
        self.asked: list[tuple[str, Thinking]] = []

    def __call__(self, model_id: str, *, thinking: Thinking) -> Model:
        self.asked.append((model_id, thinking))
        return _answering(f"{model_id}@{thinking}")


async def _run_step(registry: StepRegistry) -> object:
    ready = prepare(spec_for(defined_step(tools=())), registry)
    assert ready.report.ok, ready.report.issues
    implementation = ready.registry.get("dig")
    assert implementation is not None
    return await implementation.function(question="what is this base's purpose")


def _tier_spec(*, node_tier: Tier | None, task_tier: Tier | None) -> Workflow:
    """One node and its step, so the tier each one names is the only thing under test."""
    task = Briefing(
        name="dig",
        instructions="Find what the knowledge base says about the question and report it in a single note.",
        inputs={"question": Shape(json_schema={"type": "string"})},
        output=Shape(json_schema={"type": "object", "properties": {"note": {"type": "string"}}, "required": ["note"]}),
        tier=task_tier,
    )
    return Workflow(
        name="dig-once",
        version=1,
        description="One invented step, so the tier it resolves to is the tier under test.",
        parameters={"question": ParameterSpec(description="what to look into", json_schema={"type": "string"})},
        inline=(task,),
        nodes=(RunNode(id="dig", work="dig", inputs={"question": FromParameter(parameter="question")}, tier=node_tier),),
        output=FromNode(node="dig"),
    )


async def test_nothing_set_leaves_the_worker_tiers_default_model_answering(tiers: ModelTiers, tmp_path: Path) -> None:
    environment = await world_over(TierModel("worker", tiers), state=tmp_path, build=tiers.build, models=tiers.models)

    result = await _run_step(environment)

    assert "answered by glm-5.3-flash@xhigh" in str(result)


async def test_a_set_worker_tier_reaches_the_very_next_defined_step(tiers: ModelTiers, tmp_path: Path) -> None:
    environment = await world_over(TierModel("worker", tiers), state=tmp_path, build=tiers.build, models=tiers.models)

    model_tiers.set_tier(tiers, "worker", TierSetting(model="gpt-5.4", thinking="low"))
    result = await _run_step(environment)

    assert "answered by gpt-5.4@low" in str(result)


async def test_a_node_naming_helper_runs_its_step_on_the_helper_tier(tiers: ModelTiers, tmp_path: Path) -> None:
    environment = await world_over(TierModel("worker", tiers), state=tmp_path, build=tiers.build, models=tiers.models)
    model_tiers.set_tier(tiers, "helper", TierSetting(model="deepseek-v4-flash-0731", thinking="low"))

    result = await run_workflow(
        _tier_spec(node_tier="helper", task_tier=None), environment, params={"question": "what is this base's purpose"}, approval=ApproveAll()
    )

    assert "answered by deepseek-v4-flash-0731@low" in str(result)


async def test_a_node_naming_no_tier_runs_its_step_on_worker(tiers: ModelTiers, tmp_path: Path) -> None:
    environment = await world_over(TierModel("worker", tiers), state=tmp_path, build=tiers.build, models=tiers.models)

    result = await run_workflow(
        _tier_spec(node_tier=None, task_tier=None), environment, params={"question": "what is this base's purpose"}, approval=ApproveAll()
    )

    assert "answered by glm-5.3-flash@xhigh" in str(result)


async def test_a_step_naming_orchestrator_under_a_node_naming_none_runs_on_orchestrator(tiers: ModelTiers, tmp_path: Path) -> None:
    environment = await world_over(TierModel("worker", tiers), state=tmp_path, build=tiers.build, models=tiers.models)
    model_tiers.set_tier(tiers, "orchestrator", TierSetting(model="gpt-5.4", thinking="medium"))

    result = await run_workflow(
        _tier_spec(node_tier=None, task_tier="orchestrator"),
        environment,
        params={"question": "what is this base's purpose"},
        approval=ApproveAll(),
    )

    assert "answered by gpt-5.4@medium" in str(result)
