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

import pytest

from core.ai.workflows.contracts import JUDGE_NOTE_FALLBACK, FromNode, GateDecision, JudgeGate, RunNode, Workflow

from harness.features.running.approval import ChatApproval
from harness.features.running.domain.progress import GateVerdict
from harness.session import NoSessionLog, Runtime

if TYPE_CHECKING:
    from core.ai.workflows.contracts import StepRegistry

    from harness.domain.gates import GateMode

from tests.support.state import a_throwaway_state


def _approval(spec: Workflow, registry: StepRegistry, *, timeout: float, gate_mode: GateMode = "wait") -> ChatApproval:
    session = Runtime(registry=registry, library=cast("Any", None), agent=cast("Any", None), session_log=NoSessionLog(), state=a_throwaway_state())
    session.active.gate_mode = gate_mode
    return ChatApproval(session=session, workflow="wf.gated", spec=spec, timeout_seconds=timeout)


def _judged() -> Workflow:
    """One drafting step and a judge gate that reviews what it drafted."""
    return Workflow(
        name="judged-drafts",
        version=1,
        description="One drafting step, and a judge gate that reviews what it drafted.",
        nodes=(
            RunNode(id="drafted", work="draft"),
            JudgeGate(id="check", work="check", reviews=FromNode(node="drafted"), approved_when="outcome", rejected_to="drafted"),
        ),
        output=FromNode(node="check"),
    )


async def _until_parked(approval: ChatApproval) -> asyncio.Future[GateVerdict]:
    for _ in range(500):
        if (decision := approval.session.active.decision) is not None:
            return decision
        await asyncio.sleep(0.001)
    msg = "the gate never parked"
    raise AssertionError(msg)


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
    assert approval.decisions == {"approve": [False, False]}, "every unanswered wait is a refusal on the record"


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
    assert approval.decisions == {"approve": [False, False]}


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
    assert routed.decisions == {"approve": [False, True]}, "the asker's verdicts are evidence like any click"


# --- a judge that cannot decide, under each gate mode -----------------------------------------


async def test_under_automode_a_judge_that_cannot_decide_sends_the_work_back_with_its_note(registry: StepRegistry) -> None:
    """Automode decides nothing, so nobody approves what the judge could not judge.

    The refusal is the judge's own, carrying its note back to the step the gate reruns, and it
    spends one of the gate's reaches like any other refusal.
    """
    approval = _approval(_judged(), registry, timeout=0.01, gate_mode="auto")

    decision = await approval.review("check", "the draft", note="The drafts are empty.")

    assert decision == GateDecision(approve=False, feedback="The drafts are empty.", source="judge")
    assert approval.decisions == {"check": [False]}
    assert approval.auto_gates() == (), "automode passed nothing, so the record counts nothing in its name"
    assert approval.session.active.pending is None, "nothing waits for a person"


@pytest.mark.parametrize("note", ["", "  \n "])
async def test_under_automode_a_judge_that_left_no_note_is_refused_with_words_the_rerun_can_act_on(registry: StepRegistry, note: str) -> None:
    approval = _approval(_judged(), registry, timeout=0.01, gate_mode="auto")

    decision = await approval.review("check", "the draft", note=note)

    assert decision == GateDecision(approve=False, feedback=JUDGE_NOTE_FALLBACK, source="judge")


async def test_under_automode_a_person_gate_still_passes_with_nobody_asked(quarterly_report: Workflow, registry: StepRegistry) -> None:
    approval = _approval(quarterly_report, registry, timeout=0.01, gate_mode="auto")

    decision = await approval.review("approve", "the draft")

    assert decision == GateDecision(approve=True, source="auto")
    assert approval.auto_gates() == ("approve",)
    assert approval.decisions == {}, "an automatic pass is never a verdict on the record"


async def test_waiting_a_judge_that_cannot_decide_still_asks_a_person(registry: StepRegistry) -> None:
    approval = _approval(_judged(), registry, timeout=0.5)
    waiting = asyncio.create_task(approval.review("check", "the draft", note="The drafts are empty."))
    decision = await _until_parked(approval)

    pending = approval.session.active.pending
    assert pending is not None
    assert pending.reviewing.startswith("The reviewer could not judge this and said: The drafts are empty.\n\n")
    decision.set_result(GateVerdict(approve=True))

    assert await waiting == GateDecision(approve=True, source="person")
    assert approval.decisions == {"check": [True]}
