"""What a gate answers with: the verdict, the exact words, and who actually decided.

A rejection with no comment reruns exactly as it always has -- that is the existing gate test in
`test_evidence.py`, kept green rather than duplicated here. A rejection with one hands the words
back to the graph, which files them under the node the rejection reruns; nothing here keeps a
slot for them. Automode is the third plugin into a gate -- a run's own choice to cross every gate
on the record, with nobody asked, and the record must say so without ever counting it as a human
approval. The deadline is the fourth, and it decides nothing either.
"""

from __future__ import annotations

import asyncio
import json

from contextlib import asynccontextmanager
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import TYPE_CHECKING, Any, cast

import pytest

from core.ai.workflows.contracts import (
    CURRENT_STEP,
    Briefing,
    FromNode,
    FromParameter,
    GateDecision,
    ParameterSpec,
    PersonGate,
    RetryExhaustedError,
    RetryFeedback,
    RunEvidence,
    RunNode,
    Shape,
    StepRegistry,
    Workflow,
    build_graph,
)

from harness.features.running import service
from harness.features.running.api_models import RunRequest
from harness.features.running.approval import ChatApproval
from harness.features.running.domain.progress import GateVerdict
from harness.features.running.infrastructure.run_log import RunLog, load_waiting, reviewed_values
from harness.session import NoSessionLog, Runtime, SessionPool, run_log_path
from harness.settings import RunSettings
from tests.support.controllers import a_client_of
from tests.support.fakes.broadcaster import FakeBroadcaster
from tests.support.running import a_pool, a_running_controller
from tests.support.runtime import EmptyLibrary, a_runtime
from tests.support.staging import staged_names
from tests.support.state import a_state, records_under

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator

    import httpx



def _session(registry: StepRegistry, tmp_path: Path) -> Runtime:
    return Runtime(registry=registry, library=cast("Any", None), agent=cast("Any", None), session_log=NoSessionLog(), state=a_state(tmp_path))


async def _resolve(session: Runtime, verdict: GateVerdict) -> None:
    """See the gate out and settle it -- the future the person's click resolves.

    A settled future is skipped rather than answered twice: a gate reached again after its own
    rejection leaves the last answer in place until it re-parks with a fresh one.
    """
    for _ in range(500):
        decision = session.active.decision
        if decision is not None and not decision.done():
            decision.set_result(verdict)
            return
        await asyncio.sleep(0.001)
    msg = "the gate never waited"
    raise AssertionError(msg)


def _spec_with_brought_parent() -> Workflow:
    """A gate whose rejection target is a step the artifact brought, not a registered step."""
    return Workflow(
        name="feedback-probe",
        version=1,
        description="A gate rejecting back into a step the artifact brought with it.",
        inline=(
            Briefing(
                name="draft_task",
                instructions="Draft a short summary of the reconciled ledger, ready for a person to approve or send back.",
                output=Shape(json_schema={"type": "string"}),
            ),
        ),
        nodes=(
            RunNode(id="draft", work="draft_task"),
            PersonGate(id="approve", reviews=FromNode(node="draft"), rejected_to="draft"),
        ),
        output=FromNode(node="approve"),
    )


async def test_approve_ignores_feedback(quarterly_report: Workflow, registry: StepRegistry, tmp_path: Path) -> None:
    """A stray remark on an approve must never turn into a rerun nobody asked for."""
    session = _session(registry, tmp_path)
    approval = ChatApproval(session=session, workflow="wf.gated", spec=quarterly_report)
    waiting = asyncio.create_task(approval.review("approve", "the draft"))
    await _resolve(session, GateVerdict(approve=True, feedback="not thrilled but ship it"))
    assert (await waiting).approve is True


async def test_bare_reject_reruns_exactly_as_before(quarterly_report: Workflow, registry: StepRegistry, tmp_path: Path) -> None:
    """No feedback, no routing -- today's semantics, unchanged."""
    session = _session(registry, tmp_path)
    approval = ChatApproval(session=session, workflow="wf.gated", spec=quarterly_report)
    waiting = asyncio.create_task(approval.review("approve", "the draft"))
    await _resolve(session, GateVerdict(approve=False))
    assert (await waiting).approve is False


async def test_a_rejection_answers_with_the_exact_words_and_who_said_them(tmp_path: Path) -> None:
    """The graph files the repair under the node it reruns, carrying the words and who said them."""
    session = _session(StepRegistry(), tmp_path)
    approval = ChatApproval(session=session, workflow="wf.probe", spec=_spec_with_brought_parent())

    waiting = asyncio.create_task(approval.review("approve", "the draft"))
    await _resolve(session, GateVerdict(approve=False, feedback="tighten the second paragraph"))

    decision = await waiting
    assert decision.approve is False
    assert decision.feedback == "tighten the second paragraph"
    assert decision.source == "person"
    assert approval.rejections() == {"approve": 1}


async def test_a_gate_nobody_answered_is_refused_by_the_deadline_and_says_so(tmp_path: Path) -> None:
    """Every unanswered wait is a refusal of its own, and never a person's word.

    How many such refusals a run survives is the gate's own ceiling, so a deployment with no way
    to answer a gate spends that gate's reaches and stops rather than waiting for ever.
    """
    session = _session(StepRegistry(), tmp_path)
    approval = ChatApproval(session=session, workflow="wf.probe", spec=_spec_with_brought_parent(), timeout_seconds=0.001)

    decisions = [await approval.review("approve", "draft A") for _ in range(3)]

    assert [decision.source for decision in decisions] == ["timeout"] * 3
    assert not any(decision.approve for decision in decisions)
    assert decisions[0].feedback == "Nobody decided this gate before it timed out."
    assert approval.rejections() == {"approve": 3}


async def test_automode_crosses_the_gate_with_no_decision_future(quarterly_report: Workflow, registry: StepRegistry, tmp_path: Path) -> None:
    """Automode answers `review` itself: nothing waits, and the gate's own count says so."""
    session = _session(registry, tmp_path)
    session.active.auto_approve = True
    approval = ChatApproval(session=session, workflow="wf.gated", spec=quarterly_report)
    assert (await approval.review("approve", "the draft")).source == "auto"
    assert session.active.decision is None
    assert session.active.pending is None
    assert approval.auto_gates() == ("approve",)
    assert approval.rejections() == {}


async def test_an_automode_run_attests_the_gate_as_auto_not_approved(quarterly_report: Workflow, registry: StepRegistry, tmp_path: Path) -> None:
    """End to end through the launch path: the gate crosses unattended, and the ledger says how."""
    records = records_under(tmp_path)
    session = _session(registry, tmp_path)
    session.session_id = "sess-automode"
    service.launch(session, records=records, topics=FakeBroadcaster(), spec=quarterly_report, workflow_id="wf.automode", params={"quarter": "Q2"}, auto_approve_gates=True)
    task = session.active.task
    assert task is not None
    await task

    assert session.active.pending is None, "automode never holds the gate for a person"
    events = await records.evidence.events_for("wf.automode")
    attested = [event for event in events if isinstance(event, RunEvidence)]
    assert [event.outcome for event in attested] == ["completed"]
    assert attested[0].gates_approved == (), "an auto-passed gate must not read as human-approved"
    assert attested[0].gates_auto == ("approve",)

    archived = records.archive.recent(10)
    record = next(r for r in archived if r.workflow_id == "wf.automode")
    assert record.gates_auto == ("approve",)
    assert record.gates_approved == ()


@asynccontextmanager
async def _pool_over(registry: StepRegistry, *, run: RunSettings | None = None) -> AsyncGenerator[tuple[httpx.AsyncClient, SessionPool]]:
    """The running controller over ASGI, with the pool its routes act on."""
    pool = a_pool(registry)
    with TemporaryDirectory() as state:
        async with a_client_of(a_running_controller(pool, Path(state), run=run)) as client:
            yield client, pool


def _held_open_spec(hold: asyncio.Event) -> tuple[Workflow, StepRegistry]:
    """One step waiting on `hold`, so a test can flip automode while a run is genuinely running."""
    registry = StepRegistry()

    async def slow(quarter: str) -> str:
        await hold.wait()
        return quarter

    registry.register(slow)
    spec = Workflow(
        name="held-open",
        version=1,
        description="One step held open on an event, so a test can act while it is still running.",
        parameters={"quarter": ParameterSpec(description="Fiscal quarter", json_schema={"type": "string"})},
        nodes=(RunNode(id="slow", work="slow", inputs={"quarter": FromParameter(parameter="quarter")}),),
        output=FromNode(node="slow"),
    )
    return spec, registry


async def test_the_automode_endpoint_flips_mid_run_and_refuses_when_idle(tmp_path: Path) -> None:
    records = records_under(tmp_path)
    hold = asyncio.Event()
    spec, registry = _held_open_spec(hold)

    async with _pool_over(registry) as (client, pool):
        idle = await client.post("/api/run/automode", json={"on": True})
        assert idle.status_code == 409
        assert idle.json() == {"ok": False, "message": "nothing is running"}

        service.launch(pool.default, records=records, topics=FakeBroadcaster(), spec=spec, workflow_id="wf.held", params={"quarter": "Q2"})
        for _ in range(500):
            if pool.default.active.progress.running:
                break
            await asyncio.sleep(0.01)
        else:
            pytest.fail("the run never started")

        before = await client.get("/api/run/state")
        assert before.json()["auto_approve"] is False

        flipped = await client.post("/api/run/automode", json={"on": True})
        assert flipped.status_code == 200
        assert flipped.json() == {"ok": True, "auto_approve": True}
        assert pool.default.active.auto_approve is True

        state = await client.get("/api/run/state")
        assert state.json()["auto_approve"] is True, "the poll must see the same flag the flip just set"

        hold.set()
        task = pool.default.active.task
        assert task is not None
        await task


class _ReportStore(EmptyLibrary):
    """Answers `fetch_workflow` for the one workflow id `_start` looks up before it dispatches."""

    def __init__(self, spec: Workflow) -> None:
        super().__init__()
        self._spec = spec

    async def fetch_workflow(self, workflow_id: str) -> Workflow:
        """The one spec this store holds, whichever id is asked for."""
        del workflow_id
        return self._spec


async def test_an_unreachable_temporal_engine_refuses_a_start_in_words(quarterly_report: Workflow, registry: StepRegistry, tmp_path: Path) -> None:
    """A 409 naming why, not the generic 500 a bare exception would answer with."""
    service.install_engine_reachable(reachable=False)
    try:
        pool = SessionPool(a_runtime(registry=registry, library=_ReportStore(quarterly_report)))
        controller = a_running_controller(pool, tmp_path, run=RunSettings(run_engine="temporal"))

        response = await controller.start_run(RunRequest(workflow_id="wf.report", arguments={"quarter": "Q2"}))

        assert response.status_code == 409
        assert "Temporal" in json.loads(bytes(response.body))["message"]
    finally:
        service.install_engine_reachable(reachable=True)


async def test_automode_is_refused_on_the_durable_engine(registry: StepRegistry) -> None:
    """The gate mode is fixed when a durable run starts, so flipping it here would only mislead the panel."""
    async with _pool_over(registry, run=RunSettings(run_engine="temporal")) as (client, _pool):
        response = await client.post("/api/run/automode", json={"on": True})
        assert response.status_code == 409
        assert "Temporal" in response.json()["message"]


# --- what the refused value is kept in --------------------------------------------------------


def test_the_value_a_gate_refused_outlives_every_newer_value_the_run_produced(tmp_path: Path) -> None:
    """The repair reads the bytes the decider saw, whatever the run produced after them.

    Kept beside the run log rather than inside it, so a process that dies mid-run still has them
    when it comes back, and copied into the archive record before the log closes.
    """
    path = tmp_path / "waiting.json"
    log = RunLog.begin(path=path, run_id="run-one", workflow_id="wf.gated", spec=_spec_with_brought_parent(), params={})
    refused = log.state.reviewed_artifacts.persist("draft", None, {"draft": "A"})
    log.state.reviewed_artifacts.persist("draft", None, {"draft": "B"})

    reopened = reviewed_values(path, refused.run_id)

    assert reopened.read(refused) == {"draft": "A"}
    with pytest.raises(ValueError, match="not recorded"):
        reopened.read(refused.model_copy(update={"node_id": "another_node"}))
    with pytest.raises(ValueError, match="another run"):
        reopened.read(refused.model_copy(update={"run_id": "another-run"}))


def test_a_run_refuses_to_read_a_value_another_run_was_refused_over(tmp_path: Path) -> None:
    """Two runs of one session share a directory and nothing else, so neither answers over the other's evidence."""
    path = tmp_path / "waiting.json"
    spec = _spec_with_brought_parent()

    first = RunLog.begin(path=path, run_id="run-one", workflow_id="wf.gated", spec=spec, params={})
    refused = first.state.reviewed_artifacts.persist("draft", None, {"draft": "A"})
    second = RunLog.begin(path=path, run_id="run-two", workflow_id="wf.gated", spec=spec, params={})

    assert refused.run_id == "run-one"
    with pytest.raises(ValueError, match="another run"):
        second.state.reviewed_artifacts.read(refused)


async def test_a_spent_gate_ceiling_ends_the_run_holding_the_refused_value(tmp_path: Path) -> None:
    """The run stops at the ceiling, and what it stops holding is the whole rejection."""

    async def draft_task() -> str:
        return "draft A"

    class RefuseEverything:
        """A reviewer who never releases anything, so every reach of the gate is spent."""

        async def review(self, gate_id: str, value: object, *, note: str = "") -> GateDecision:
            del gate_id, value, note
            return GateDecision(approve=False, feedback="cite the constraint")

    registry = StepRegistry()
    registry.register(draft_task)
    spec = _spec_with_brought_parent().model_copy(update={"inline": ()})
    log = RunLog.begin(path=tmp_path / "waiting.json", run_id="run-one", workflow_id="wf.gated", spec=spec, params={})

    with pytest.raises(RetryExhaustedError) as raised:
        await build_graph(spec, registry, approval=RefuseEverything(), observer=log).run(state=log.state)

    error = raised.value
    assert (error.gate_id, error.target_node, error.attempts) == ("approve", "draft", 3)
    assert error.feedback == log.state.retry_feedback["draft"][-1]
    assert log.state.reviewed_artifacts.read(error.feedback.reviewed) == "draft A"
    waiting = load_waiting(log.path)
    assert waiting is not None
    assert waiting.run_id == log.state.reviewed_artifacts.run_id
    assert [fact.verdict for fact in waiting.gate_decisions] == ["reject", "reject"], "the log holds every decision snapshotted before the ending"


async def test_a_refused_value_stays_reachable_from_the_run_id_the_archive_kept(tmp_path: Path) -> None:
    """The reviewed value is addressed under the run the launcher started, so an ended run can still name it.

    The run log is deleted the moment the run ends, so the only handles left are the ones the
    archive and the ledger kept. A value addressed under an id of the log's own invention would
    be unreachable from any of them.
    """
    records = records_under(tmp_path)
    refused: list[RetryFeedback] = []

    async def draft() -> str:
        identity = CURRENT_STEP.get()
        assert identity is not None
        refused.extend(identity.retry_feedback)
        return "draft A"

    registry = StepRegistry()
    registry.register(draft)
    spec = Workflow(
        name="repair-and-archive",
        version=1,
        description="One draft behind one gate, refused once and then released.",
        nodes=(RunNode(id="draft", work="draft"), PersonGate(id="approve", reviews=FromNode(node="draft"), rejected_to="draft")),
        output=FromNode(node="approve"),
    )
    session = _session(registry, tmp_path)
    session.session_id = "sess-repair"

    service.launch(session, records=records, topics=FakeBroadcaster(), spec=spec, workflow_id="wf.repair", params={})
    task = session.active.task
    assert task is not None
    await _resolve(session, GateVerdict(approve=False, feedback="cite the constraint"))
    await _resolve(session, GateVerdict(approve=True))
    await task

    archived = next(record for record in records.archive.recent(10) if record.workflow_id == "wf.repair")
    assert len(refused) == 1, "the rerun read the one rejection it owed"
    assert refused[0].reviewed.run_id == archived.run_id, "the value the gate refused belongs to the run that was archived"
    assert not run_log_path(session).exists(), "the run ended, so the log that knew the id is gone"
    assert not reviewed_values(run_log_path(session), archived.run_id).captured(), "and so are the copies that sat beside it"
    whole = records.archive.get(archived.run_id)  # a listing row leaves the reviewed copies out; the record carries them
    assert whole is not None
    kept = next(value for value in whole.reviewed if value.ref == refused[0].reviewed)
    assert json.loads(kept.canonical) == "draft A", "the refused value is readable from the archived record alone"


def test_the_run_log_stages_each_snapshot_under_a_hidden_temp_name(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A listing or a glob that skips dotfiles never sees a run log still being written."""
    staged = staged_names(monkeypatch)

    log = RunLog.begin(path=tmp_path / "waiting.json", run_id="run-one", workflow_id="wf.gated", spec=_spec_with_brought_parent(), params={})
    log.started("draft")

    assert staged, "the run log is swapped into place"
    assert all(name.startswith(".") for name in staged)
