"""The run's own event grammar: past-tense facts, each naming its run, and a forgiving codec.

The lifecycle of a run belongs to the workflow that ran. Every event carries the run id and the
workflow id, so one line says what it is about without the file around it.
"""

from __future__ import annotations

from datetime import UTC, datetime

from core.ai.workflows.contracts import (
    GateDecided,
    GateReached,
    RecordedRunEvent,
    RunFinished,
    RunLaunched,
    RunPaused,
    RunResumed,
    RunStopped,
    decode_run_event,
    encode_run_event,
)

AT = datetime(2026, 9, 3, 12, 30, tzinfo=UTC)


def test_every_run_event_names_its_run_and_its_workflow() -> None:
    events = (
        RunLaunched(run_id="r1", workflow_id="wf.report"),
        RunPaused(run_id="r1", workflow_id="wf.report"),
        RunResumed(run_id="r1", workflow_id="wf.report"),
        RunStopped(run_id="r1", workflow_id="wf.report"),
        RunFinished(run_id="r1", workflow_id="wf.report", status="completed"),
        GateReached(run_id="r1", workflow_id="wf.report", gate_id="review"),
        GateDecided(run_id="r1", workflow_id="wf.report", gate_id="review", approved=True),
    )
    assert {event.run_id for event in events} == {"r1"}
    assert {event.workflow_id for event in events} == {"wf.report"}


def test_every_kind_is_past_tense() -> None:
    kinds = {
        RunLaunched(run_id="r1", workflow_id="wf.report").kind,
        RunPaused(run_id="r1", workflow_id="wf.report").kind,
        RunResumed(run_id="r1", workflow_id="wf.report").kind,
        RunStopped(run_id="r1", workflow_id="wf.report").kind,
        RunFinished(run_id="r1", workflow_id="wf.report", status="failed").kind,
        GateReached(run_id="r1", workflow_id="wf.report", gate_id="review").kind,
        GateDecided(run_id="r1", workflow_id="wf.report", gate_id="review", approved=False).kind,
    }
    assert kinds == {"run_launched", "run_paused", "run_resumed", "run_stopped", "run_finished", "gate_reached", "gate_decided"}


def test_a_line_round_trips_through_the_codec() -> None:
    record = RecordedRunEvent(seq=4, at=AT, event=GateDecided(run_id="r1", workflow_id="wf.report", gate_id="review", approved=True))
    line = encode_run_event(record)
    assert decode_run_event(line) == record


def test_the_line_is_flat_so_it_greps_without_the_codec() -> None:
    line = encode_run_event(RecordedRunEvent(seq=1, at=AT, event=RunLaunched(run_id="r1", workflow_id="wf.report")))
    assert '"kind": "run_launched"' in line
    assert '"run_id": "r1"' in line
    assert '"seq": 1' in line


def test_a_torn_line_costs_one_event_and_never_the_read() -> None:
    assert decode_run_event("{not json") is None
    assert decode_run_event('{"seq": 1}') is None
    assert decode_run_event('{"seq": 1, "at": "2026-09-03T12:30:00+00:00", "kind": "something_else"}') is None


def test_a_decided_gate_defaults_to_a_person() -> None:
    decided = GateDecided(run_id="r1", workflow_id="wf.report", gate_id="review", approved=True)
    assert decided.by == "human"
