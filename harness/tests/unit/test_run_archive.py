"""Ended runs rest in the archive instead of vanishing when their session log closes."""

from __future__ import annotations

import json

from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any, Literal, cast

import pytest

from core.ai.artifacts import artifact_digest
from core.ai.workflows.contracts import (
    Briefing,
    FromNode,
    FromParameter,
    GateDecision,
    GateDecisionFact,
    MapBatch,
    MapNode,
    Outcome,
    ParameterSpec,
    PersonGate,
    ResultArtifactSpec,
    RetryExhaustedError,
    RetryFeedback,
    ReviewedArtifactRef,
    RunNode,
    Shape,
    StepRegistry,
    Workflow,
    WorkflowResultSpec,
    WorkflowState,
    WorkItemFact,
    build_graph,
    canonical_reviewed_value,
    reviewed_value_digest,
)
from core.ai.workflows.domain.vocabulary import RequiredGateCheck

from harness.container import create_container
from harness.features.running.contracts import RunArchive
from harness.features.running.domain.ended_run import EndedRun, ReviewedValue, RunArchiveRecord, checked_outcome
from harness.features.running.infrastructure.archive import FileRunArchive
from harness.features.running.service import archive_run
from harness.session import NoSessionLog, Runtime
from harness.settings import HarnessSettings, RunSettings
from tests.support.container import answering_from_memory, rooted_at
from tests.support.staging import staged_names
from tests.support.state import KEPT_DAYS, a_state, records_under

if TYPE_CHECKING:
    from pathlib import Path


def _record(
    *,
    run_id: str,
    workflow_id: str = "wf.report",
    run_title: str = "",
    ended_at: str | None = None,
    started_at: str | None = None,
    pinned: bool = False,
    conversations: dict[str, list[dict[str, object]]] | None = None,
) -> RunArchiveRecord:
    now = datetime.now(UTC).isoformat()
    return RunArchiveRecord(
        run_id=run_id,
        workflow_id=workflow_id,
        run_title=run_title,
        outcome="completed",
        started_at=started_at or now,
        ended_at=ended_at or now,
        runner="app:harness",
        session_id="sess-1",
        params={"quarter": "Q2"},
        spec={"name": "quarterly-report"},
        values={"reconcile": {"net": 400}},
        conversations=conversations or {},
        events=[{"seq": 1, "node": "reconcile", "at": 0.1, "finished": True, "failed": False}],
        pinned=pinned,
    )


def _session(tmp_path: Path) -> Runtime:
    return Runtime(registry=StepRegistry(), library=cast("Any", None), agent=cast("Any", None), session_log=NoSessionLog(), state=a_state(tmp_path))


def _spec() -> Workflow:
    return Workflow(
        name="quarterly-report",
        version=1,
        description="A minimal spec for the archive tests.",
        parameters={"quarter": ParameterSpec(description="Fiscal quarter", json_schema={"type": "string"})},
        nodes=(RunNode(id="reconcile", work="reconcile", inputs={}),),
        output=FromNode(node="reconcile"),
    )


def test_put_get_and_list_for_round_trip(tmp_path: Path) -> None:
    archive = FileRunArchive(tmp_path, retention_days=KEPT_DAYS)
    record = _record(run_id="run-1", conversations={"draft_report": [{"kind": "request"}]})

    archive.put(record)

    assert archive.get("run-1") == record, "get reads the whole record back, unchanged"
    listed = archive.list_for("wf.report", limit=10)
    assert [item.run_id for item in listed] == ["run-1"]
    assert listed[0].values == {}, "list_for loads envelope fields only, never the heavy payload"
    assert listed[0].spec == {}
    assert listed[0].events == []
    assert listed[0].conversations == {}, "a listing read must not validate every worker's full transcript"
    assert listed[0].outcome == "completed"


def test_list_for_sorts_newest_first_and_respects_the_limit(tmp_path: Path) -> None:
    archive = FileRunArchive(tmp_path, retention_days=KEPT_DAYS)
    now = datetime.now(UTC)
    for offset, run_id in ((3, "oldest"), (1, "middle"), (2, "newest")):
        archive.put(_record(run_id=run_id, ended_at=(now - timedelta(hours=offset)).isoformat()))

    assert [item.run_id for item in archive.list_for("wf.report", limit=10)] == ["middle", "newest", "oldest"]
    assert [item.run_id for item in archive.list_for("wf.report", limit=2)] == ["middle", "newest"]


def test_recent_reads_across_every_workflow_newest_first(tmp_path: Path) -> None:
    archive = FileRunArchive(tmp_path, retention_days=KEPT_DAYS)
    now = datetime.now(UTC)
    archive.put(_record(run_id="a", workflow_id="wf.one", ended_at=(now - timedelta(hours=2)).isoformat()))
    archive.put(_record(run_id="b", workflow_id="wf.two", ended_at=(now - timedelta(hours=1)).isoformat()))

    assert [item.run_id for item in archive.recent(10)] == ["b", "a"]


def test_sweep_removes_old_unpinned_keeps_pinned_and_fresh(tmp_path: Path) -> None:
    archive = FileRunArchive(tmp_path, retention_days=KEPT_DAYS)
    now = datetime.now(UTC)
    archive.put(_record(run_id="old", ended_at=(now - timedelta(days=40)).isoformat()))
    archive.put(_record(run_id="old-pinned", ended_at=(now - timedelta(days=40)).isoformat(), pinned=True))
    archive.put(_record(run_id="fresh", ended_at=now.isoformat()))

    removed = archive.sweep(30)

    assert removed == 1
    assert archive.get("old") is None
    assert archive.get("old-pinned") is not None
    assert archive.get("fresh") is not None


def test_set_pinned_and_delete(tmp_path: Path) -> None:
    archive = FileRunArchive(tmp_path, retention_days=KEPT_DAYS)
    archive.put(_record(run_id="run-1"))

    assert archive.set_pinned("run-1", pinned=True) is True
    record = archive.get("run-1")
    assert record is not None
    assert record.pinned is True

    assert archive.set_pinned("no-such-run", pinned=True) is False
    assert archive.delete("run-1") is True
    assert archive.get("run-1") is None
    assert archive.delete("run-1") is False


class TestTheRunSectionsRetentionReachesTheArchiveTheContainerBuilds:
    """`archive_days` is one number in one section, and the container is what carries it to the sweep.

    Asserted through the real container rather than a hand-built archive: the registration is the
    only thing between the deployment's own knob and the number a sweep reads, so an archive built
    directly proves nothing about what a deployment gets.
    """

    async def _archive(self, run: RunSettings, home: Path) -> RunArchive:
        """The archive this deployment's container hands out, its state home under `home`."""
        container = create_container(rooted_at(HarnessSettings(run=run), home))
        with answering_from_memory(container):
            archive = await container.get(RunArchive)
        await container.close()
        return archive

    async def test_archive_days_zero_sweeps_everything(self, tmp_path: Path) -> None:
        archive = await self._archive(RunSettings(archive_days=0), tmp_path)
        archive.put(_record(run_id="ended-just-now"))

        removed = archive.sweep(archive.retention_days)

        assert removed == 1
        assert archive.get("ended-just-now") is None

    async def test_the_default_sweeps_nothing_younger_than_the_sections_own_number(self, tmp_path: Path) -> None:
        archive = await self._archive(RunSettings(), tmp_path)
        now = datetime.now(UTC)
        archive.put(_record(run_id="inside", ended_at=(now - timedelta(days=KEPT_DAYS - 1)).isoformat()))
        archive.put(_record(run_id="outside", ended_at=(now - timedelta(days=KEPT_DAYS + 1)).isoformat()))

        removed = archive.sweep(archive.retention_days)

        assert removed == 1
        assert archive.get("inside") is not None
        assert archive.get("outside") is None


def test_archive_days_zero_means_archive_run_is_a_no_op(tmp_path: Path) -> None:
    records = replace(records_under(tmp_path), archive=FileRunArchive(tmp_path / "archive", retention_days=0))
    session = _session(tmp_path)
    session.session_id = "sess-1"
    session.active.workflow_id = "wf.report"
    session.active.started_at = datetime.now(UTC).isoformat()

    archive_run(
        session,
        archive=records.archive,
        outcome="completed",
        failure="",
        runner="app:harness",
        spec=_spec(),
        params={"quarter": "Q2"},
        values={},
        conversations={},
        gates_approved=(),
        gates_rejected={},
        gates_auto=(),
    )

    assert records.archive.recent(10) == []


def test_an_archived_run_carries_started_at_before_ended_at(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    records = records_under(tmp_path)
    session = _session(tmp_path)
    session.session_id = "sess-1"
    session.active.workflow_id = "wf.report"
    session.active.started_at = datetime.now(UTC).isoformat()

    archive_run(
        session,
        archive=records.archive,
        outcome="completed",
        failure="",
        runner="app:harness",
        spec=_spec(),
        params={"quarter": "Q2"},
        values={"reconcile": {"net": 400}},
        conversations={},
        gates_approved=("approve",),
        gates_rejected={},
        gates_auto=(),
    )

    listed = records.archive.recent(10)
    assert len(listed) == 1
    record = records.archive.get(listed[0].run_id)
    assert record is not None
    assert record.started_at <= record.ended_at
    assert record.workflow_id == "wf.report"
    assert record.session_id == "sess-1"
    assert record.gates_approved == ("approve",)
    assert record.gates_auto == ()
    assert record.values == {"reconcile": {"net": 400}}


# --- the checked outcome, and the references a record fixes ------------------------------------


_DRAFT = {"text": "net: 400", "net": 400}


def _declaring(*, empty_primary: Literal["noop", "unknown"] = "unknown") -> Workflow:
    """A map over a parameter, one gate over its output, and two declared artifacts."""
    return Workflow(
        name="declaring",
        version=1,
        description="Map a parameter, review the result, and declare what a caller may select.",
        parameters={"words": ParameterSpec(description="items", json_schema={"type": "array", "items": {"type": "string"}})},
        inline=(
            Briefing(
                name="shout",
                instructions="Shout one word back, in capitals, and nothing else at all besides it.",
                inputs={"word": Shape(json_schema={"type": "string"})},
                output=Shape(json_schema={"type": "string"}),
            ),
        ),
        nodes=(
            MapNode(id="each", turns=("shout",), over=FromParameter(parameter="words"), each="word"),
            PersonGate(id="approve", reviews=FromNode(node="each"), rejected_to="each"),
        ),
        output=FromNode(node="approve"),
        result=WorkflowResultSpec(
            summary="the shouting",
            primary_work="each",
            empty_primary=empty_primary,
            required_gates=(RequiredGateCheck(gate_id="approve", accepted_sources=("person",)),),
            artifacts=(
                ResultArtifactSpec(id="shouted", label="What was shouted", source=FromNode(node="each"), media_type="application/json"),
            ),
        ),
    )


def _ended(**overrides: object) -> EndedRun:
    fields: dict[str, object] = {
        "spec": _declaring(),
        "run_id": "run-1",
        "status": "completed",
        "values": {"each": ["A", "B"]},
        "params": {"words": "a, b"},
        "work_facts": [
            WorkItemFact(node_id="each", generation=0, item_key="0:0:aa", disposition="succeeded"),
            WorkItemFact(node_id="each", generation=0, item_key="0:1:bb", disposition="succeeded"),
        ],
        "batches": {"each": MapBatch(generation=0, ordered_digest="a" * 64, items=("0:0:aa", "0:1:bb"))},
        "gate_decisions": [],
        "gate_generations": {"approve": 1},
    }
    return EndedRun(**(fields | overrides))  # type: ignore[arg-type]


def _approved(digest: str) -> GateDecisionFact:
    return GateDecisionFact(
        gate_id="approve", attempt=1, verdict="approve", source="person", review_generation=1, reviewed_value_digest=digest
    )


def _digest(value: object) -> str:
    return reviewed_value_digest(canonical_reviewed_value(value))


def test_a_run_whose_work_settled_and_whose_gate_accepted_it_checks_out_as_succeeded() -> None:
    result = checked_outcome(_ended(gate_decisions=[_approved(_digest(["A", "B"]))]))

    assert result.status == "succeeded"
    assert (result.attempted, result.succeeded, result.primary_input) == (2, 2, 2)
    assert [check.passed for check in result.checks] == [True]


def test_the_gate_check_is_put_over_the_bytes_the_run_ended_holding_and_no_others() -> None:
    result = checked_outcome(_ended(gate_decisions=[_approved(_digest(["A", "OTHER"]))]))

    assert result.status == "unknown", "an approval of other bytes releases nothing"
    assert result.checks[0].passed is False


def test_a_reference_is_minted_over_the_value_the_archive_will_actually_keep() -> None:
    result = checked_outcome(_ended(gate_decisions=[_approved(_digest(["A", "B"]))]))

    (ref,) = result.artifacts
    assert (ref.id, ref.node_id, ref.turn, ref.run_id) == ("shouted", "each", None, "run-1")
    assert ref.value_digest == artifact_digest(["A", "B"])
    assert ref.media_type == "application/json"


def test_no_reference_is_minted_for_an_artifact_the_run_never_produced_a_value_for() -> None:
    result = checked_outcome(_ended(values={}, work_facts=[], batches={}))

    assert result.artifacts == (), "an address nobody could resolve is worse than no address"
    assert result.status == "unknown"


def test_a_stopped_run_reports_what_settled_and_claims_nothing() -> None:
    result = checked_outcome(_ended(status="stopped", gate_decisions=[_approved(_digest(["A", "B"]))]))

    assert result.status == "unknown"
    assert (result.attempted, result.succeeded) == (2, 2)


def test_an_archived_record_carries_its_checked_outcome_and_the_values_its_gates_reviewed(tmp_path: Path) -> None:
    archive = FileRunArchive(tmp_path, retention_days=KEPT_DAYS)
    canonical = canonical_reviewed_value(_DRAFT)
    kept = ReviewedValue(
        ref=ReviewedArtifactRef(run_id="run-1", node_id="each", turn=None, value_digest=reviewed_value_digest(canonical)),
        canonical=canonical,
    )
    archive.put(
        _record(run_id="run-1").model_copy(
            update={"result": Outcome(status="partial", summary="one of two"), "reviewed": (kept,)}
        )
    )

    read_back = archive.get("run-1")

    assert read_back is not None
    assert read_back.result is not None
    assert read_back.result.status == "partial"
    assert read_back.reviewed == (kept,)


def test_a_listing_row_leaves_the_reviewed_values_out_and_still_carries_the_checked_outcome(tmp_path: Path) -> None:
    archive = FileRunArchive(tmp_path, retention_days=KEPT_DAYS)
    canonical = canonical_reviewed_value(_DRAFT)
    kept = ReviewedValue(
        ref=ReviewedArtifactRef(run_id="run-1", node_id="each", turn=None, value_digest=reviewed_value_digest(canonical)),
        canonical=canonical,
    )
    archive.put(_record(run_id="run-1").model_copy(update={"result": Outcome(status="succeeded", summary="all of it"), "reviewed": (kept,)}))

    (row,) = archive.recent(10)

    assert row.reviewed == (), "a listing row stays light"
    assert row.result is not None
    assert row.result.status == "succeeded"


def test_a_record_archived_before_checked_outcomes_still_parses_and_says_nothing(tmp_path: Path) -> None:
    archive = FileRunArchive(tmp_path, retention_days=KEPT_DAYS)
    archive.put(_record(run_id="run-old"))

    read_back = archive.get("run-old")

    assert read_back is not None
    assert read_back.result is None
    assert read_back.reviewed == ()


_LEGACY_ARCHIVE_JSON = (
    '{"run_id":"run-legacy","workflow_id":"wf.quarterly-close","outcome":"completed",'
    '"started_at":"2026-01-05T09:00:00+00:00","ended_at":"2026-01-05T09:04:00+00:00",'
    '"runner":"app:harness","session_id":"sess-legacy","params":{"quarter":"Q1"},'
    '"spec":{"name":"quarterly-close","version":1},'
    '"values":{"collect_sales":{"total":900}},'
    '"events":[{"seq":1,"node":"collect_sales","at":0.2,"finished":true,"failed":false}],'
    '"gates_approved":["approve_report"]}'
)
"""One archived run exactly as it would have been written to disk before `result` and `reviewed`
existed -- hand-typed, never dumped from today's model, so a change to what gets defaulted would
fail loudly here rather than the fixture quietly growing the field along with the code."""


def test_a_record_literally_written_before_result_and_reviewed_existed_still_parses() -> None:
    legacy = RunArchiveRecord.model_validate_json(_LEGACY_ARCHIVE_JSON)

    assert legacy.result is None, "an archive predating the checked outcome cannot say what happened -- unknown, honestly"
    assert legacy.reviewed == ()
    assert legacy.gates_rejected == {}
    assert legacy.gates_auto == ()
    assert legacy.gates_orchestrated == ()
    assert legacy.conversations == {}


# --- a required gate reviewing a downstream render, over a real graph ---------------------------


@dataclass(frozen=True)
class Draft:
    text: str


@dataclass(frozen=True)
class Render:
    body: str


@dataclass(frozen=True)
class Shipped:
    where: str


def _reviewing_a_render(*, sign_off_attempts: int = 3) -> Workflow:
    """`sign_off` reviews what `render` made of the primary's draft, never the draft itself.

    A rejection at `final` rewinds the primary, so `render` runs again and `sign_off` is reached a
    second time over a second generation of the same bytes.
    """
    return Workflow(
        name="reviewing-a-render",
        version=1,
        description="Draft, render the draft, sign the render off, ship it, and review the shipping.",
        parameters={"topic": ParameterSpec(description="anything", json_schema={"type": "string"})},
        nodes=(
            RunNode(id="draft", work="draft", inputs={"topic": FromParameter(parameter="topic")}),
            RunNode(id="render", work="render", inputs={"draft": FromNode(node="draft")}),
            PersonGate(id="sign_off", reviews=FromNode(node="render"), rejected_to="draft", max_attempts=sign_off_attempts),
            RunNode(id="ship", work="ship", inputs={"render": FromNode(node="sign_off")}),
            PersonGate(id="final", reviews=FromNode(node="ship"), rejected_to="draft", max_attempts=2),
        ),
        output=FromNode(node="final"),
        result=WorkflowResultSpec(
            summary="the shipped render",
            primary_work="draft",
            required_gates=(RequiredGateCheck(gate_id="sign_off", accepted_sources=("person",)),),
        ),
    )


def _render_registry() -> StepRegistry:
    """Every draft renders to the same bytes, so only the generation can tell two reviews apart."""
    reg = StepRegistry()

    async def draft(topic: str) -> Draft:
        return Draft(text=f"a draft about {topic}")

    async def render(draft: Draft) -> Render:
        del draft
        return Render(body="the same rendered body every time")

    async def ship(render: Render) -> Shipped:
        return Shipped(where=f"https://ship.example/{len(render.body)}")

    for fn in (draft, render, ship):
        reg.register(fn)
    return reg


class _Scripted:
    """Answers each gate by which reach of it this is, and remembers what it was shown."""

    def __init__(self, script: dict[tuple[str, int], GateDecision]) -> None:
        self.script = script
        self.reaches: dict[str, int] = {}
        self.shown: list[tuple[str, object]] = []

    async def review(self, gate_id: str, value: object, *, note: str = "") -> GateDecision:
        del note
        reach = self.reaches[gate_id] = self.reaches.get(gate_id, 0) + 1
        self.shown.append((gate_id, value))
        return self.script[(gate_id, reach)]


def _approve() -> GateDecision:
    return GateDecision(approve=True, source="person")


def _reject(words: str) -> GateDecision:
    return GateDecision(approve=False, feedback=words, requested_repair=words, source="person")


async def _ran(spec: Workflow, approval: _Scripted) -> WorkflowState:
    state = WorkflowState(params={"topic": "quarter three"})
    await build_graph(spec, _render_registry(), approval=approval).run(state=state)
    return state


def _ended_from(spec: Workflow, state: WorkflowState, *, status: Literal["completed", "failed"], exhausted: object = None) -> EndedRun:
    return EndedRun(
        spec=spec,
        run_id="run-1",
        status=status,
        values=state.values,
        params={"topic": "quarter three"},
        work_facts=list(state.work_facts.values()),
        batches=state.batches,
        gate_decisions=state.gate_decisions,
        gate_generations=state.gate_generations,
        exhausted=exhausted if isinstance(exhausted, RetryFeedback) else None,
    )


async def test_the_check_is_put_over_the_generation_the_gate_is_actually_on_not_the_one_it_approved_before() -> None:
    """Approve render A, reject downstream, draft B renders to the same bytes, approve render B.

    Both approvals carry the same digest. Only the second belongs to the reach the run ended on, and
    the check has to be reading that one -- an approval of generation 1 releases nothing for
    generation 2 however identical the bytes are.
    """
    spec = _reviewing_a_render()
    approval = _Scripted(
        {
            ("sign_off", 1): _approve(),
            ("final", 1): _reject("ship it somewhere else"),
            ("sign_off", 2): _approve(),
            ("final", 2): _approve(),
        }
    )
    state = await _ran(spec, approval)

    result = checked_outcome(_ended_from(spec, state, status="completed"))

    rendered = state.values["render"]
    digest = reviewed_value_digest(canonical_reviewed_value(rendered))
    (check,) = result.checks
    assert [gate for gate, _ in approval.shown] == ["sign_off", "final", "sign_off", "final"]
    assert dict(approval.shown)["sign_off"] == rendered, "the gate reviewed the render, not the draft"
    assert state.gate_generations["sign_off"] == 2
    approvals = [fact for fact in state.gate_decisions if fact.gate_id == "sign_off"]
    assert [fact.review_generation for fact in approvals] == [1, 2]
    assert {fact.reviewed_value_digest for fact in approvals} == {digest}, "identical bytes across both generations"
    assert check.passed is True
    assert check.review_generation == 2, "the reach the run ended on, not the one that was approved first"
    assert check.reviewed_value_digest == digest
    assert check.reviewed is not None
    assert (check.reviewed.node_id, check.reviewed.turn) == ("render", None), "the check resolves the gate's own reviews wire"
    assert result.status == "succeeded"


async def test_an_earlier_approval_of_identical_bytes_does_not_satisfy_a_later_reach_that_refused() -> None:
    """Approve render A, reject downstream, draft B renders to the same bytes, refuse render B.

    The refusal spends the gate's last reach, so the run ends holding it. The check names generation
    2 and those bytes, and the generation-1 approval of the very same bytes is on file and unused.
    """
    spec = _reviewing_a_render(sign_off_attempts=2)
    approval = _Scripted(
        {
            ("sign_off", 1): _approve(),
            ("final", 1): _reject("ship it somewhere else"),
            ("sign_off", 2): _reject("the body still says nothing"),
        }
    )
    state = WorkflowState(params={"topic": "quarter three"})
    with pytest.raises(RetryExhaustedError) as refusal:
        await build_graph(spec, _render_registry(), approval=approval).run(state=state)

    result = checked_outcome(_ended_from(spec, state, status="failed", exhausted=refusal.value.feedback))

    digest = reviewed_value_digest(canonical_reviewed_value(state.values["render"]))
    (check,) = result.checks
    approved, refused = [fact for fact in state.gate_decisions if fact.gate_id == "sign_off"]
    assert (approved.verdict, approved.review_generation, approved.reviewed_value_digest) == ("approve", 1, digest)
    assert (refused.verdict, refused.review_generation, refused.reviewed_value_digest) == ("reject", 2, digest)
    assert check.passed is False, "the approval of generation 1 released nothing for generation 2"
    assert check.review_generation == 2
    assert check.reviewed_value_digest == digest
    assert check.reviewed is not None
    assert check.reviewed.node_id == "render"
    assert check.observed_source == "person"
    assert check.feedback == "the body still says nothing"
    assert check.requested_repair == "the body still says nothing"
    assert result.status == "failed"


class _Unanswerable(_Scripted):
    """Scripted, except that a reach with no line in the script is one nobody could answer."""

    async def review(self, gate_id: str, value: object, *, note: str = "") -> GateDecision:
        reach = self.reaches.get(gate_id, 0) + 1
        if (gate_id, reach) not in self.script:
            self.reaches[gate_id] = reach
            self.shown.append((gate_id, value))
            msg = f"nobody could be asked to review {gate_id!r}"
            raise RuntimeError(msg)
        return await super().review(gate_id, value, note=note)


async def test_a_reach_nobody_decided_is_not_satisfied_by_the_approval_the_reach_before_it_got() -> None:
    """Approve render A, reject downstream, draft B renders to the same bytes, and nobody answers B.

    The gate is on generation 2 and only generation 1 was ever decided, over byte-identical bytes.
    Reading the newest decision for the gate would call this approved. Reading the newest decision
    *of the reach the run ended on* calls it undecided, which is what it is.
    """
    spec = _reviewing_a_render()
    approval = _Unanswerable({("sign_off", 1): _approve(), ("final", 1): _reject("ship it somewhere else")})
    state = WorkflowState(params={"topic": "quarter three"})
    with pytest.raises(RuntimeError, match="nobody could be asked"):
        await build_graph(spec, _render_registry(), approval=approval).run(state=state)

    result = checked_outcome(_ended_from(spec, state, status="failed"))

    digest = reviewed_value_digest(canonical_reviewed_value(state.values["render"]))
    (check,) = result.checks
    (approved,) = [fact for fact in state.gate_decisions if fact.gate_id == "sign_off"]
    assert (approved.verdict, approved.review_generation, approved.reviewed_value_digest) == ("approve", 1, digest)
    assert state.gate_generations["sign_off"] == 2, "the gate was reached again over a second generation of the same bytes"
    assert check.passed is False
    assert check.observed_source == "unknown", "nothing decided the reach this run ended on"
    assert check.review_generation is None
    assert check.reviewed is not None
    assert (check.reviewed.node_id, check.reviewed.value_digest) == ("render", digest)


# --- a run's own title, kept apart from the session that hosted it -----------------------------


def test_an_archived_run_carries_the_title_its_launch_gave_it(tmp_path: Path) -> None:
    """The run is named by whoever started it, and the session it ran in stays a separate fact."""
    records = records_under(tmp_path)
    session = _session(tmp_path)
    session.session_id = "sess-1"
    session.active.workflow_id = "wf.report"
    session.active.run_title = "Which cache do we use?"
    session.active.started_at = datetime.now(UTC).isoformat()

    archive_run(
        session,
        archive=records.archive,
        outcome="completed",
        failure="",
        runner="app:harness",
        spec=_spec(),
        params={"quarter": "Q2"},
        values={},
        conversations={},
        gates_approved=(),
        gates_rejected={},
        gates_auto=(),
    )

    (listed,) = records.archive.recent(10)
    record = records.archive.get(listed.run_id)
    assert record is not None
    assert record.run_title == "Which cache do we use?"
    assert record.session_id == "sess-1", "which container it ran in is a different fact from what the run is called"
    assert record.workflow_id == "wf.report"


def test_a_run_title_survives_the_reload_a_listing_read_does(tmp_path: Path) -> None:
    archive = FileRunArchive(tmp_path, retention_days=KEPT_DAYS)
    archive.put(_record(run_id="run-1", run_title="Which cache do we use?"))

    whole = archive.get("run-1")
    assert whole is not None
    assert whole.run_title == "Which cache do we use?"
    assert [item.run_title for item in archive.recent(10)] == ["Which cache do we use?"]
    assert [item.run_title for item in archive.list_for("wf.report", limit=10)] == ["Which cache do we use?"]


def test_a_record_written_before_runs_had_titles_reads_as_untitled(tmp_path: Path) -> None:
    """An older record carries no title at all, and reads as none rather than refusing to parse."""
    written = json.loads(_record(run_id="legacy").model_dump_json())
    del written["run_title"]
    folder = tmp_path / "wf.report"
    folder.mkdir(parents=True)
    (folder / "legacy.json").write_text(json.dumps(written), encoding="utf-8")

    record = FileRunArchive(tmp_path, retention_days=KEPT_DAYS).get("legacy")

    assert record is not None
    assert record.run_title == "", "nothing invents a name for a run that was never given one"


def test_put_stages_the_record_under_a_hidden_temp_name(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A listing or a glob that skips dotfiles never sees a record still being written."""
    staged = staged_names(monkeypatch)

    FileRunArchive(tmp_path, retention_days=KEPT_DAYS).put(_record(run_id="run-1"))

    assert staged, "the record is swapped into place"
    assert all(name.startswith(".") for name in staged)
