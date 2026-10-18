"""The person gate's wire, tested where nobody answers.

A gated workflow driven over a plugin with no decide surface (an editor speaking ACP) used to
loop forever: the timeout rejects, the rejection reruns the upstream, the gate waits again --
every fifteen minutes, invisibly. Each unanswered wait is now a refusal like any other, recorded
as the deadline's own, and the gate's declared ceiling of reaches is what ends the run: one
count, in the workflow, rather than a second one kept here.
"""

from __future__ import annotations

import asyncio

from typing import TYPE_CHECKING, Any, cast

from harness.features.running.approval import ChatApproval
from harness.features.running.domain.progress import GateVerdict
from harness.session import NoSessionLog, Runtime

if TYPE_CHECKING:
    from core.ai.workflows.contracts import StepRegistry, Workflow

from tests.support.state import a_throwaway_state


def _approval(quarterly_report: Workflow, registry: StepRegistry, *, timeout: float) -> ChatApproval:
    session = Runtime(registry=registry, library=cast("Any", None), agent=cast("Any", None), session_log=NoSessionLog(), state=a_throwaway_state())
    return ChatApproval(session=session, workflow="wf.gated", spec=quarterly_report, timeout_seconds=timeout)


async def test_an_unanswered_park_refuses_in_the_deadlines_own_name(quarterly_report: Workflow, registry: StepRegistry) -> None:
    """Nothing irreversible happens, and nothing reads the refusal as a person's.

    Each refusal spends one of the gate's reaches, so a run over an entry that cannot answer a
    gate ends at the ceiling the workflow declared instead of waiting for ever.
    """
    approval = _approval(quarterly_report, registry, timeout=0.01)

    first = await approval.review("approve", "the draft")
    second = await approval.review("approve", "the draft")

    assert (first.approve, first.source) == (False, "timeout")
    assert (second.approve, second.source) == (False, "timeout")
    assert approval.rejections() == {"approve": 2}, "every unanswered wait is a refusal on the record"


async def test_a_person_present_and_rejecting_may_keep_rejecting(quarterly_report: Workflow, registry: StepRegistry) -> None:
    """A click and a deadline are both refusals, and the record says which was which."""
    approval = _approval(quarterly_report, registry, timeout=0.2)
    waiting = asyncio.create_task(approval.review("approve", "first pass"))
    for _ in range(500):
        if (decision := approval.session.active.decision) is not None:
            decision.set_result(GateVerdict(approve=False))  # a human rejection, not a timeout
            break
        await asyncio.sleep(0.001)

    clicked = await waiting

    assert (clicked.approve, clicked.source) == (False, "person")
    assert (await approval.review("approve", "second pass")).source == "timeout"
    assert approval.rejections() == {"approve": 2}


class FakeAsker:
    """A plugin with its own way of asking, scripted. Verdicts pop in order; asks are recorded."""

    def __init__(self, verdicts: list[bool]) -> None:
        self.verdicts = verdicts
        self.asked: list[tuple[str, str]] = []

    async def ask(self, gate_id: str, rendered: str) -> bool:
        self.asked.append((gate_id, rendered))
        return self.verdicts.pop(0)


async def test_a_door_with_an_asker_never_parks(quarterly_report: Workflow, registry: StepRegistry) -> None:
    """Over ACP nothing polls `/api/pending` and nothing can POST a decision: the plugin itself asks."""
    approval = _approval(quarterly_report, registry, timeout=0.01)
    asker = FakeAsker([False, True])
    routed = ChatApproval(
        session=approval.session, workflow=approval.workflow, spec=approval.spec, timeout_seconds=0.01, asker=asker
    )
    assert (await routed.review("approve", "first pass")).approve is False
    assert (await routed.review("approve", "second pass")).approve is True
    assert [gate for gate, _ in asker.asked] == ["approve", "approve"]
    assert routed.session.active.pending is None, "nothing waiting: no panel is polling for it"
    assert routed.rejections() == {"approve": 1}, "the asker's verdicts are evidence like any click"
