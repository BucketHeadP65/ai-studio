"""A run's evidence counts every rejection its gates recorded, a judge's own included, and each one once."""

from __future__ import annotations

import asyncio

from typing import TYPE_CHECKING

import pytest

from core.ai.workflows.contracts import FromNode, JudgeGate, PersonGate, RunEvidence, RunNode, StepRegistry, Workflow

from harness.features.running import service
from harness.features.running.domain.progress import GateVerdict
from tests.support.fakes.broadcaster import FakeBroadcaster
from tests.support.runtime import a_runtime
from tests.support.state import a_state, records_under

if TYPE_CHECKING:
    from pathlib import Path

    from harness.features.running.contracts import RunRecords
    from harness.session import Runtime


async def _draft() -> str:
    return "draft A"


def _gated(gate: JudgeGate | PersonGate) -> Workflow:
    return Workflow(
        name="judged-drafts",
        version=1,
        description="One drafting step, and a gate that reviews what it drafted.",
        nodes=(RunNode(id="drafted", work="draft"), gate),
        output=FromNode(node=gate.id),
    )


async def _evidence(records: RunRecords, workflow_id: str) -> RunEvidence:
    (evidence,) = [event for event in await records.evidence.events_for(workflow_id) if isinstance(event, RunEvidence)]
    return evidence


async def _answer_the_gate(session: Runtime, *, approve: bool) -> None:
    """Wait for the run to wait at its gate, then answer it as a person would."""
    for _ in range(500):
        decision = session.active.decision
        if decision is not None and not decision.done():
            decision.set_result(GateVerdict(approve=approve, feedback="" if approve else "Cite the source."))
            return
        await asyncio.sleep(0.001)
    pytest.fail("the run never waited at its gate")


async def test_a_judge_that_rejects_twice_and_then_approves_is_on_the_record_with_two_rejections(tmp_path: Path) -> None:
    verdicts = iter((False, False, True))

    async def check(value: str) -> bool:
        del value
        return next(verdicts)

    registry = StepRegistry()
    registry.register(_draft, name="draft")
    registry.register(check)
    session = a_runtime(registry=registry, state=a_state(tmp_path))
    records = records_under(tmp_path)

    gate = JudgeGate(id="check", work="check", reviews=FromNode(node="drafted"), rejected_to="drafted")
    service.launch(session, records=records, topics=FakeBroadcaster(), spec=_gated(gate), workflow_id="wf.judged-drafts.v1", params={})
    task = session.active.task
    assert task is not None
    await task

    evidence = await _evidence(records, "wf.judged-drafts.v1")
    assert evidence.outcome == "completed"
    assert evidence.gates_rejected == {"check": 2}, "the judge's own rejections never pass the approval hook, and still count"


async def test_a_person_gate_refused_once_through_the_approval_hook_reads_one(tmp_path: Path) -> None:
    registry = StepRegistry()
    registry.register(_draft, name="draft")
    session = a_runtime(registry=registry, state=a_state(tmp_path))
    records = records_under(tmp_path)

    gate = PersonGate(id="approve", reviews=FromNode(node="drafted"), rejected_to="drafted")
    service.launch(session, records=records, topics=FakeBroadcaster(), spec=_gated(gate), workflow_id="wf.judged-drafts.v1", params={})
    task = session.active.task
    assert task is not None
    await _answer_the_gate(session, approve=False)
    await _answer_the_gate(session, approve=True)
    await task

    evidence = await _evidence(records, "wf.judged-drafts.v1")
    assert evidence.gates_approved == ("approve",)
    assert evidence.gates_rejected == {"approve": 1}, "the hook heard the refusal and the graph recorded it, and it counts once"
