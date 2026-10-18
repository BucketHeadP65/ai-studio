"""Resume: a run rebuilt from its journal replays completed work instead of redoing it.

`memo` is the seed: node id -> the output a previous run recorded, consumed at most once.
Consume-once is what makes rewind semantics survive a resume -- a rejection target whose
memo was already spent runs fresh, exactly as a live rejection would rerun it. An approved
gate's memo entry is its recorded pass-through value, so replaying it never asks a human.
"""

from __future__ import annotations

from core.ai.workflows.contracts import (
    FromNode,
    FromParameter,
    JudgeGate,
    MapNode,
    ParameterSpec,
    RunNode,
    StepRegistry,
    Workflow,
    run_workflow,
)
from tests.unit.workflows.conftest import CallLog, CostData, Ledger, ReportDraft, ReportUrl, SalesData


class Recording:
    """Approve (or first-reject) while keeping every value a gate showed us."""

    def __init__(self, *, reject_first: bool = False) -> None:
        self.reviewed: list[tuple[str, object]] = []
        self.reject_first = reject_first

    async def review(self, gate_id: str, value: object, *, note: str = "") -> bool:
        del note
        self.reviewed.append((gate_id, value))
        return not (self.reject_first and len(self.reviewed) == 1)


class NobodyIsThere:
    """The hook a replay must never reach: asking proves the journal was ignored."""

    async def review(self, gate_id: str, value: object, *, note: str = "") -> bool:
        del value, note
        msg = f"gate {gate_id!r} asked a human during a replay that already recorded the answer"
        raise AssertionError(msg)


PREFIX: dict[str, object] = {
    "fetch_sales": SalesData(quarter="Q2", total=1000),
    "fetch_costs": CostData(quarter="Q2", total=600),
    "reconcile": Ledger(net=400),
    "draft_report": ReportDraft(text="net: 400"),
}


async def test_memo_short_circuits_the_completed_prefix(quarterly_report: Workflow, registry: StepRegistry, call_log: CallLog) -> None:
    approval = Recording()
    result = await run_workflow(quarterly_report, registry, params={"quarter": "Q2"}, approval=approval, memo=dict(PREFIX))
    assert isinstance(result, ReportUrl)
    assert call_log.calls == ["publish"], "every pre-gate step must replay from the journal"
    assert approval.reviewed == [("approve", ReportDraft(text="net: 400"))]


async def test_an_approved_gate_replays_without_asking_anyone(
    quarterly_report: Workflow, registry: StepRegistry, call_log: CallLog
) -> None:
    memo: dict[str, object] = dict(PREFIX) | {"approve": ReportDraft(text="net: 400")}
    result = await run_workflow(quarterly_report, registry, params={"quarter": "Q2"}, approval=NobodyIsThere(), memo=memo)
    assert isinstance(result, ReportUrl)
    assert call_log.calls == ["publish"]


async def test_rejection_after_resume_reruns_the_target_fresh(
    quarterly_report: Workflow, registry: StepRegistry, call_log: CallLog
) -> None:
    memo: dict[str, object] = dict(PREFIX) | {"draft_report": ReportDraft(text="the stale draft")}
    approval = Recording(reject_first=True)
    result = await run_workflow(quarterly_report, registry, params={"quarter": "Q2"}, approval=approval, memo=memo)
    assert isinstance(result, ReportUrl)
    assert call_log.calls == ["draft_report", "publish"], "the rewind target's memo was spent; it must run fresh"
    first, second = approval.reviewed
    assert first == ("approve", ReportDraft(text="the stale draft"))
    assert second == ("approve", ReportDraft(text="net: 400"))


async def test_a_map_replays_its_collected_result_without_fanning_out() -> None:
    ran = CallLog()
    reg = StepRegistry()

    async def shout(word: str) -> str:
        ran.calls.append(word)
        return word.upper()

    reg.register(shout)
    spec = Workflow(
        name="shout-all",
        version=1,
        description="uppercase every word",
        parameters={"words": ParameterSpec(description="the words", json_schema={"type": "array", "items": {"type": "string"}})},
        nodes=(MapNode(id="shouted", turns=("shout",), over=FromParameter(parameter="words"), each="word"),),
        output=FromNode(node="shouted"),
    )
    result = await run_workflow(spec, reg, params={"words": ["a", "b"]}, approval=NobodyIsThere(), memo={"shouted": ["A", "B"]})
    assert result == ["A", "B"]
    assert ran.calls == [], "a journaled map must not redo its fan-out"


async def test_an_approved_judge_replays_without_judging_again() -> None:
    ran = CallLog()
    reg = StepRegistry()

    async def polish(text: str) -> str:
        ran.calls.append(f"polish:{text}")
        return text.strip()

    async def judge_plainly(text: str) -> bool:
        ran.calls.append("judge")
        return True

    reg.register(polish)
    reg.register(judge_plainly)
    spec = Workflow(
        name="judged",
        version=1,
        description="draft, judge, report",
        parameters={"claim": ParameterSpec(description="the claim", json_schema={"type": "string"})},
        nodes=(
            RunNode(id="drafted", work="polish", inputs={"text": FromParameter(parameter="claim")}),
            JudgeGate(id="quality", work="judge_plainly", reviews=FromNode(node="drafted"), rejected_to="drafted"),
            RunNode(id="report", work="polish", inputs={"text": FromNode(node="quality")}),
        ),
        output=FromNode(node="report"),
    )
    result = await run_workflow(
        spec, reg, params={"claim": " fine "}, approval=NobodyIsThere(), memo={"drafted": "fine", "quality": "fine"}
    )
    assert result == "fine"
    assert ran.calls == ["polish:fine"], "only the step after the judge runs"
