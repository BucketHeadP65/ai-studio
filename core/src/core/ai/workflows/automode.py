"""What automode answers at a gate it reaches when the gate is a judge's."""

from __future__ import annotations

from typing import TYPE_CHECKING

from core.ai.workflows.domain.vocabulary import JudgeGate
from core.ai.workflows.graph_builder import GateDecision

if TYPE_CHECKING:
    from core.ai.workflows.domain.vocabulary import Workflow

JUDGE_NOTE_FALLBACK = "The judge could not decide and gave no reason. Rework the value so its judge can decide it."
"""The feedback of a judge's refusal under automode when the judge left no note."""


def automode_refusal(workflow: Workflow, gate_id: str, note: str) -> GateDecision | None:
    """The refusal automode answers a judge gate with, or `None` for a gate automode passes.

    A judge gate reaches an approval hook only when its judge could not decide. The refusal is
    credited to the judge and spends one of the gate's attempts. The caller counts it in its own record.

    Args:
        workflow: The workflow the run runs.
        gate_id: The gate being reviewed.
        note: What the judge said. A blank or whitespace-only note becomes `JUDGE_NOTE_FALLBACK`.

    Returns:
        A rejection credited to `"judge"` carrying the note, when `gate_id` names a `JudgeGate` in
        `workflow`. `None` for any other gate id.
    """
    if not any(isinstance(node, JudgeGate) and node.id == gate_id for node in workflow.nodes):
        return None
    return GateDecision(approve=False, feedback=note.strip() or JUDGE_NOTE_FALLBACK, source="judge")
