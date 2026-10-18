"""Durability, observed rather than asserted: a real server, real history, a real worker death.

Every claim this module makes is a claim about something that happened on a Temporal dev
server. The one that matters is `test_completed_work_survives_the_worker_dying`: the waiting
run's worker is shut down, a new one picks the run up, and the steps that already ran are
not run again -- which is the property the in-process session log cannot offer, because a
suspended coroutine dies with its process.
"""

from __future__ import annotations

import asyncio
import socket

from contextlib import AsyncExitStack
from dataclasses import dataclass
from typing import TYPE_CHECKING

import pytest

from core.ai.workflows.contracts import (
    Briefing,
    DurableWorkflowRun,
    FromNode,
    FromParameter,
    GateVerdict,
    ParameterSpec,
    PersonGate,
    RunNode,
    RunRequest,
    Shape,
    StepRegistry,
    Workflow,
    connect,
)
from core.ai.workflows.domain.vocabulary import SameAs
from core.ai.workflows.infrastructure.temporal import bind as bind_registry
from core.ai.workflows.infrastructure.temporal import sandbox_runner, step_activities
from pydantic import TypeAdapter
from temporalio.client import WorkflowExecutionStatus, WorkflowFailureError
from temporalio.exceptions import ActivityError, ApplicationError, RetryState
from temporalio.worker import Worker

from harness.adapters.temporal_server import ensure_server
from harness.settings import RunSettings

if TYPE_CHECKING:
    from collections.abc import Iterator
    from pathlib import Path

    from temporalio.client import Client, WorkflowHandle

pytestmark = pytest.mark.asyncio(loop_scope="module")

CALLS: list[str] = []
"""Which steps this process actually executed. The re-execution assertion reads it."""

BRIEFED: list[str] = []
"""What a brought step was handed, as `<step name> received <type name>`."""


@dataclass(frozen=True)
class SalesData:
    quarter: str
    total: int


@dataclass(frozen=True)
class ReportDraft:
    text: str


@dataclass(frozen=True)
class ReportUrl:
    url: str


async def fetch_sales(quarter: str) -> SalesData:
    CALLS.append("fetch_sales")
    return SalesData(quarter=quarter, total=1000)


async def draft_report(sales: SalesData) -> ReportDraft:
    CALLS.append("draft_report")
    return ReportDraft(text=f"net {sales.total}")


async def publish(report: ReportDraft) -> ReportUrl:
    CALLS.append("publish")
    return ReportUrl(url=f"https://reports.example/{len(report.text)}")


async def perform_briefing(task: Briefing, output_type: type, /, **inputs: object) -> object:
    """This registry's answer to a briefing: record what arrived, return a fixed value of the declared shape.

    A fixed value rather than a model call, so what the test proves is the wiring of a brought
    step across the activity boundary and nothing about a model.
    """
    sales = inputs["sales"]
    BRIEFED.append(f"{task.name} received {type(sales).__name__}")
    total = sales.total if isinstance(sales, SalesData) else -1
    return TypeAdapter(output_type).validate_python({"text": f"net {total}"})


def _registry() -> StepRegistry:
    registry = StepRegistry(perform=perform_briefing)
    for fn in (fetch_sales, draft_report, publish):
        registry.register(fn)
    return registry


QUARTERLY = Workflow(
    name="quarterly-report",
    version=1,
    description="Fetch sales, draft, human-approve, publish.",
    parameters={"quarter": ParameterSpec(description="Fiscal quarter", json_schema={"type": "string"})},
    nodes=(
        RunNode(id="fetch_sales", work="fetch_sales", inputs={"quarter": FromParameter(parameter="quarter")}),
        RunNode(id="draft_report", work="draft_report", inputs={"sales": FromNode(node="fetch_sales")}),
        PersonGate(id="approve", reviews=FromNode(node="draft_report"), rejected_to="draft_report"),
        RunNode(id="publish", work="publish", inputs={"report": FromNode(node="approve")}),
    ),
    output=FromNode(node="publish"),
)


SUMMARISE = Briefing(
    name="summarise_sales",
    instructions="Read the quarter's sales and write one sentence about them a board member could read aloud.",
    inputs={"sales": SameAs(like="fetch_sales")},
    output=Shape(json_schema={"type": "object", "properties": {"text": {"type": "string"}}, "required": ["text"]}),
)

BROUGHT = Workflow(
    name="quarterly-summary",
    version=1,
    description="Fetch sales, then summarise them with a step this artifact brought with it.",
    parameters={"quarter": ParameterSpec(description="Fiscal quarter", json_schema={"type": "string"})},
    inline=(SUMMARISE,),
    nodes=(
        RunNode(id="fetch_sales", work="fetch_sales", inputs={"quarter": FromParameter(parameter="quarter")}),
        RunNode(id="summarise_sales", work="summarise_sales", inputs={"sales": FromNode(node="fetch_sales")}),
    ),
    output=FromNode(node="summarise_sales"),
)


@pytest.fixture(autouse=True)
def _fresh_calls() -> Iterator[None]:
    CALLS.clear()
    BRIEFED.clear()
    yield
    CALLS.clear()
    BRIEFED.clear()


def _worker(client: Client, task_queue: str) -> Worker:
    """A worker on its own queue, so tests cannot pick up each other's runs."""
    registry = _registry()
    bind_registry(task_queue, registry)
    return Worker(
        client,
        task_queue=task_queue,
        workflows=[DurableWorkflowRun],
        activities=step_activities(registry),
        workflow_runner=sandbox_runner(),
    )


async def _wait_until_waiting(handle: WorkflowHandle[DurableWorkflowRun, object], gate_id: str) -> None:
    for _ in range(200):
        waiting = await handle.query(DurableWorkflowRun.waiting_at)
        if waiting is not None and waiting.gate_id == gate_id:
            return
        await asyncio.sleep(0.05)
    msg = f"run never waiting at {gate_id!r}"
    raise AssertionError(msg)


async def test_a_run_completes_through_activities(temporal_client: Client) -> None:
    """The spec crosses as data, every step runs as an activity, and step types survive the boundary.

    `publish` produced a url ending in 8, which is `len("net 1000")` -- so `draft_report`'s
    `ReportDraft` reached it as a `ReportDraft` and not as the dict it was serialised to. That is
    the payload converter doing its job across two activity boundaries.

    The workflow's own result is JSON-shaped rather than a `ReportUrl`: a graph's output type is
    `object`, so Temporal has nothing to decode into unless the caller names a type.
    """
    queue = "test-completes"
    async with _worker(temporal_client, queue):
        handle = await temporal_client.start_workflow(
            DurableWorkflowRun.run,
            RunRequest(spec=QUARTERLY, params={"quarter": "Q2"}, workflow_id="quarterly-report"),
            id="run-completes",
            task_queue=queue,
        )
        await _wait_until_waiting(handle, "approve")
        await handle.signal(DurableWorkflowRun.decide, GateVerdict(gate_id="approve", approved=True))
        result = await handle.result()

    assert result == {"url": "https://reports.example/8"}
    assert CALLS == ["fetch_sales", "draft_report", "publish"]


async def test_a_run_completes_a_step_its_own_spec_brought(temporal_client: Client) -> None:
    """A brought step is not in the worker's activity table, and a durable run must execute it anyway.

    The worker's table is fixed at construction from the base registry, and `summarise_sales` only
    exists on the registry the spec extends, built inside the workflow. So the run reaches it
    through the one activity that rebuilds that extension from the spec it is handed.

    Two claims: the run completes and returns the brought step's own output, and the step was
    handed a real `SalesData` rather than the dict it was serialised to -- which is the brought
    step's `like` type surviving both directions of the boundary.
    """
    queue = "test-brought"
    async with _worker(temporal_client, queue):
        handle = await temporal_client.start_workflow(
            DurableWorkflowRun.run,
            RunRequest(spec=BROUGHT, params={"quarter": "Q2"}, workflow_id="quarterly-summary"),
            id="run-brought",
            task_queue=queue,
        )
        result = await handle.result()

    assert result == {"text": "net 1000"}
    assert CALLS == ["fetch_sales"], "the registered step still runs as its own activity"
    assert BRIEFED == ["summarise_sales received SalesData"]


async def test_an_activity_the_worker_never_registered_fails_the_run(temporal_client: Client) -> None:
    """A permanent failure ends the run as failed, where Temporal's own default would retry it forever.

    The workflow believes `fetch_sales` is published, but the worker's activity table leaves it out,
    so every delivery is refused as unregistered. The run must fail on the first refusal and say
    which activity it was, instead of sitting at RUNNING with nothing to report.
    """
    queue = "test-unregistered"
    registry = _registry()
    bind_registry(queue, registry)
    activities = [a for a in step_activities(registry) if getattr(a, "__temporal_activity_definition").name != "fetch_sales"]
    async with Worker(
        temporal_client, task_queue=queue, workflows=[DurableWorkflowRun], activities=activities, workflow_runner=sandbox_runner()
    ):
        handle = await temporal_client.start_workflow(
            DurableWorkflowRun.run,
            RunRequest(spec=QUARTERLY, params={"quarter": "Q2"}, workflow_id="quarterly-report"),
            id="run-unregistered",
            task_queue=queue,
        )
        with pytest.raises(WorkflowFailureError) as failure:
            await asyncio.wait_for(handle.result(), timeout=30)
        described = await handle.describe()

    assert described.status == WorkflowExecutionStatus.FAILED
    activity_failure = failure.value.cause
    assert isinstance(activity_failure, ActivityError)
    assert activity_failure.activity_type == "fetch_sales"
    assert activity_failure.retry_state == RetryState.NON_RETRYABLE_FAILURE, "five refusals would read MAXIMUM_ATTEMPTS_REACHED"
    assert isinstance(activity_failure.cause, ApplicationError)
    assert activity_failure.cause.type == "NotFoundError"
    assert CALLS == [], "nothing downstream of the failed step ran"


async def test_a_gate_parks_until_a_signal_arrives(temporal_client: Client) -> None:
    """Nothing downstream of a gate runs while it waits, and the query says what it waits on."""
    queue = "test-waits"
    async with _worker(temporal_client, queue):
        handle = await temporal_client.start_workflow(
            DurableWorkflowRun.run,
            RunRequest(spec=QUARTERLY, params={"quarter": "Q3"}, workflow_id="quarterly-report"),
            id="run-waits",
            task_queue=queue,
        )
        await _wait_until_waiting(handle, "approve")
        waiting = await handle.query(DurableWorkflowRun.waiting_at)
        assert waiting is not None
        assert waiting.gate_id == "approve"
        assert waiting.rejection_reruns == "draft_report"
        assert waiting.reviewing == {"text": "net 1000"}, "the gate publishes what it is asking about"
        assert "publish" not in CALLS, "a waiting gate must hold everything downstream of it"

        await asyncio.sleep(0.3)
        assert "publish" not in CALLS, "waiting is not a timeout: nothing proceeds without a verdict"

        await handle.signal(DurableWorkflowRun.decide, GateVerdict(gate_id="approve", approved=True))
        await handle.result()
        # Queried inside the worker's lifetime: a query is served by a worker replaying the run,
        # so asking after the last one has gone is answered with "no poller", not with state.
        assert await handle.query(DurableWorkflowRun.waiting_at) is None
    assert "publish" in CALLS


async def test_a_rejection_rewinds_and_asks_again(temporal_client: Client) -> None:
    """A rejected gate re-runs its target and waits again, without repaying completed upstream work."""
    queue = "test-rejects"
    async with _worker(temporal_client, queue):
        handle = await temporal_client.start_workflow(
            DurableWorkflowRun.run,
            RunRequest(spec=QUARTERLY, params={"quarter": "Q4"}, workflow_id="quarterly-report"),
            id="run-rejects",
            task_queue=queue,
        )
        await _wait_until_waiting(handle, "approve")
        await handle.signal(DurableWorkflowRun.decide, GateVerdict(gate_id="approve", approved=False))
        # The rejection re-runs `draft_report`, so the gate must ask a second time.
        for _ in range(200):
            if CALLS.count("draft_report") == 2:
                break
            await asyncio.sleep(0.05)
        await _wait_until_waiting(handle, "approve")
        await handle.signal(DurableWorkflowRun.decide, GateVerdict(gate_id="approve", approved=True))
        await handle.result()

    assert CALLS.count("draft_report") == 2, "rejection must re-run the draft"
    assert CALLS.count("fetch_sales") == 1, "the rewind must not repay completed upstream work"
    assert CALLS.count("publish") == 1


async def test_completed_work_survives_the_worker_dying(temporal_client: Client) -> None:
    """The durability claim itself: a worker dies mid-run and its completed steps do not re-run.

    The run waits at the gate, its worker is shut down, and a second worker picks the run up.
    Temporal rebuilds the workflow by replaying its history from the start -- a fresh
    `DurableWorkflowRun` instance, the graph indexed again, every step walked again -- so a
    step whose result were not already in that history would execute a second time. `CALLS`
    is process-global and never cleared between the two workers, so a re-execution would be
    visible here. It is not.
    """
    queue = "test-restart"
    async with _worker(temporal_client, queue):
        handle = await temporal_client.start_workflow(
            DurableWorkflowRun.run,
            RunRequest(spec=QUARTERLY, params={"quarter": "Q1"}, workflow_id="quarterly-report"),
            id="run-restart",
            task_queue=queue,
        )
        await _wait_until_waiting(handle, "approve")

    assert CALLS == ["fetch_sales", "draft_report"], "two steps ran before the worker went down"

    # No worker is polling the queue: the run is waiting in the server's history, not in a process.
    await asyncio.sleep(0.2)
    assert CALLS == ["fetch_sales", "draft_report"]

    async with _worker(temporal_client, queue):
        await handle.signal(DurableWorkflowRun.decide, GateVerdict(gate_id="approve", approved=True))
        result = await handle.result()

    assert result == {"url": "https://reports.example/8"}
    assert CALLS == ["fetch_sales", "draft_report", "publish"], "replay must read completed activities from history, not repeat them"


async def test_progress_is_readable_off_the_workflow(temporal_client: Client) -> None:
    """A watcher reads a live run's transitions from the workflow, with no store of its own."""
    queue = "test-progress"
    async with _worker(temporal_client, queue):
        handle = await temporal_client.start_workflow(
            DurableWorkflowRun.run,
            RunRequest(spec=QUARTERLY, params={"quarter": "Q2"}, workflow_id="quarterly-report"),
            id="run-progress",
            task_queue=queue,
        )
        await _wait_until_waiting(handle, "approve")
        waiting_progress = await handle.query(DurableWorkflowRun.progress)
        assert waiting_progress.running == ["approve"], "the gate is running for exactly as long as the person takes"
        assert waiting_progress.done == ["fetch_sales", "draft_report"]

        await handle.signal(DurableWorkflowRun.decide, GateVerdict(gate_id="approve", approved=True))
        await handle.result()
        final = await handle.query(DurableWorkflowRun.progress)

    assert final.running == []
    assert final.failed == []
    assert final.done == ["fetch_sales", "draft_report", "approve", "publish"]
    assert [t.node for t in final.transitions if not t.finished] == ["fetch_sales", "draft_report", "approve", "publish"]
    assert all(t.at >= 0 for t in final.transitions), "transition times come from workflow.now(), which replay reproduces"


def _free_port() -> int:
    """A port nothing holds right now, so this test's server cannot collide with the module's."""
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return int(probe.getsockname()[1])


async def test_a_waiting_gate_survives_the_server_it_was_waiting_on(tmp_path: Path) -> None:
    """The claim the SQLite file makes: history outlives the server process, not just the worker.

    The app's own `ensure_server` brings the server up, a run waits at its gate, and the context
    exits -- which stops the server, not just the worker. Entering it again on the same database
    file finds the same run still waiting, answers it, and the steps that already ran do not run
    again. Without the file this is a fresh, empty server and the handle would find nothing.
    """
    settings = RunSettings(run_engine="temporal", temporal_address=f"127.0.0.1:{_free_port()}")
    queue = "test-server-restart"

    async with AsyncExitStack() as first:
        try:
            outcome = await first.enter_async_context(ensure_server(settings, tmp_path))
        except Exception as exc:  # no network, no cached binary, no port -- none of them are failures here
            pytest.skip(f"no local Temporal dev server available: {type(exc).__name__}: {exc}")
        assert outcome.started is True, "nothing was listening on a port picked one line ago"
        client = await connect(settings.temporal_address)
        async with _worker(client, queue):
            handle = await client.start_workflow(
                DurableWorkflowRun.run,
                RunRequest(spec=QUARTERLY, params={"quarter": "Q1"}, workflow_id="quarterly-report"),
                id="run-across-server-restarts",
                task_queue=queue,
            )
            await _wait_until_waiting(handle, "approve")

    assert CALLS == ["fetch_sales", "draft_report"], "two steps ran before the server went down"
    assert (tmp_path / "temporal.db").exists(), "the history is a file, which is the whole point"

    async with AsyncExitStack() as second:
        outcome = await second.enter_async_context(ensure_server(settings, tmp_path))
        assert outcome.started is True, "the first server is gone, so this one is started too"
        client = await connect(settings.temporal_address)
        async with _worker(client, queue):
            handle = client.get_workflow_handle("run-across-server-restarts")
            await _wait_until_waiting(handle, "approve")
            await handle.signal(DurableWorkflowRun.decide, GateVerdict(gate_id="approve", approved=True))
            result = await handle.result()

    assert result == {"url": "https://reports.example/8"}
    assert CALLS == ["fetch_sales", "draft_report", "publish"], "replay reads completed activities from the file, never repeats them"
