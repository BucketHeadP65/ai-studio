"""Each run of a consumer version on live trial is read as ignored, failed, worse or counting, by what the run recorded."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING

import pytest

from core.ai.workflows.contracts import (
    FromNode,
    FromParameter,
    Grade,
    JudgeGate,
    LiveTrialMarker,
    LiveTrialStep,
    LiveTrialVerdict,
    Outcome,
    ParameterSpec,
    PersonGate,
    RunEvidence,
    RunNode,
    Workflow,
    live_trial_verdict,
)

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence

    from core.ai.workflows.contracts import Node, OutcomeStatus

T0 = datetime(2026, 11, 5, 9, 0, tzinfo=UTC)
SOURCE = {"source": ParameterSpec(description="the source", json_schema={"type": "string"})}


def _workflow(*nodes: Node, version: int = 2) -> Workflow:
    return Workflow(name="digest-sources", version=version, description="Digest one source.", parameters=SOURCE, nodes=nodes, output=FromNode(node=nodes[-1].id))


FETCH = RunNode(id="fetch", work="fetch", inputs={"source": FromParameter(parameter="source")})
DIGEST = RunNode(id="digest", work="digest", inputs={"text": FromNode(node="fetch")})
CHECK = JudgeGate(id="check", work="check", reviews=FromNode(node="digest"), rejected_to="digest")
PUBLISH = RunNode(id="publish", work="publish", inputs={"page": FromNode(node="check")})
LIVE_TRIAL = _workflow(FETCH, DIGEST, CHECK, PUBLISH)
"""The live trial version: `digest` runs the adopted step and wires from `fetch`, a judge checks it."""
PREVIOUS = _workflow(FETCH, DIGEST, CHECK, PUBLISH, version=1)
"""The version the adoption replaced, holding the same gate."""
TOUCHED = ("digest",)
EVERY_NODE = {"fetch": "str", "digest": "str", "check": "str", "publish": "str"}


def _run(
    *,
    outcome: str = "completed",
    status: OutcomeStatus = "succeeded",
    produced: Mapping[str, str] = EVERY_NODE,
    rejected: Mapping[str, int] | None = None,
    at: datetime = T0,
    version: int = 2,
) -> RunEvidence:
    return RunEvidence.model_validate({
        "workflow_id": f"wf.digest-sources.v{version}",
        "spec_hash": f"hash-v{version}",
        "workflow_version": version,
        "outcome": outcome,
        "result": Outcome(status=status, summary="what the run did"),
        "gates_auto": (),
        "gates_orchestrated": (),
        "gates_rejected": dict(rejected or {}),
        "produced": dict(produced),
        "at": at,
    })


def _baseline(*statuses: OutcomeStatus, rejected: Mapping[str, int] | None = None) -> tuple[RunEvidence, ...]:
    return tuple(_run(status=status, rejected=rejected, version=1, at=T0 - timedelta(days=index + 1)) for index, status in enumerate(statuses))


def _grade(run: RunEvidence, score: float, *, measure: str = "evidence-recall") -> Grade:
    return Grade(
        workflow_id=run.workflow_id,
        spec_hash=run.spec_hash,
        workflow_version=run.workflow_version,
        judge_kind="structural",
        judge="recall/groundtruth-v1",
        measure=measure,
        score=score,
        graded_run_at=run.at,
    )


def _verdict(
    run: RunEvidence,
    *,
    baseline: Sequence[RunEvidence] = (),
    run_grades: Sequence[Grade] = (),
    baseline_grades: Sequence[Grade] = (),
    workflow: Workflow = LIVE_TRIAL,
    touched: Sequence[str] = TOUCHED,
) -> LiveTrialVerdict:
    return live_trial_verdict(workflow, touched, run, run_grades=run_grades, baseline=baseline, baseline_grades=baseline_grades, previous=PREVIOUS)


def test_a_stopped_run_is_ignored() -> None:
    verdict = _verdict(_run(outcome="stopped", produced={"fetch": "str"}), baseline=_baseline("succeeded"))

    assert verdict.kind == "ignored"
    assert "stopped" in verdict.reason


def test_a_failed_run_where_a_node_the_touched_node_wires_from_produced_nothing_is_ignored() -> None:
    verdict = _verdict(_run(outcome="failed", status="failed", produced={}), baseline=_baseline("succeeded"))

    assert verdict.kind == "ignored"
    assert "fetch" in verdict.reason


def test_a_failed_run_where_the_touched_node_alone_did_not_produce_is_failed() -> None:
    verdict = _verdict(_run(outcome="failed", status="failed", produced={"fetch": "str"}))

    assert verdict.kind == "failed"
    assert "digest" in verdict.reason


def test_a_touched_node_wired_only_from_parameters_that_did_not_produce_is_failed() -> None:
    lone = RunNode(id="digest", work="digest", inputs={"text": FromParameter(parameter="source")})
    workflow = _workflow(lone, RunNode(id="publish", work="publish", inputs={"page": FromNode(node="digest")}))

    verdict = _verdict(_run(outcome="failed", status="unknown", produced={}), workflow=workflow)

    assert verdict.kind == "failed"


@pytest.mark.parametrize("produced", [{"fetch": "str"}, EVERY_NODE], ids=["the touched node silent", "every node produced"])
def test_a_completed_run_whose_checked_status_is_failed_ranks_below_a_baseline_that_did_not_fail_and_is_worse(produced: Mapping[str, str]) -> None:
    verdict = _verdict(_run(status="failed", produced=produced), baseline=_baseline("succeeded", "partial", "unknown"))

    assert verdict.kind == "worse"
    assert "failed" in verdict.reason


def test_a_completed_run_whose_checked_status_is_failed_counts_against_a_baseline_that_failed_too() -> None:
    verdict = _verdict(_run(status="failed"), baseline=_baseline("failed", "failed"))

    assert verdict.kind == "counts"


@pytest.mark.parametrize("produced", [{"fetch": "str"}, EVERY_NODE], ids=["the touched node silent", "every node produced"])
def test_a_completed_run_whose_checked_status_is_failed_is_ignored_without_a_baseline(produced: Mapping[str, str]) -> None:
    verdict = _verdict(_run(status="failed", produced=produced))

    assert verdict.kind == "ignored"
    assert "no completed run" in verdict.reason


def test_a_completed_partial_run_against_three_succeeded_runs_is_worse() -> None:
    verdict = _verdict(_run(status="partial"), baseline=_baseline("succeeded", "succeeded", "noop"))

    assert verdict.kind == "worse"
    assert "partial" in verdict.reason


def test_a_status_that_ranks_with_one_of_the_baselines_counts() -> None:
    verdict = _verdict(_run(status="partial"), baseline=_baseline("succeeded", "partial", "succeeded"))

    assert verdict.kind == "counts"


@pytest.mark.parametrize("baseline", [(), _baseline("succeeded", "succeeded", "succeeded")], ids=["no baseline", "a baseline"])
def test_a_run_that_failed_outside_the_touched_nodes_is_ignored(baseline: tuple[RunEvidence, ...]) -> None:
    verdict = _verdict(_run(outcome="failed", status="failed"), baseline=baseline)

    assert verdict.kind == "ignored"
    assert "outside" in verdict.reason


def test_a_gate_rejected_more_times_than_in_any_baseline_run_is_worse_a_judges_rejections_included() -> None:
    baseline = (*_baseline("succeeded", rejected={"check": 1}), *_baseline("succeeded"))

    verdict = _verdict(_run(rejected={"check": 2}), baseline=baseline)

    assert verdict.kind == "worse"
    assert "check" in verdict.reason


def test_a_gate_rejected_as_often_as_the_most_of_the_baseline_counts() -> None:
    verdict = _verdict(_run(rejected={"check": 1}), baseline=_baseline("succeeded", rejected={"check": 1}))

    assert verdict.kind == "counts"


def test_a_gate_the_previous_version_did_not_hold_is_not_compared() -> None:
    review = PersonGate(id="review", reviews=FromNode(node="digest"), rejected_to="digest")
    workflow = _workflow(FETCH, DIGEST, review, RunNode(id="publish", work="publish", inputs={"page": FromNode(node="review")}))

    verdict = _verdict(_run(rejected={"review": 2}), baseline=_baseline("succeeded"), workflow=workflow)

    assert verdict.kind == "counts"


@pytest.mark.parametrize(("score", "kind"), [(0.6, "worse"), (0.65, "worse"), (0.7, "counts"), (0.75, "counts")])
def test_a_structural_grade_more_than_a_tenth_below_the_baselines_mean_is_worse(score: float, kind: str) -> None:
    run = _run()
    baseline = _baseline("succeeded", "succeeded")
    baseline_grades = (_grade(baseline[0], 0.7), _grade(baseline[1], 0.9))

    verdict = _verdict(run, baseline=baseline, run_grades=(_grade(run, score),), baseline_grades=baseline_grades)

    assert verdict.kind == kind


def test_a_structural_grade_is_compared_with_the_baselines_grades_of_the_same_measure() -> None:
    run = _run()
    baseline = _baseline("succeeded")

    verdict = _verdict(run, baseline=baseline, run_grades=(_grade(run, 0.5, measure="precision"),), baseline_grades=(_grade(baseline[0], 0.9),))

    assert verdict.kind == "counts"


def test_an_empty_baseline_makes_only_failed_possible() -> None:
    run = _run(status="partial", rejected={"check": 2})

    assert _verdict(run, run_grades=(_grade(run, 0.1),)).kind == "counts"
    assert _verdict(_run(outcome="failed", status="failed", produced={"fetch": "str"})).kind == "failed"


def test_a_marker_counts_three_runs_unless_it_says_otherwise_and_refuses_none() -> None:
    marker = LiveTrialMarker(previous="wf.digest-sources.v1", steps=(LiveTrialStep(step="digest", step_hash="abc"),))

    assert marker.runs == 3
    with pytest.raises(ValueError, match="greater than or equal to 1"):
        LiveTrialMarker(previous="wf.digest-sources.v1", steps=(LiveTrialStep(step="digest", step_hash="abc"),), runs=0)


def test_a_marker_holds_at_least_one_step_and_each_step_once() -> None:
    with pytest.raises(ValueError, match="at least 1"):
        LiveTrialMarker(previous="wf.digest-sources.v1", steps=())
    with pytest.raises(ValueError, match="digest"):
        LiveTrialMarker(
            previous="wf.digest-sources.v1", steps=(LiveTrialStep(step="digest", step_hash="abc"), LiveTrialStep(step="digest", step_hash="def"))
        )


def test_a_marker_with_steps_keeps_its_previous_puts_each_new_step_after_the_earlier_ones_and_replaces_an_earlier_step_of_the_same_name() -> None:
    earlier = LiveTrialMarker(previous="wf.digest-sources.v1", steps=(LiveTrialStep(step="digest", step_hash="a"), LiveTrialStep(step="fetch", step_hash="b")))

    stacked = earlier.with_steps((LiveTrialStep(step="summarize", step_hash="c"), LiveTrialStep(step="digest", step_hash="d")))

    assert stacked.previous == "wf.digest-sources.v1"
    assert [(each.step, each.step_hash) for each in stacked.steps] == [("fetch", "b"), ("summarize", "c"), ("digest", "d")]
    assert stacked.runs == earlier.runs
