"""The report: the honest table. No aggregate hides a failed or contaminated row."""

from __future__ import annotations

from typing import Literal

from core.ai.workflows.contracts import Briefing, FromParameter, Program, Shape, Workflow

from proof.records import RunRecord
from proof.report import DistillationCost, collective_step_budget, render_report


def _record(
    question_id: str,
    path: Literal["baseline", "reuse"],
    *,
    recall: float,
    requests: int,
    failed: bool = False,
    contaminated: bool = False,
) -> RunRecord:
    return RunRecord(
        question_id=question_id,
        path=path,
        surfaced=(),
        recall=recall,
        missed=(),
        requests=requests,
        input_tokens=100,
        output_tokens=50,
        seconds=1.0,
        contaminated=contaminated,
        failed=failed,
    )


def test_the_report_shows_break_even_and_never_hides_a_bad_row() -> None:
    baseline = [_record("q.a", "baseline", recall=0.8, requests=30), _record("q.b", "baseline", recall=0.7, requests=34)]
    reuse = [_record("q.a", "reuse", recall=0.8, requests=6), _record("q.b", "reuse", recall=0.5, requests=7, failed=True)]
    text = render_report([*baseline, *reuse], DistillationCost(workflow_id="wf.r.v1", model_calls=40, validator_round_trips=3))
    assert "q.b" in text  # a failed run is a row
    assert "FAILED" in text
    assert "break-even" in text.lower()  # 40 / (32 - 6.5) ≈ 2 runs — assert the number the code computes
    assert "validator round-trips: 3" in text.lower() or "3 validator" in text.lower()


def test_missing_distillation_states_it_was_not_recorded_rather_than_crashing() -> None:
    text = render_report([_record("q.a", "baseline", recall=1.0, requests=10)], None)
    assert "distillation cost not recorded" in text.lower()


def test_excluded_rows_are_listed_and_never_folded_into_the_clean_aggregate() -> None:
    records = [
        _record("q.a", "reuse", recall=0.9, requests=5),
        _record("q.b", "reuse", recall=0.1, requests=5, contaminated=True),
    ]
    text = render_report(records, None)
    assert "q.b" in text
    assert "CONTAMINATED" in text
    # the clean aggregate reflects only q.a's recall (0.90), not an average with the contaminated run
    assert "0.90" in text
    assert "0.50" not in text


def test_missing_reuse_records_render_what_exists_and_say_what_is_absent() -> None:
    text = render_report([_record("q.a", "baseline", recall=1.0, requests=10)], None)
    assert "no reuse" in text.lower()


def test_empty_records_render_without_crashing() -> None:
    text = render_report([], None)
    assert "no run records" in text.lower()


async def test_collective_step_budget_sums_tool_bearing_and_briefing_only_tasks() -> None:
    spec = Workflow(
        name="wf-test",
        version=1,
        description="a fixture spec for the budget arithmetic, nothing more",
        inline=(
            Briefing(
                name="synthesize",
                instructions="read the surfaced evidence and synthesize a short cited answer",
                output=Shape(json_schema={"type": "string"}),
                tools=("run_sql",),
            ),
            Program(name="merge", source="result = 1", output=Shape(json_schema={"type": "string"}), tools=()),
        ),
        nodes=(),
        output=FromParameter(parameter="question"),
    )

    async def _fetch(workflow_id: str) -> Workflow:
        assert workflow_id == "wf.r.v1"
        return spec

    budget = await collective_step_budget("wf.r.v1", fetch_workflow=_fetch)
    assert budget == 24 + 4  # DEFINED_STEP_BUDGET (tool-bearing) + BRIEFING_ONLY_BUDGET (briefing-only)


async def test_collective_step_budget_degrades_to_none_when_the_store_is_unreachable() -> None:
    async def _raises(workflow_id: str) -> Workflow:
        msg = f"no route to the store fetching {workflow_id}"
        raise ConnectionError(msg)

    assert await collective_step_budget("wf.r.v1", fetch_workflow=_raises) is None


async def test_collective_step_budget_is_none_without_a_workflow_id() -> None:
    assert await collective_step_budget(None) is None
