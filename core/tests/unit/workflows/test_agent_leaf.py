"""Fenced improvisation, made real: a pydantic-ai agent as one step of the workflow.

The spec is byte-identical to the deterministic one -- swapping a function step for an
agent step touches zero artifacts, because the spec references work by name and the
contract (Ledger in, ReportDraft out) is the step's, not the workflow's. The agent
improvises freely *inside* the typed cell; the wiring, the gate, and the envelope
neither know nor care that a model is in there.

An agent step is integrator code -- this module is the documented pattern, not an API.
"""

from __future__ import annotations

from pydantic_ai import Agent
from pydantic_ai.models.test import TestModel
from pydantic_ai.usage import UsageLimits

from core.ai.workflows.contracts import StepRegistry, Workflow, run_workflow
from tests.unit.workflows.conftest import ApproveAll, CallLog, Ledger, ReportDraft, ReportUrl, register_quarterly_steps

drafting_agent: Agent[None, ReportDraft] = Agent(
    TestModel(), output_type=ReportDraft, instructions="You draft quarterly revenue reports from a reconciled ledger."
)


async def test_agent_step_runs_inside_the_compiled_graph(quarterly_report: Workflow, call_log: CallLog) -> None:
    registry = StepRegistry()
    register_quarterly_steps(registry, call_log, exclude=frozenset({"draft_report"}))

    async def draft_report(ledger: Ledger) -> ReportDraft:
        call_log.calls.append("draft_report")
        result = await drafting_agent.run(
            f"Draft the quarterly report. Net position: {ledger.net}.", usage_limits=UsageLimits(request_limit=3)
        )
        return result.output

    registry.register(draft_report)

    result = await run_workflow(quarterly_report, registry, params={"quarter": "Q2"}, approval=ApproveAll())
    assert isinstance(result, ReportUrl)
    assert call_log.calls.count("draft_report") == 1
    assert call_log.calls[-1] == "publish"


async def test_agent_output_satisfies_the_step_contract(call_log: CallLog) -> None:
    """The agent's structured output IS the step's return contract -- one type, one truth."""
    result = await drafting_agent.run("Draft anything.", usage_limits=UsageLimits(request_limit=3))
    assert isinstance(result.output, ReportDraft)
