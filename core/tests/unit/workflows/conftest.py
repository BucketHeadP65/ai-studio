"""Shared fixtures: the quarterly-report workflow, with fake steps.

`publish` consumes `FromNode("approve")`, never `FromNode("draft_report")` -- publish without
approval is structurally impossible.
"""

from __future__ import annotations

from dataclasses import dataclass, field

import pytest

from core.ai.workflows.contracts import FromNode, FromParameter, ParameterSpec, PersonGate, RunNode, StepRegistry, Workflow


@dataclass(frozen=True)
class SalesData:
    quarter: str
    total: int


@dataclass(frozen=True)
class CostData:
    quarter: str
    total: int


@dataclass(frozen=True)
class Ledger:
    net: int


@dataclass(frozen=True)
class ReportDraft:
    text: str


@dataclass(frozen=True)
class ReportUrl:
    url: str


@dataclass
class CallLog:
    """Which steps ran, in order -- the assertion surface for loop tests."""

    calls: list[str] = field(default_factory=list)


@pytest.fixture
def call_log() -> CallLog:
    return CallLog()


class ApproveAll:
    """Approves every review and records what it reviewed -- the gate stand-in when the gate is not the subject."""

    def __init__(self) -> None:
        self.reviewed: list[tuple[str, object, str]] = []

    async def review(self, gate_id: str, value: object, *, note: str = "") -> bool:
        self.reviewed.append((gate_id, value, note))
        return True


class RejectFirst:
    """Reject the first review, approve every one after -- the rewind-loop probe."""

    def __init__(self) -> None:
        self.reviews = 0

    async def review(self, gate_id: str, value: object, *, note: str = "") -> bool:
        del gate_id, value, note
        self.reviews += 1
        return self.reviews > 1


def register_quarterly_steps(reg: StepRegistry, call_log: CallLog, *, exclude: frozenset[str] = frozenset()) -> None:
    """Register the report's deterministic steps; `exclude` lets a test swap one out."""

    async def fetch_sales(quarter: str) -> SalesData:
        call_log.calls.append("fetch_sales")
        return SalesData(quarter=quarter, total=1000)

    async def fetch_costs(quarter: str) -> CostData:
        call_log.calls.append("fetch_costs")
        return CostData(quarter=quarter, total=600)

    async def reconcile(sales: SalesData, costs: CostData) -> Ledger:
        call_log.calls.append("reconcile")
        return Ledger(net=sales.total - costs.total)

    async def draft_report(ledger: Ledger) -> ReportDraft:
        call_log.calls.append("draft_report")
        return ReportDraft(text=f"net: {ledger.net}")

    async def publish(report: ReportDraft) -> ReportUrl:
        call_log.calls.append("publish")
        return ReportUrl(url=f"https://reports.example/{len(report.text)}")

    for fn in (fetch_sales, fetch_costs, reconcile, draft_report, publish):
        if fn.__name__ not in exclude:
            reg.register(fn)


@pytest.fixture
def registry(call_log: CallLog) -> StepRegistry:
    reg = StepRegistry()
    register_quarterly_steps(reg, call_log)
    return reg


@pytest.fixture
def quarterly_report() -> Workflow:
    return Workflow(
        name="quarterly-report",
        version=3,
        description="Fetch sales and costs, reconcile, draft, human-approve, publish.",
        parameters={"quarter": ParameterSpec(description="Fiscal quarter", json_schema={"type": "string"})},
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
