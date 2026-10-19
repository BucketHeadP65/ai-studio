"""The run log: what a live run steps on disk, and what a resume makes of it.

The contract has three parts. The run log snapshots recorded outputs after every step,
atomically, dropping values that will not serialize rather than dying on them. Loading is
deliberately forgiving -- a missing, damaged, or outdated run log reads as nothing, never
as a refused boot. Reviving types each JSON value against the step that produced it, and
drops what no longer validates; a dropped value simply re-runs.
"""

from __future__ import annotations

import json

from typing import TYPE_CHECKING

import pytest

from core.ai.workflows.contracts import (
    Briefing,
    FromNode,
    FromParameter,
    MapBatch,
    MapNode,
    ParameterSpec,
    Shape,
    StepRegistry,
    Workflow,
    WorkItemFact,
    build_graph,
    run_workflow,
)

from harness.features.running.domain.progress import LiveProgress, Watchers
from harness.features.running.infrastructure.run_log import RunLog, load_waiting, revive_values, sweep_orphan_reviewed
from tests.conftest import CallLog, CostData, Ledger, ReportDraft, ReportUrl, SalesData

if TYPE_CHECKING:
    from pathlib import Path


LAUNCHED_RUN = "20260914T120000000000-abcdef12"
"""A launcher's own run id, in the shape `service.launch` mints. What everything else keys on."""

_MINIMAL_SPEC: dict[str, object] = {
    "name": "minimal",
    "version": 1,
    "description": "One node, so a hand-written log parses without a fixture.",
    "nodes": [{"kind": "run", "id": "digest", "work": "digest", "inputs": {}}],
    "output": {"node": "digest"},
}
"""A workflow a hand-written run log can carry, for the cases about the log rather than the graph."""


class ApproveEverything:
    async def review(self, gate_id: str, value: object, *, note: str = "") -> bool:
        return True


def begun(path: Path, spec: Workflow) -> RunLog:
    return RunLog.begin(path=path, run_id=LAUNCHED_RUN, workflow_id="wf.report", spec=spec, params={"quarter": "Q2"}, started_at="2026-08-22T12:00:00+00:00")


def test_snapshot_survives_and_revives(tmp_path: Path, quarterly_report: Workflow, registry: StepRegistry) -> None:
    run_log = begun(tmp_path / "run-log.json", quarterly_report)
    run_log.state.values["fetch_sales"] = SalesData(quarter="Q2", total=1000)
    run_log.finished("fetch_sales")
    run_log.state.values["reconcile"] = Ledger(net=400)
    run_log.finished("reconcile")

    waiting = load_waiting(tmp_path / "run-log.json")
    assert waiting is not None
    assert waiting.workflow_id == "wf.report"
    assert waiting.spec == quarterly_report
    assert waiting.params == {"quarter": "Q2"}
    revived = revive_values(waiting.spec, registry, waiting.values)
    assert revived == {"fetch_sales": SalesData(quarter="Q2", total=1000), "reconcile": Ledger(net=400)}
    assert isinstance(revived["fetch_sales"], SalesData)


def test_close_removes_the_run_log(tmp_path: Path, quarterly_report: Workflow) -> None:
    run_log = begun(tmp_path / "run-log.json", quarterly_report)
    run_log.state.values["fetch_sales"] = SalesData(quarter="Q2", total=1000)
    run_log.finished("fetch_sales")
    assert load_waiting(tmp_path / "run-log.json") is not None
    run_log.close()
    assert load_waiting(tmp_path / "run-log.json") is None
    run_log.close()  # closing what is already closed is not an event


def test_a_value_that_cannot_serialize_is_dropped_not_fatal(tmp_path: Path, quarterly_report: Workflow) -> None:
    run_log = begun(tmp_path / "run-log.json", quarterly_report)
    run_log.state.values["fetch_sales"] = object()
    run_log.state.values["fetch_costs"] = CostData(quarter="Q2", total=600)
    run_log.finished("fetch_costs")
    waiting = load_waiting(tmp_path / "run-log.json")
    assert waiting is not None
    assert "fetch_costs" in waiting.values
    assert "fetch_sales" not in waiting.values, "an unserializable value re-runs; it must not kill the snapshot"


def test_a_snapshot_leaves_out_a_rewound_key_until_it_is_written_again(tmp_path: Path, quarterly_report: Workflow) -> None:
    run_log = begun(tmp_path / "run-log.json", quarterly_report)
    run_log.state.values["fetch_sales"] = SalesData(quarter="Q2", total=1000)
    run_log.state.values["fetch_costs"] = CostData(quarter="Q2", total=600)
    run_log.state.rewound.add("fetch_costs")
    run_log.finished("fetch_sales")
    waiting = load_waiting(tmp_path / "run-log.json")
    assert waiting is not None
    assert "fetch_sales" in waiting.values
    assert "fetch_costs" not in waiting.values, "a rewound value would be replayed instead of rerun"

    run_log.state.rewound.discard("fetch_costs")
    run_log.finished("fetch_costs")
    waiting = load_waiting(tmp_path / "run-log.json")
    assert waiting is not None
    assert "fetch_costs" in waiting.values


def test_a_failed_step_does_not_snapshot(tmp_path: Path, quarterly_report: Workflow) -> None:
    run_log = begun(tmp_path / "run-log.json", quarterly_report)
    run_log.finished("fetch_sales", failed=True)
    assert load_waiting(tmp_path / "run-log.json") is None


def test_corrupt_or_alien_journals_read_as_nothing(tmp_path: Path) -> None:
    assert load_waiting(tmp_path / "missing.json") is None
    (tmp_path / "corrupt.json").write_text("{not json", encoding="utf-8")
    assert load_waiting(tmp_path / "corrupt.json") is None
    (tmp_path / "alien.json").write_text(json.dumps({"workflow_id": "x"}), encoding="utf-8")
    assert load_waiting(tmp_path / "alien.json") is None


def test_revive_drops_what_no_longer_validates(quarterly_report: Workflow, registry: StepRegistry) -> None:
    revived = revive_values(
        quarterly_report, registry, {"reconcile": {"wrong_field": 1}, "draft_report": {"text": "ok"}, "ghost_node": {"text": "?"}}
    )
    assert revived == {"draft_report": ReportDraft(text="ok")}


def test_revive_types_a_gate_by_what_it_reviews(quarterly_report: Workflow, registry: StepRegistry) -> None:
    revived = revive_values(quarterly_report, registry, {"approve": {"text": "net: 400"}})
    assert revived == {"approve": ReportDraft(text="net: 400")}
    assert isinstance(revived["approve"], ReportDraft)


def test_revive_types_a_map_as_a_list_of_its_step_output(registry: StepRegistry) -> None:
    reg = StepRegistry()

    async def shout(word: str) -> str:
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
    revived = revive_values(spec, reg, {"shouted": ["A", "B"]})
    assert revived == {"shouted": ["A", "B"]}


def _briefing(name: str, inputs: dict[str, Shape], output: Shape, *, tools: tuple[str, ...] = ()) -> Briefing:
    instructions = f"Perform the turn named {name!r}, start to finish, exactly as briefed here, for this test."
    return Briefing(name=name, instructions=instructions, inputs=inputs, output=output, tools=tools)


async def _stub_perform(task: Briefing, output_type: type, /, **inputs: object) -> object:
    """Never called: reviving a run_log only types values, it never performs a briefing."""
    raise NotImplementedError


def _digest_and_relate_spec() -> Workflow:
    """One two-turn map, both turns briefings -- what a multi turn map's grammar requires."""
    digest_source = _briefing("digest_source", {"source": Shape(json_schema={"type": "string"})}, Shape(json_schema={"type": "string"}))
    relate = _briefing("relate", {"digest": Shape(json_schema={"type": "string"})}, Shape(json_schema={"type": "integer"}))
    return Workflow(
        name="digest-and-relate",
        version=1,
        description="digest each source, then relate it to the base",
        parameters={"sources": ParameterSpec(description="the sources", json_schema={"type": "array", "items": {"type": "string"}})},
        inline=(digest_source, relate),
        nodes=(MapNode(id="digests", turns=("digest_source", "relate"), over=FromParameter(parameter="sources"), each="source"),),
        output=FromNode(node="digests"),
    )


def test_revive_types_a_multi_turn_maps_dotted_values_independently() -> None:
    """Every turn of a multi-turn map revives under its own dotted key, typed to that turn's own output."""
    spec = _digest_and_relate_spec()
    registry = StepRegistry(perform=_stub_perform)
    revived = revive_values(spec, registry, {"digests": [1, 2], "digests.digest_source": ["a", "b"], "digests.relate": [1, 2]})
    assert revived == {"digests": [1, 2], "digests.digest_source": ["a", "b"], "digests.relate": [1, 2]}


def test_revive_drops_a_wrong_shaped_dotted_value_alone() -> None:
    """One turn's run_log predates a shape change.

    That turn's list is dropped, the map's own value stays, and the indexer re-runs a map
    missing any turn's list whole.
    """
    spec = _digest_and_relate_spec()
    registry = StepRegistry(perform=_stub_perform)
    revived = revive_values(
        spec, registry, {"digests": [1, 2], "digests.digest_source": ["a", "b"], "digests.relate": [{"wrong": "shape"}]}
    )
    assert revived == {"digests": [1, 2], "digests.digest_source": ["a", "b"]}
    assert "digests.relate" not in revived


async def test_a_waiting_run_resumes_from_its_journal(
    tmp_path: Path, quarterly_report: Workflow, registry: StepRegistry, call_log: CallLog
) -> None:
    """The whole point of the file: die at the gate, come back, repay nothing."""

    class DiedError(Exception): ...

    class DiesAtTheGate:
        async def review(self, gate_id: str, value: object, *, note: str = "") -> bool:
            raise DiedError

    run_log = begun(tmp_path / "run-log.json", quarterly_report)
    graph = build_graph(quarterly_report, registry, approval=DiesAtTheGate(), observer=run_log)
    with pytest.raises(DiedError):
        await graph.run(state=run_log.state)
    # no run_log.close(): the process died -- that is the one case the file exists for

    waiting = load_waiting(tmp_path / "run-log.json")
    assert waiting is not None
    memo = revive_values(waiting.spec, registry, waiting.values)
    call_log.calls.clear()
    result = await run_workflow(waiting.spec, registry, params=dict(waiting.params), approval=ApproveEverything(), memo=memo)
    assert isinstance(result, ReportUrl)
    assert call_log.calls == ["publish"], "the second life must repay none of the first life's work"


def test_watchers_fan_out_every_transition() -> None:
    first, second = LiveProgress(), LiveProgress()
    both = Watchers(first, second)
    both.started("a")
    both.finished("a")
    both.finished("b", failed=True)
    for one in (first, second):
        assert one.done == ["a", "b"]
        assert one.failed == {"b"}


def test_replayed_marks_done_without_running() -> None:
    progress = LiveProgress()
    progress.replayed("fetch_sales")
    assert progress.done == ["fetch_sales"]
    assert progress.running == set()
    assert [e["node"] for e in progress.events] == ["fetch_sales"]
    assert progress.events[0]["finished"] is True


def test_the_values_a_run_reviews_are_addressed_under_the_run_that_was_launched(tmp_path: Path, quarterly_report: Workflow) -> None:
    """The launcher's own id, not one the log invented: the ledger, the events and the archive all key on it.

    The log itself is deleted the moment the run ends, so an id known only to the log would leave
    the values it reviewed unreachable from anything an ended run left behind.
    """
    run_log = begun(tmp_path / "run-log.json", quarterly_report)

    reviewed = run_log.state.reviewed_artifacts.persist("draft_report", None, {"text": "net: 400"})
    run_log.finished("draft_report")

    assert reviewed.run_id == LAUNCHED_RUN
    waiting = load_waiting(tmp_path / "run-log.json")
    assert waiting is not None
    assert waiting.run_id == LAUNCHED_RUN


# --- what a run's work facts and its reviewed values do to the log -----------------------------


def test_the_log_carries_every_generation_of_facts_and_the_input_each_belongs_to(tmp_path: Path, quarterly_report: Workflow) -> None:
    run_log = begun(tmp_path / "run-log.json", quarterly_report)
    run_log.state.batches["digest"] = MapBatch(generation=1, ordered_digest="a" * 64, items=("1:0:aa", "1:1:bb"))
    for fact in (
        WorkItemFact(node_id="digest", generation=0, item_key="0:0:aa", disposition="failed"),
        WorkItemFact(node_id="digest", generation=1, item_key="1:0:aa", disposition="succeeded", retry_attempts=1),
        WorkItemFact(node_id="digest", generation=1, item_key="1:1:bb", disposition="skipped", detail="the fetch timed out"),
    ):
        run_log.state.work_facts[fact.identity] = fact
    run_log.finished("digest")

    waiting = load_waiting(tmp_path / "run-log.json")

    assert waiting is not None
    assert len(waiting.work_facts) == 3, "an audit generation is kept beside the active one"
    assert waiting.batches["digest"].items == ("1:0:aa", "1:1:bb")
    assert waiting.batches["digest"].generation == 1
    assert len(waiting.batches["digest"].items) == 2, "the primary's input cardinality reads off its own batch"
    settled = {fact.item_key: fact for fact in waiting.work_facts if fact.generation == 1}
    assert settled["1:0:aa"].retry_attempts == 1
    assert settled["1:1:bb"].detail == "the fetch timed out"


@pytest.mark.parametrize("key", ["work_facts", "batches", "retry_feedback", "gate_decisions", "gate_generations", "run_id"])
def test_a_log_missing_any_of_its_run_state_is_a_log_nothing_resumes_from(
    tmp_path: Path, quarterly_report: Workflow, key: str
) -> None:
    log = begun(tmp_path / "run-log.json", quarterly_report)
    log.started("fetch_costs")
    raw = json.loads((tmp_path / "run-log.json").read_text(encoding="utf-8"))
    del raw[key]
    (tmp_path / "run-log.json").write_text(json.dumps(raw), encoding="utf-8")

    assert load_waiting(tmp_path / "run-log.json") is None


def test_a_damaged_batch_costs_the_batches_and_not_the_whole_log(tmp_path: Path) -> None:
    path = tmp_path / "run-log.json"
    payload = {"workflow_id": "wf.report", "spec": _MINIMAL_SPEC, "params": {}, "values": {}, "batches": {"digest": {"nonsense": 1}}}
    path.write_text(json.dumps(payload), encoding="utf-8")

    assert load_waiting(path) is None, "a log that cannot be trusted about its work is a log nothing resumes from"


def test_the_values_a_run_reviewed_are_readable_back_as_a_set_and_cleared_for_the_next_run(
    tmp_path: Path, quarterly_report: Workflow
) -> None:
    path = tmp_path / "run-log.json"
    first = begun(path, quarterly_report)
    first.state.reviewed_artifacts.persist("draft_report", None, {"text": "net: 400"})
    first.state.reviewed_artifacts.persist("draft_report", None, {"text": "net: 500"})

    kept = first.reviewed()
    assert len(kept) == 2, "every value a gate saw this run, read back off disk"
    assert {json.loads(value.canonical)["text"] for value in kept} == {"net: 400", "net: 500"}
    assert {value.ref.run_id for value in kept} == {LAUNCHED_RUN}

    second = RunLog.begin(path=path, run_id="another-run", workflow_id="wf.report", spec=quarterly_report, params={}, started_at="2026-08-22T12:00:00+00:00")

    assert second.reviewed() == (), "a new run starts with nothing beside its log -- nothing could read the old values anyway"
    assert not list((path.parent / "reviewed").glob("*.json"))


def test_an_ending_reads_what_was_refused_before_the_log_closes_and_takes_it_with_it(tmp_path: Path, quarterly_report: Workflow) -> None:
    """The ending copies them into its archive record, which is where a reference resolves from afterwards."""
    path = tmp_path / "run-log.json"
    run_log = begun(path, quarterly_report)
    run_log.state.reviewed_artifacts.persist("draft_report", None, {"text": "net: 400"})
    run_log.finished("draft_report")

    assert len(run_log.reviewed()) == 1, "the ending reads what was refused while its log is still open"

    run_log.close()

    assert not path.exists()
    assert run_log.reviewed() == ()
    assert not (tmp_path / "reviewed" / LAUNCHED_RUN).exists(), "and this run's own subtree goes with it"


def test_one_sessions_run_never_deletes_another_sessions_captured_values(tmp_path: Path, quarterly_report: Workflow) -> None:
    """Two pooled sessions keep their run logs side by side, so their reviewed values must not share a home."""
    first = RunLog.begin(
        path=tmp_path / "sess-a.waiting-run.json", run_id=LAUNCHED_RUN, workflow_id="wf.report", spec=quarterly_report, params={},
        started_at="2026-08-22T12:00:00+00:00",
    )
    held = first.state.reviewed_artifacts.persist("draft_report", None, {"text": "net: 400"})

    second = RunLog.begin(
        path=tmp_path / "sess-b.waiting-run.json", run_id="another-run", workflow_id="wf.report", spec=quarterly_report, params={},
        started_at="2026-08-22T12:00:00+00:00",
    )
    second.state.reviewed_artifacts.persist("draft_report", None, {"text": "net: 900"})

    assert len(first.reviewed()) == 1, "the run still in flight kept every value its gates saw"
    assert first.state.reviewed_artifacts.read(held) == {"text": "net: 400"}, "and a repair can still read back what was refused"
    assert len(second.reviewed()) == 1, "and the run that started second sees only its own"

    first.close()

    assert first.reviewed() == (), "the ending archived them, so the run that ended takes its own with it"
    assert len(second.reviewed()) == 1, "and leaves the other session's alone"


def test_the_boot_sweep_removes_every_reviewed_subtree_no_run_log_still_names(tmp_path: Path, quarterly_report: Workflow) -> None:
    """A process death and a resume both leave a subtree nobody will close; boot is where they go.

    The live run's own subtree stays, and so does the one belonging to the pooled session beside it:
    a sweep that read only the log it was handed would delete values another session still owes a
    repair over.
    """
    live = tmp_path / "run-log.json"
    running = begun(live, quarterly_report)
    running.state.reviewed_artifacts.persist("draft_report", None, {"text": "net: 400"})
    running.started("draft_report")
    pooled = RunLog.begin(
        path=tmp_path / "sess-a.waiting-run.json", run_id="pooled-run", workflow_id="wf.report", spec=quarterly_report, params={},
        started_at="2026-08-22T12:00:00+00:00",
    )
    pooled.state.reviewed_artifacts.persist("draft_report", None, {"text": "net: 900"})
    pooled.started("draft_report")
    orphan = RunLog.begin(path=tmp_path / "gone.json", run_id="dead-run", workflow_id="wf.report", spec=quarterly_report, params={}, started_at="2026-08-22T12:00:00+00:00")
    orphan.state.reviewed_artifacts.persist("draft_report", None, {"text": "net: 000"})
    orphan.started("draft_report")
    (tmp_path / "gone.json").unlink()  # the process died here, so nothing ever closed that run

    assert sweep_orphan_reviewed(live) == 1, "one subtree, and only the one no log still names"

    assert (tmp_path / "reviewed" / LAUNCHED_RUN).is_dir(), "the live run keeps what its own gate reviewed"
    assert (tmp_path / "reviewed" / "pooled-run").is_dir(), "and so does the pooled session's run beside it"
    assert not (tmp_path / "reviewed" / "dead-run").exists(), "the run whose log is gone takes its values with it"


def test_a_run_log_keeps_the_gate_mode_timeout_and_runner_it_was_launched_with(tmp_path: Path, quarterly_report: Workflow) -> None:
    log = RunLog.begin(
        path=tmp_path / "run-log.json",
        run_id=LAUNCHED_RUN,
        workflow_id="wf.report",
        spec=quarterly_report,
        params={"quarter": "Q2"},
        gate_mode="auto",
        gate_timeout=None,
        runner="schedule:nightly",
        session_kind="runner",
        started_at="2026-08-22T12:00:00+00:00",
    )
    log.started("fetch_costs")

    waiting = load_waiting(tmp_path / "run-log.json")

    assert waiting is not None
    assert (waiting.gate_mode, waiting.gate_timeout, waiting.runner) == ("auto", None, "schedule:nightly")
    assert waiting.session_kind == "runner"


@pytest.mark.parametrize("key", ["gate_mode", "gate_timeout", "runner", "session_kind", "run_title", "started_at"])
def test_a_run_log_missing_a_launch_setting_is_a_log_nothing_resumes_from(tmp_path: Path, quarterly_report: Workflow, key: str) -> None:
    log = begun(tmp_path / "run-log.json", quarterly_report)
    log.started("fetch_costs")
    raw = json.loads((tmp_path / "run-log.json").read_text(encoding="utf-8"))
    del raw[key]
    (tmp_path / "run-log.json").write_text(json.dumps(raw), encoding="utf-8")

    assert load_waiting(tmp_path / "run-log.json") is None


def test_a_run_log_reads_an_unknown_session_kind_as_chat_and_keeps_its_start_time(tmp_path: Path, quarterly_report: Workflow) -> None:
    log = RunLog.begin(
        path=tmp_path / "run-log.json",
        run_id=LAUNCHED_RUN,
        workflow_id="wf.report",
        spec=quarterly_report,
        params={"quarter": "Q2"},
        started_at="2026-09-14T12:00:00+00:00",
    )
    log.started("fetch_costs")
    assert (load_waiting(tmp_path / "run-log.json") or pytest.fail("no log")).started_at == "2026-09-14T12:00:00+00:00"
    raw = json.loads((tmp_path / "run-log.json").read_text(encoding="utf-8"))
    raw["session_kind"] = "nonsense"
    (tmp_path / "run-log.json").write_text(json.dumps(raw), encoding="utf-8")

    waiting = load_waiting(tmp_path / "run-log.json")

    assert waiting is not None
    assert waiting.session_kind == "chat"
