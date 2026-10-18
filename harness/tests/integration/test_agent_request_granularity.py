"""The retry unit, measured: a replay-safe agent step is recorded per model request, not per step.

Every claim here is a claim about a real Temporal dev server. The subject is the *size* of what
a worker death costs. `tests/integration/test_temporal_durability.py` already shows that a
completed step is not re-executed; that is step granularity, and it says nothing about an agent
that died on its sixth model call, because one activity records no interior.

The instrument is two process-global lists that survive the worker being replaced -- the same
technique the step-granularity module uses on `CALLS`. `BODIES` is appended in *workflow* code
and so is itself a replay-visible side effect, which is exactly what `replay_safe` forbids; the
test spends that on purpose, because the double-append is the mechanism it has to observe. A
step shipped this way would be mismarked.
"""

from __future__ import annotations

import asyncio

from dataclasses import dataclass
from typing import TYPE_CHECKING

import pytest

from core.ai.workflows.contracts import (
    DurableWorkflowRun,
    FromNode,
    FromParameter,
    GateVerdict,
    ParameterSpec,
    PersonGate,
    RunNode,
    RunRequest,
    StepRegistry,
    Workflow,
)
from core.ai.workflows.infrastructure.temporal import bind as bind_registry
from core.ai.workflows.infrastructure.temporal import sandbox_runner, step_activities, worker
from pydantic_ai import Agent
from pydantic_ai.durable_exec.temporal import AgentPlugin, TemporalDurability
from pydantic_ai.exceptions import UserError
from pydantic_ai.messages import ModelMessage, ModelResponse, ToolCallPart
from pydantic_ai.models.function import AgentInfo, FunctionModel
from temporalio.worker import Worker

if TYPE_CHECKING:
    from collections.abc import Iterator

    from temporalio.client import Client, WorkflowHandle

pytestmark = pytest.mark.asyncio(loop_scope="module")

REQUESTS: list[str] = []
"""Every model request this process actually served, by turn. Never cleared across workers."""

BODIES: list[str] = []
"""Every time a step's own body ran. Replay re-runs workflow code, so this grows past one."""

HELD_TURNS: list[int] = []
"""Which turns' model requests hang until their worker is shut down. Mutated, never rebound.

A list rather than a module-level `int` the tests reassign, so nothing needs `global`: the model
function runs inside an activity and has no argument path back to the test that armed it."""

_FINAL_TURN = 2
"""The turn the drafter stops calling tools and answers, so a run costs exactly three requests."""


@dataclass(frozen=True)
class Answer:
    text: str


async def _drafter(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
    """Two tool calls, then an answer -- three model requests per agent run, deterministically.

    Offline by construction: a `FunctionModel` instance is registered as the durability
    capability's `'default'` model, so it crosses into the model-request activity as `model_id`
    `None` and is resolved back to this very instance rather than rebuilt from a string.
    """
    del info
    turn = len([message for message in messages if isinstance(message, ModelResponse)])
    REQUESTS.append(f"turn-{turn}")
    if turn in HELD_TURNS:
        await asyncio.sleep(3600)  # cancelled when the worker is shut down, which is the point
    if turn < _FINAL_TURN:
        return ModelResponse(parts=[ToolCallPart(tool_name="look_up", args={"topic": f"t{turn}"})])
    return ModelResponse(parts=[ToolCallPart(tool_name="final_result", args={"text": f"answered on turn {turn}"})])


durable_drafter: Agent[None, Answer] = Agent(
    FunctionModel(_drafter), name="durable_drafter", output_type=Answer, capabilities=[TemporalDurability()]
)
"""The wired agent: its model requests and tool calls are activities of their own."""

bare_drafter: Agent[None, Answer] = Agent(FunctionModel(_drafter), name="bare_drafter", output_type=Answer)
"""The same agent without the capability -- the control the negative test needs."""


@durable_drafter.tool_plain
async def look_up(topic: str) -> str:
    """Look one thing up, so the agent's loop takes more than one model request."""
    return f"facts about {topic}"


bare_drafter._function_toolset.add_function(look_up)  # the same tool, so the two agents differ in one thing only


async def draft_durable(question: str) -> Answer:
    BODIES.append("draft_durable")
    return (await durable_drafter.run(question)).output


async def draft_bare(question: str) -> Answer:
    BODIES.append("draft_bare")
    return (await bare_drafter.run(question)).output


async def publish(answer: Answer) -> str:
    return f"published: {answer.text}"


def _spec(work: str) -> Workflow:
    """Draft, wait at a gate, publish. The gate is what holds the run still while a worker dies."""
    return Workflow(
        name="cited-answer",
        version=1,
        description="An agent drafts, a person approves, the answer is published.",
        parameters={"question": ParameterSpec(description="The question to answer", json_schema={"type": "string"})},
        nodes=(
            RunNode(id="draft", work=work, inputs={"question": FromParameter(parameter="question")}),
            PersonGate(id="approve", reviews=FromNode(node="draft"), rejected_to="draft"),
            RunNode(id="publish", work="publish", inputs={"answer": FromNode(node="approve")}),
        ),
        output=FromNode(node="publish"),
    )


def _registry(work: str) -> StepRegistry:
    registry = StepRegistry()
    registry.register(draft_durable if work == "draft_durable" else draft_bare, name=work, replay_safe=True)
    registry.register(publish)
    return registry


def _worker(client: Client, task_queue: str, work: str, *, agents: tuple[Agent[None, Answer], ...]) -> Worker:
    """A worker on its own queue, so tests cannot pick up each other's runs."""
    registry = _registry(work)
    bind_registry(task_queue, registry)
    return Worker(
        client,
        task_queue=task_queue,
        workflows=[DurableWorkflowRun],
        activities=step_activities(registry),
        workflow_runner=sandbox_runner(),
        plugins=[AgentPlugin(agent) for agent in agents],
    )


@pytest.fixture(autouse=True)
def _fresh_counters() -> Iterator[None]:
    for counter in (REQUESTS, BODIES, HELD_TURNS):
        counter.clear()
    yield
    for counter in (REQUESTS, BODIES, HELD_TURNS):
        counter.clear()


async def _wait_until_waiting(handle: WorkflowHandle[DurableWorkflowRun, object], gate_id: str) -> None:
    for _ in range(400):
        waiting = await handle.query(DurableWorkflowRun.waiting_at)
        if waiting is not None and waiting.gate_id == gate_id:
            return
        await asyncio.sleep(0.05)
    msg = f"run never waiting at {gate_id!r}"
    raise AssertionError(msg)


async def _wait_for_requests(count: int) -> None:
    for _ in range(400):
        if len(REQUESTS) >= count:
            return
        await asyncio.sleep(0.05)
    msg = f"only {len(REQUESTS)} model requests arrived, wanted {count}"
    raise AssertionError(msg)


async def _start(client: Client, task_queue: str, work: str, run_id: str) -> WorkflowHandle[DurableWorkflowRun, object]:
    return await client.start_workflow(
        DurableWorkflowRun.run,
        RunRequest(spec=_spec(work), params={"question": "why?"}, workflow_id="cited-answer"),
        id=run_id,
        task_queue=task_queue,
    )


async def test_the_steps_body_reruns_but_its_completed_model_calls_do_not(temporal_client: Client) -> None:
    """The granularity claim: replay re-runs the step, and the model requests inside it are free.

    A replay-safe step runs in workflow code, so a second worker rebuilding the run from history
    executes its body again -- `BODIES` proves it did. What it does *not* do is call the model
    again: all three requests are activities already recorded in that history, so the whole agent
    run replays from the session log. Under whole-step granularity this measurement cannot happen at
    all, because the body would never run a second time to be measured.
    """
    queue = "test-agent-granularity"
    async with _worker(temporal_client, queue, "draft_durable", agents=(durable_drafter,)):
        handle = await _start(temporal_client, queue, "draft_durable", "run-agent-granularity")
        await _wait_until_waiting(handle, "approve")

    assert REQUESTS == ["turn-0", "turn-1", "turn-2"], "one agent run costs three model requests"
    assert BODIES == ["draft_durable"]

    async with _worker(temporal_client, queue, "draft_durable", agents=(durable_drafter,)):
        await handle.signal(DurableWorkflowRun.decide, GateVerdict(gate_id="approve", approved=True))
        result = await handle.result()

    assert result == "published: answered on turn 2"
    assert BODIES == ["draft_durable", "draft_durable"], "workflow code replays, so the step's body ran on the new worker too"
    assert REQUESTS == ["turn-0", "turn-1", "turn-2"], "and not one model request was paid for twice"


async def test_only_the_interrupted_model_call_is_repeated(temporal_client: Client) -> None:
    """Killed *partway through the agent's own loop*, the run resumes at the request it died on.

    The second model request hangs until its worker is shut down, which cancels it mid-flight.
    A new worker replays: request one is read from history, the cancelled request two is retried
    -- that one repetition is the true cost of the crash -- and request three runs for the first
    time. Step granularity would have charged for all three.
    """
    HELD_TURNS.append(1)
    queue = "test-agent-midloop"
    async with _worker(temporal_client, queue, "draft_durable", agents=(durable_drafter,)):
        handle = await _start(temporal_client, queue, "draft_durable", "run-agent-midloop")
        await _wait_for_requests(2)
        assert REQUESTS == ["turn-0", "turn-1"], "the run died with its second model call in flight"

    HELD_TURNS.clear()
    async with _worker(temporal_client, queue, "draft_durable", agents=(durable_drafter,)):
        await _wait_until_waiting(handle, "approve")
        await handle.signal(DurableWorkflowRun.decide, GateVerdict(gate_id="approve", approved=True))
        result = await handle.result()

    assert result == "published: answered on turn 2"
    assert REQUESTS == ["turn-0", "turn-1", "turn-1", "turn-2"], "only the request that never finished was paid for twice"
    assert REQUESTS.count("turn-0") == 1, "the completed first call is read back from history, not re-issued"


async def test_the_marker_alone_buys_nothing_without_the_durability_capability(temporal_client: Client) -> None:
    """The failure the marker cannot prevent: a replay-safe step whose agent records nothing.

    Identical to the first test but for a `bare_drafter` with no `TemporalDurability`. Its model
    requests are plain calls inside workflow code, so replay re-issues every one of them: six
    requests for three calls' worth of work -- strictly worse than leaving the step unmarked.
    The marker says "re-running me is free"; only the capability makes that true of an agent.
    """
    queue = "test-agent-bare"
    async with _worker(temporal_client, queue, "draft_bare", agents=()):
        handle = await _start(temporal_client, queue, "draft_bare", "run-agent-bare")
        await _wait_until_waiting(handle, "approve")

    assert REQUESTS == ["turn-0", "turn-1", "turn-2"]

    async with _worker(temporal_client, queue, "draft_bare", agents=()):
        await handle.signal(DurableWorkflowRun.decide, GateVerdict(gate_id="approve", approved=True))
        await handle.result()

    assert BODIES == ["draft_bare", "draft_bare"]
    assert REQUESTS == ["turn-0", "turn-1", "turn-2", "turn-0", "turn-1", "turn-2"], "an unrecorded agent pays for its whole loop again"


async def test_a_worker_refuses_an_agent_that_journals_nothing(temporal_client: Client) -> None:
    """What the framework *can* check, it checks before a single run: at worker construction.

    Handing `worker()` an agent without `TemporalDurability` is the misconfiguration the test
    above measures, and it is refused here rather than discovered as a doubled bill later.
    Nothing can check the other half -- an agent the step runs but never lists in `agents=` --
    which is why the flag is documented as an assertion.
    """
    with pytest.raises(UserError, match="TemporalDurability"):
        worker(temporal_client, _registry("draft_bare"), task_queue="test-agent-refused", agents=(bare_drafter,))
