"""What automode answers at a gate it reaches: a judge gate is refused in its judge's name, every other gate passes."""

from __future__ import annotations

import pytest

from core.ai.workflows.contracts import (
    JUDGE_NOTE_FALLBACK,
    FromNode,
    GateDecision,
    JudgeGate,
    PersonGate,
    RunNode,
    Workflow,
    automode_refusal,
)


def _draft_judge_and_approve() -> Workflow:
    return Workflow(
        name="judged-and-approved",
        version=1,
        description="One drafting step, a judge gate over it, and a person gate after the judge.",
        nodes=(
            RunNode(id="draft", work="draft"),
            JudgeGate(id="check", work="check", reviews=FromNode(node="draft"), approved_when="outcome", rejected_to="draft"),
            PersonGate(id="approve", reviews=FromNode(node="check"), rejected_to="draft"),
        ),
        output=FromNode(node="approve"),
    )


def test_a_judge_gate_is_refused_in_the_judges_name_with_its_note() -> None:
    refusal = automode_refusal(_draft_judge_and_approve(), "check", "The drafts are empty.")

    assert refusal == GateDecision(approve=False, feedback="The drafts are empty.", source="judge")


@pytest.mark.parametrize("note", ["", "   \n\t "])
def test_a_judge_that_left_no_words_is_refused_with_the_fallback(note: str) -> None:
    refusal = automode_refusal(_draft_judge_and_approve(), "check", note)

    assert refusal == GateDecision(approve=False, feedback=JUDGE_NOTE_FALLBACK, source="judge")


@pytest.mark.parametrize("gate_id", ["approve", "no-such-gate"])
def test_a_gate_that_names_no_judge_is_not_refused(gate_id: str) -> None:
    """A person gate, or an id the workflow does not hold, is left for automode to pass."""
    assert automode_refusal(_draft_judge_and_approve(), gate_id, "The drafts are empty.") is None
