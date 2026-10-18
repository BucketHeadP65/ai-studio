"""The run observer: which node is running, reported as it happens.

The interesting assertion is the first one. Two steps each wait for the other to start before
either returns, so the test can only pass if the graph really runs them at the same time --
if compilation ever quietly went back to running nodes in sequence, this deadlocks and the
timeout fails it. That makes "independent steps run concurrently" a checked property rather
than a claim in a docstring, and it is exactly what the dashboard draws when two boxes light
up together.
"""

from __future__ import annotations

import asyncio

from dataclasses import dataclass, field

import pytest

from core.ai.workflows.contracts import FromNode, FromParameter, ParameterSpec, PersonGate, RunNode, StepRegistry, Workflow, run_workflow
from tests.unit.workflows.conftest import (
    ApproveAll,
    CallLog,
    CostData,
    Ledger,
    RejectFirst,
    ReportDraft,
    SalesData,
    register_quarterly_steps,
)


@dataclass
class Recorder:
    """Every start and finish, in order, plus who was running at the same time."""

    events: list[tuple[str, str]] = field(default_factory=list)
    overlaps: set[frozenset[str]] = field(default_factory=set)
    _live: set[str] = field(default_factory=set)

    def started(self, node_id: str) -> None:
        self._live.add(node_id)
        self.events.append(("start", node_id))
        if len(self._live) > 1:
            self.overlaps.add(frozenset(self._live))

    def finished(self, node_id: str, *, failed: bool = False) -> None:
        self._live.discard(node_id)
        self.events.append(("fail" if failed else "finish", node_id))

    @property
    def order(self) -> list[str]:
        return [node for kind, node in self.events if kind == "finish"]


async def test_forked_steps_are_reported_running_at_the_same_time() -> None:
    """Each step blocks until the other has started, so sequential execution cannot pass."""
    registry = StepRegistry()
    both_started = {name: asyncio.Event() for name in ("fetch_sales", "fetch_costs")}

    async def fetch_sales(quarter: str) -> SalesData:
        both_started["fetch_sales"].set()
        await asyncio.wait_for(both_started["fetch_costs"].wait(), timeout=2)
        return SalesData(quarter=quarter, total=1000)

    async def fetch_costs(quarter: str) -> CostData:
        both_started["fetch_costs"].set()
        await asyncio.wait_for(both_started["fetch_sales"].wait(), timeout=2)
        return CostData(quarter=quarter, total=600)

    async def reconcile(sales: SalesData, costs: CostData) -> Ledger:
        return Ledger(net=sales.total - costs.total)

    for fn in (fetch_sales, fetch_costs, reconcile):
        registry.register(fn)

    spec = Workflow(
        name="fork",
        version=1,
        description="two fetches at once, then a join",
        parameters={"quarter": ParameterSpec(description="q", json_schema={"type": "string"})},
        nodes=(
            RunNode(id="fetch_sales", work="fetch_sales", inputs={"quarter": FromParameter(parameter="quarter")}),
            RunNode(id="fetch_costs", work="fetch_costs", inputs={"quarter": FromParameter(parameter="quarter")}),
            RunNode(
                id="reconcile", work="reconcile", inputs={"sales": FromNode(node="fetch_sales"), "costs": FromNode(node="fetch_costs")}
            ),
        ),
        output=FromNode(node="reconcile"),
    )
    watcher = Recorder()

    result = await run_workflow(spec, registry, params={"quarter": "Q2"}, approval=ApproveAll(), observer=watcher)

    assert isinstance(result, Ledger)
    assert frozenset({"fetch_sales", "fetch_costs"}) in watcher.overlaps
    assert watcher.order[-1] == "reconcile"  # the join really did wait for both


async def test_a_gate_is_reported_running_while_the_human_thinks(
    quarterly_report: Workflow, registry: StepRegistry, call_log: CallLog
) -> None:
    """The dashboard shows the gate as `waiting for you`, which needs the gate to report itself."""
    del call_log
    watcher = Recorder()
    await run_workflow(quarterly_report, registry, params={"quarter": "Q2"}, approval=ApproveAll(), observer=watcher)

    assert ("start", "approve") in watcher.events
    assert watcher.events.index(("start", "approve")) < watcher.events.index(("finish", "publish"))


async def test_a_rejected_gate_reports_the_rerun(quarterly_report: Workflow, registry: StepRegistry, call_log: CallLog) -> None:
    """A rejection reruns the guarded step, so that step finishes twice -- say so."""
    del call_log
    watcher = Recorder()
    await run_workflow(quarterly_report, registry, params={"quarter": "Q2"}, approval=RejectFirst(), observer=watcher)

    assert watcher.events.count(("finish", "draft_report")) == 2
    assert watcher.events.count(("finish", "fetch_sales")) == 1  # upstream work is never repaid


async def test_a_step_that_raises_is_reported_failed_not_left_running(call_log: CallLog) -> None:
    """A watcher must never be left showing a step as running after it has stopped."""
    registry = StepRegistry()
    register_quarterly_steps(registry, call_log, exclude=frozenset({"draft_report"}))

    async def draft_report(ledger: Ledger) -> ReportDraft:
        del ledger
        msg = "no printer"
        raise RuntimeError(msg)

    registry.register(draft_report)
    spec = Workflow(
        name="breaks",
        version=1,
        description="the drafting step throws",
        parameters={"quarter": ParameterSpec(description="q", json_schema={"type": "string"})},
        nodes=(
            RunNode(id="fetch_sales", work="fetch_sales", inputs={"quarter": FromParameter(parameter="quarter")}),
            RunNode(id="fetch_costs", work="fetch_costs", inputs={"quarter": FromParameter(parameter="quarter")}),
            RunNode(
                id="reconcile", work="reconcile", inputs={"sales": FromNode(node="fetch_sales"), "costs": FromNode(node="fetch_costs")}
            ),
            RunNode(id="draft_report", work="draft_report", inputs={"ledger": FromNode(node="reconcile")}),
            PersonGate(id="approve", reviews=FromNode(node="draft_report"), rejected_to="draft_report"),
            RunNode(id="publish", work="publish", inputs={"report": FromNode(node="approve")}),
        ),
        output=FromNode(node="publish"),
    )
    watcher = Recorder()

    # pydantic_graph may deliver the failure bare or wrapped in an exception group
    with pytest.raises((RuntimeError, BaseExceptionGroup)):
        await run_workflow(spec, registry, params={"quarter": "Q2"}, approval=ApproveAll(), observer=watcher)

    assert ("fail", "draft_report") in watcher.events
    assert not watcher._live
