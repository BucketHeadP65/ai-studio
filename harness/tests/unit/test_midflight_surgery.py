"""Mid-flight graph surgery: pause a run, edit the graph, resume -- and the untouched prefix replays.

Three rulings are under test, and the first is what makes the other two possible at all.

**A pause is not an ending.** `service.launch`'s runner writes an `RunEvidence` and closes the
run log in a `finally`, and `asyncio.CancelledError` is a `BaseException`: it walks straight past
`except Exception` and into that `finally`. So a naive `task.cancel()` would file a *failed*
evidence record for a run that did not fail, and delete the run log that is the entire point of
pausing. That poisons the evidence, which is the trust level's cardinal sin. The runner therefore
catches the cancellation, marks the ending as paused and re-raises, so the evidence record and the
close are for real endings only. What a pause steps behind is exactly what a process death
steps behind: a run log
on disk, and nothing in the ledger.

**A memo's trust is node-granular, withdrawn by the same derivation that withdraws trust at save
time.** A resumed run replays a logged value only where the node that produced it is still the
same node -- `moved_nodes`, which compares wiring *and* briefing. There is no second wiring
comparison anywhere that could disagree with the first.

**Nothing is paused or resumed silently.** A pause with no run to pause answers `False` and writes
nothing at all; a resume with no run log on disk answers `None` and starts nothing.
"""

from __future__ import annotations

import asyncio
import json

from typing import TYPE_CHECKING, Any, cast

import yaml

from core.ai.workflows.contracts import (
    CURRENT_STEP,
    Briefing,
    FromNode,
    LibraryError,
    Node,
    PersonGate,
    RunNode,
    Shape,
    StepRegistry,
    Workflow,
    moved_nodes,
)

from harness.composition import restore_waiting_run
from harness.features.authoring.domain import Draft
from harness.features.running import controller, service
from harness.features.running.domain.progress import GateVerdict
from harness.features.running.infrastructure.run_log import load_waiting
from harness.session import NoSessionLog, Runtime
from harness.session import run_log_path as session_run_log_path
from harness.settings import RunSettings
from tests.conftest import CallLog, Ledger, ReportDraft, register_quarterly_steps
from tests.support.fakes.broadcaster import FakeBroadcaster
from tests.support.state import a_state, records_under

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path

    from harness.features.running.records import RunRecords

PREFIX = {"fetch_sales", "fetch_costs", "reconcile"}
"""What the report's run log holds by the time the run reaches its gate."""


def _session(registry: StepRegistry, tmp_path: Path, *, session_kind: str = "chat") -> Runtime:
    """A session with only the half a run needs: the store and the agent are the authoring slice's."""
    return Runtime(
        registry=registry,
        library=cast("Any", None),
        agent=cast("Any", None),
        session_log=NoSessionLog(),
        state=a_state(tmp_path),
        session_kind=session_kind,
    )


class _StubStore:
    """A store that answers `fetch_workflow` for one workflow id and refuses every other one."""

    def __init__(self, spec_by_id: dict[str, Workflow]) -> None:
        self._spec_by_id = spec_by_id

    async def fetch_workflow(self, workflow_id: str) -> Workflow:
        spec = self._spec_by_id.get(workflow_id)
        if spec is None:
            msg = f"no artifact {workflow_id!r}"
            raise LibraryError(msg)
        return spec


def _session_with_store(registry: StepRegistry, spec_by_id: dict[str, Workflow], tmp_path: Path) -> Runtime:
    """A session whose store can answer `fetch_workflow`, for the fallback a bodyless resume takes."""
    return Runtime(registry=registry, library=cast("Any", _StubStore(spec_by_id)), agent=cast("Any", None), session_log=NoSessionLog(), state=a_state(tmp_path))


async def _until(predicate: Callable[[], bool], *, what: str) -> None:
    """Poll for a background run to reach a state, the way the suite's other launch tests do."""
    for _ in range(500):
        if predicate():
            return
        await asyncio.sleep(0.01)
    raise AssertionError(what)


async def _waiting(session: Runtime) -> asyncio.Future[GateVerdict]:
    """The gate's future, once the run has actually stopped at it."""
    await _until(lambda: session.active.decision is not None, what="the run never waiting at its gate")
    decision = session.active.decision
    assert decision is not None
    return decision


async def _finish(session: Runtime) -> None:
    """Approve the waiting gate and let the run end, so no task outlives the test."""
    (await _waiting(session)).set_result(GateVerdict(approve=True))
    task = session.active.task
    assert task is not None
    await task


def _redrafted(spec: Workflow) -> Workflow:
    """The same graph with one node's work swapped: exactly one node moves, and it is `draft_report`."""
    return spec.model_copy(
        update={
            "nodes": tuple(
                RunNode(id="draft_report", work="draft_harder", inputs={"ledger": FromNode(node="reconcile")})
                if node.id == "draft_report"
                else node
                for node in spec.nodes
            )
        },
        deep=True,
    )


def _register_harder(registry: StepRegistry, call_log: CallLog) -> None:
    """The replacement step the edited graph names. Records itself, so a rerun is visible."""

    async def draft_harder(ledger: Ledger) -> ReportDraft:
        call_log.calls.append("draft_harder")
        return ReportDraft(text=f"net: {ledger.net} (revised)")

    registry.register(draft_harder)


async def test_pausing_mid_node_keeps_the_journal_and_attests_nothing(quarterly_report: Workflow, call_log: CallLog, tmp_path: Path) -> None:
    """The cardinal case: a cancelled run must not look like a failed one, anywhere.

    Paused with `draft_report` genuinely in flight, so the cancellation lands inside a step
    rather than inside the gate's `wait_for` -- the arm that reaches the runner's `finally`.
    """
    records = records_under(tmp_path)
    held = asyncio.Event()  # never set: the pause arrives with this node still running
    registry = StepRegistry()
    register_quarterly_steps(registry, call_log, exclude=frozenset({"draft_report"}))

    async def draft_report(ledger: Ledger) -> ReportDraft:
        call_log.calls.append("draft_report")
        await held.wait()
        return ReportDraft(text="never reached")

    registry.register(draft_report)
    session = _session(registry, tmp_path)
    service.launch(session, records=records, topics=FakeBroadcaster(), spec=quarterly_report, workflow_id="wf.surgery", params={"quarter": "Q2"})
    await _until(lambda: "draft_report" in call_log.calls, what="the run never reached the node it was to be paused inside")

    assert await service.pause(session) is True

    waiting = load_waiting(session_run_log_path(session))
    assert waiting is not None, "a pause must leave the run log exactly as a process death steps it"
    assert set(waiting.values) == PREFIX, "the completed prefix is what a resume replays"
    assert not (tmp_path / "evidence.jsonl").exists(), "a pause is not an ending: nothing may be attested"
    assert "error" not in session.active.result.lower(), "nothing failed, so nothing may say it did"
    assert "failed" not in session.active.result.lower()


async def test_pausing_a_run_closes_every_open_lines_stream(quarterly_report: Workflow, call_log: CallLog, tmp_path: Path) -> None:
    """A pause ends the wire at once: a viewer tailing a step's stream is not left waiting on a resume that may never come."""
    records = records_under(tmp_path)
    held = asyncio.Event()  # never set: the pause arrives with this node still running
    registry = StepRegistry()
    register_quarterly_steps(registry, call_log, exclude=frozenset({"draft_report"}))

    async def draft_report(ledger: Ledger) -> ReportDraft:
        call_log.calls.append("draft_report")
        await held.wait()
        return ReportDraft(text="never reached")

    registry.register(draft_report)
    session = _session(registry, tmp_path)
    service.launch(session, records=records, topics=FakeBroadcaster(), spec=quarterly_report, workflow_id="wf.surgery", params={"quarter": "Q2"})
    await _until(lambda: "draft_report" in call_log.calls, what="the run never reached the node it was to be paused inside")
    probe_task = Briefing(
        name="probe",
        instructions="A probe line opened only to prove a pause closes its stream.",
        output=Shape(json_schema={"type": "string"}),
    )
    probe = session.active.conversations.open(("probe", ""), step=probe_task, output_type=str, instructions="", monitor=None)
    assert not probe.stream.closed

    assert await service.pause(session) is True

    assert probe.stream.closed, "a pause closes every open line's stream, not just the ones a later resume happens to clear"


async def test_resuming_with_an_edited_graph_reruns_what_moved_and_replays_the_rest(quarterly_report: Workflow, registry: StepRegistry, call_log: CallLog, tmp_path: Path) -> None:
    """The frontier feature, end to end: wait, pause, change one node, resume.

    The prefix replays without executing -- the observer records it as settled with no start --
    and the one node the diff moved runs again, under its new implementation.
    """
    records = records_under(tmp_path)
    _register_harder(registry, call_log)
    session = _session(registry, tmp_path)
    service.launch(session, records=records, topics=FakeBroadcaster(), spec=quarterly_report, workflow_id="wf.surgery", params={"quarter": "Q2"})
    await _waiting(session)
    assert await service.pause(session) is True
    call_log.calls.clear()

    edited = _redrafted(quarterly_report)
    assert moved_nodes(quarterly_report, edited) == ("draft_report",), "the derivation the memo filter reuses"

    resumed = service.resume_with(session, records=records, topics=FakeBroadcaster(), spec=edited)
    assert resumed is not None
    assert set(resumed.replayed) == PREFIX
    assert resumed.withdrawn == ("draft_report",), "the moved node's recorded value is not this node's value"

    await _finish(session)

    assert call_log.calls == ["draft_harder", "publish"], "exactly the touched node re-ran; the prefix did not"
    started = {event["node"] for event in session.active.progress.events if not event["finished"]}
    settled = {event["node"] for event in session.active.progress.events if event["finished"]}
    assert settled - started >= PREFIX, "a replayed node is settled without ever having started"
    assert "draft_report" in started, "the touched node actually executed again"
    assert session.active.result.startswith("Finished."), "the resumed run ends as a run, not as a resumption"


async def test_a_pause_with_nothing_running_answers_false_and_touches_nothing(registry: StepRegistry, tmp_path: Path) -> None:
    """`False` is the answer, and no file is the evidence: a pause of nothing must not invent a waiting run."""
    session = _session(registry, tmp_path)

    assert await service.pause(session) is False
    assert not session_run_log_path(session).exists()
    assert not (tmp_path / "evidence.jsonl").exists()


async def test_resuming_an_unedited_graph_replays_everything_it_recorded(quarterly_report: Workflow, registry: StepRegistry, call_log: CallLog, tmp_path: Path) -> None:
    """The filter withdraws for a reason or not at all: an untouched graph re-runs nothing it had done."""
    records = records_under(tmp_path)
    session = _session(registry, tmp_path)
    service.launch(session, records=records, topics=FakeBroadcaster(), spec=quarterly_report, workflow_id="wf.surgery", params={"quarter": "Q2"})
    await _waiting(session)
    assert await service.pause(session) is True
    call_log.calls.clear()

    resumed = service.resume_with(session, records=records, topics=FakeBroadcaster(), spec=quarterly_report)
    assert resumed is not None
    assert set(resumed.replayed) == PREFIX | {"draft_report"}
    assert resumed.withdrawn == ()

    await _finish(session)
    assert call_log.calls == ["publish"], "everything the run log held replayed"


async def test_a_recorded_value_for_a_node_the_new_graph_no_longer_has_is_withdrawn(quarterly_report: Workflow, registry: StepRegistry, call_log: CallLog, tmp_path: Path) -> None:
    """A memo may only ever name nodes the graph being launched actually has. Both filters, one memo.

    A rename is the everyday way to reach that: `draft_report`'s value is logged under an id
    the resumed graph does not contain, and a node called `draft_v2` is not the node that ran.
    """
    records = records_under(tmp_path)
    session = _session(registry, tmp_path)
    service.launch(session, records=records, topics=FakeBroadcaster(), spec=quarterly_report, workflow_id="wf.surgery", params={"quarter": "Q2"})
    await _waiting(session)
    assert await service.pause(session) is True
    call_log.calls.clear()

    nodes: list[Node] = []
    for node in quarterly_report.nodes:
        if node.id == "draft_report":
            nodes.append(RunNode(id="draft_v2", work="draft_report", inputs={"ledger": FromNode(node="reconcile")}))
        elif node.id == "approve":
            nodes.append(node.model_copy(update={"reviews": FromNode(node="draft_v2"), "rejected_to": "draft_v2"}))
        else:
            nodes.append(node)
    renamed = quarterly_report.model_copy(update={"nodes": tuple(nodes)}, deep=True)

    resumed = service.resume_with(session, records=records, topics=FakeBroadcaster(), spec=renamed)
    assert resumed is not None
    assert set(resumed.replayed) == PREFIX, "the untouched prefix still carries"
    assert "draft_report" in resumed.withdrawn, "the recorded value has no node in this graph to land in"

    await _finish(session)
    assert call_log.calls == ["draft_report", "publish"], "the renamed node runs; it is not the node that was logged"


async def test_the_endpoints_pause_and_resume_with_the_settled_draft(quarterly_report: Workflow, registry: StepRegistry, call_log: CallLog, tmp_path: Path) -> None:
    """What the two buttons do: pause with no body, resume against the draft the session settled on.

    The default is deliberately the settled draft rather than the logged spec -- pausing to edit
    the graph is the only reason to pause, and the edited graph is what the draft holds.
    """
    records = records_under(tmp_path)
    _register_harder(registry, call_log)
    session = _session(registry, tmp_path)
    service.launch(session, records=records, topics=FakeBroadcaster(), spec=quarterly_report, workflow_id="wf.surgery", params={"quarter": "Q2"})
    await _waiting(session)

    paused = await controller.run_pause(session, RunSettings())
    assert paused.status_code == 200
    assert json.loads(bytes(paused.body))["ok"] is True
    call_log.calls.clear()

    session.conversation.draft = Draft(spec_yaml=yaml.safe_dump(_redrafted(quarterly_report).model_dump(mode="json")))
    answer = await controller.run_resume(session, None, records, FakeBroadcaster())
    body = json.loads(bytes(answer.body))
    assert answer.status_code == 200, body
    assert sorted(body["replayed"]) == sorted(PREFIX)
    assert body["withdrawn"] == ["draft_report"]

    await _finish(session)
    assert call_log.calls == ["draft_harder", "publish"]


async def test_the_endpoints_refuse_a_pause_of_nothing_and_a_resume_of_nothing(registry: StepRegistry, tmp_path: Path) -> None:
    """Both refusals answer in the running slice's voice: 409 with a sentence, never a silent ok."""
    records = records_under(tmp_path)
    session = _session(registry, tmp_path)

    nothing_to_pause = await controller.run_pause(session, RunSettings())
    assert nothing_to_pause.status_code == 409
    assert json.loads(bytes(nothing_to_pause.body))["ok"] is False

    session.conversation.draft = Draft(spec_yaml=yaml.safe_dump({"name": "x"}))
    unrunnable = await controller.run_resume(session, None, records, FakeBroadcaster())
    assert unrunnable.status_code == 400, "a graph this app cannot run is refused before anything is launched"

    session.conversation.draft = Draft()
    no_graph = await controller.run_resume(session, None, records, FakeBroadcaster())
    assert no_graph.status_code == 400
    assert "settle a draft" in json.loads(bytes(no_graph.body))["message"]


async def test_a_resume_with_no_journal_on_disk_starts_nothing(quarterly_report: Workflow, registry: StepRegistry, tmp_path: Path) -> None:
    """Nothing was paused, so there is nothing to carry over -- and a fresh run is not what was asked for."""
    records = records_under(tmp_path)
    session = _session(registry, tmp_path)
    session.conversation.draft = Draft(spec_yaml=yaml.safe_dump(quarterly_report.model_dump(mode="json")))

    assert service.resume_with(session, records=records, topics=FakeBroadcaster(), spec=quarterly_report) is None
    assert session.active.task is None

    answer = await controller.run_resume(session, None, records, FakeBroadcaster())
    assert answer.status_code == 409
    assert "paused" in json.loads(bytes(answer.body))["message"]


async def test_a_bodyless_resume_falls_back_to_the_waiting_run_own_workflow(quarterly_report: Workflow, registry: StepRegistry, call_log: CallLog, tmp_path: Path) -> None:
    """The Run button and `run_workflow` never settle a draft, so an empty body must still resume.

    Pausing a run launched from a stored workflow id steps the draft empty: the studio's pause-
    edit-resume flow is not what started it. The endpoint falls back to fetching that same
    workflow id from the store, exactly the graph the waiting run logged against.
    """
    records = records_under(tmp_path)
    session = _session_with_store(registry, {"wf.stored": quarterly_report}, tmp_path)
    service.launch(session, records=records, topics=FakeBroadcaster(), spec=quarterly_report, workflow_id="wf.stored", params={"quarter": "Q2"})
    await _waiting(session)
    assert await service.pause(session) is True
    call_log.calls.clear()
    assert session.conversation.draft.spec_yaml == "", "the draft is empty: nothing here was ever settled"

    answer = await controller.run_resume(session, None, records, FakeBroadcaster())
    body = json.loads(bytes(answer.body))
    assert answer.status_code == 200, body
    assert body["running"] == "wf.stored"
    assert sorted(body["replayed"]) == sorted(PREFIX | {"draft_report"})
    assert body["withdrawn"] == []

    await _finish(session)
    assert call_log.calls == ["publish"], "the unedited graph replayed everything it had already logged"


async def test_a_bodyless_resume_with_nothing_waiting_and_no_draft_still_refuses(registry: StepRegistry, tmp_path: Path) -> None:
    """No waiting run means no workflow id to fall back to -- the original refusal still stands."""
    records = records_under(tmp_path)
    session = _session(registry, tmp_path)

    answer = await controller.run_resume(session, None, records, FakeBroadcaster())
    body = json.loads(bytes(answer.body))
    assert answer.status_code == 400
    assert "settle a draft" in body["message"]


async def test_a_bodyless_resume_survives_a_restart_by_reading_the_waiting_file(quarterly_report: Workflow, registry: StepRegistry, call_log: CallLog, tmp_path: Path) -> None:
    """A restart carries no `active.workflow_id` at all -- only its own waiting file does.

    Simulated the way a restart actually differs: the file a pause left on disk stays put, but the
    `Runtime` naming this run is a brand new one with a fresh, empty `ActiveRun`, exactly what a
    rehydrated session carries in place of the one that paused it. Both `Runtime`s here point at
    the same default run log path, since neither overrides `waiting_run_file`.
    """
    records = records_under(tmp_path)
    waiting = _session(registry, tmp_path)
    service.launch(waiting, records=records, topics=FakeBroadcaster(), spec=quarterly_report, workflow_id="wf.stored", params={"quarter": "Q2"})
    await _waiting(waiting)
    assert await service.pause(waiting) is True

    rehydrated = _session_with_store(registry, {"wf.stored": quarterly_report}, tmp_path)
    assert rehydrated.active.workflow_id == "", "a rehydrated runtime starts with no active run of its own"
    call_log.calls.clear()

    answer = await controller.run_resume(rehydrated, None, records, FakeBroadcaster())
    body = json.loads(bytes(answer.body))
    assert answer.status_code == 200, body
    assert body["running"] == "wf.stored"
    assert sorted(body["replayed"]) == sorted(PREFIX | {"draft_report"})
    assert body["withdrawn"] == []

    await _finish(rehydrated)
    assert call_log.calls == ["publish"], "the unedited graph replayed everything it had already logged"


def _repairable_spec(*, max_attempts: int) -> Workflow:
    """One brought step, a person gate that sends it back, and the reach ceiling under test."""
    return Workflow(
        name="repairable-draft",
        version=1,
        description="Draft one line, have a person send it back, and draft it again.",
        inline=(Briefing(name="draft_task", instructions="Draft one line for a person to approve or send back.", output=Shape(json_schema={"type": "string"})),),
        nodes=(
            RunNode(id="draft", work="draft_task"),
            PersonGate(id="approve", reviews=FromNode(node="draft"), rejected_to="draft", max_attempts=max_attempts),
        ),
        output=FromNode(node="approve"),
    )


def _gate_is_open(session: Runtime) -> bool:
    """Whether a gate is waiting on an answer nobody has given yet, rather than on a settled one."""
    decision = session.active.decision
    return decision is not None and not decision.done()


async def _open_gate(session: Runtime) -> asyncio.Future[GateVerdict]:
    """The future of the reach the run is on now, never the settled one a previous reach left behind."""
    await _until(lambda: _gate_is_open(session), what="the run never reached a gate with a decision outstanding")
    decision = session.active.decision
    assert decision is not None
    return decision


class _Reruns:
    """What each run of the brought step was handed: the repairs it owed, and the values behind them."""

    def __init__(self) -> None:
        self.owed: list[tuple[str, ...]] = []
        self.reviewed: list[tuple[object, ...]] = []
        self.hold = asyncio.Event()
        self.hold_on_run = 0

    async def perform(self, task: Briefing, output_type: type, /, **inputs: object) -> object:
        """Record this call's owed repairs, reading every refused value back by its own address."""
        del task, output_type, inputs
        identity = CURRENT_STEP.get()
        assert identity is not None
        reader = identity.reviewed_artifacts
        assert reader is not None
        self.owed.append(tuple(item.feedback for item in identity.retry_feedback))
        self.reviewed.append(tuple(reader.read(item.reviewed) for item in identity.retry_feedback))
        if len(self.owed) == self.hold_on_run:
            await self.hold.wait()
        return f"draft {len(self.owed)}"


async def _paused_owing_a_repair(session: Runtime, reruns: _Reruns, records: RunRecords, spec: Workflow) -> None:
    """Launch, refuse the first reach, and pause while the rerun that owes the repair is in flight."""
    service.launch(session, records=records, topics=FakeBroadcaster(), spec=spec, workflow_id="wf.repair", params={})
    (await _open_gate(session)).set_result(GateVerdict(approve=False, feedback="tighten it"))
    await _until(lambda: len(reruns.owed) == 2, what="the rejection never reran the step that owed the repair")
    assert await service.pause(session) is True


async def test_a_run_paused_owing_a_repair_resumes_still_owing_it(tmp_path: Path) -> None:
    """The repair a pause interrupted is still owed after the resume, in the words and over the value it was refused on.

    A rejection drops the refused value from the run log, so the resumed run reruns the step at
    once. That rerun owes only the interrupted repair, and it reads the exact bytes the person
    refused back through the address that feedback carries. A resume mints a new run id, so
    the second half only holds if the values travel with the repair. The next refusal then adds to
    what is owed.
    """
    records = records_under(tmp_path)
    reruns = _Reruns()
    reruns.hold_on_run = 2
    spec = _repairable_spec(max_attempts=3)
    session = _session(StepRegistry(perform=reruns.perform), tmp_path)
    await _paused_owing_a_repair(session, reruns, records, spec)

    assert service.resume_with(session, records=records, topics=FakeBroadcaster(), spec=spec) is not None

    await _until(lambda: len(reruns.owed) == 3, what="the resumed run never reran the step the refusal had sent it back to")
    assert reruns.owed[2] == ("tighten it",), "the resumed rerun owes the repair the pause interrupted, and only that one"
    assert reruns.reviewed[2] == ("draft 1",), "and can still read the exact value that refusal was made over"

    (await _open_gate(session)).set_result(GateVerdict(approve=False, feedback="and cite it"))
    await _until(lambda: len(reruns.owed) == 4, what="the second refusal never reran the step")
    (await _open_gate(session)).set_result(GateVerdict(approve=True))
    task = session.active.task
    assert task is not None
    await task

    assert reruns.owed[3] == ("tighten it", "and cite it"), "the next rerun owes the interrupted repair as well as the new one"
    assert reruns.reviewed[3] == ("draft 1", "draft 3"), "and reads back the value each refusal was made over"


async def test_a_resumed_run_does_not_get_the_gate_reaches_the_paused_one_spent(tmp_path: Path) -> None:
    """A gate's ceiling is a fact about the run, not about the process: a pause and resume buys no fresh reaches."""
    records = records_under(tmp_path)
    reruns = _Reruns()
    reruns.hold_on_run = 2
    spec = _repairable_spec(max_attempts=2)
    session = _session(StepRegistry(perform=reruns.perform), tmp_path)
    await _paused_owing_a_repair(session, reruns, records, spec)

    assert service.resume_with(session, records=records, topics=FakeBroadcaster(), spec=spec) is not None

    await _until(lambda: len(reruns.owed) == 3, what="the resumed run never reran the step the refusal had sent it back to")
    (await _open_gate(session)).set_result(GateVerdict(approve=False, feedback="still not right"))
    task = session.active.task
    assert task is not None
    await _until(task.done, what="the run never ended: it took a reach the paused run had already spent")

    assert "on reach 2 of 2" in session.active.result, "the second refusal spent the last reach the paused run had left"
    assert len(reruns.owed) == 3, "and no fourth run of the step was started on a reach the run no longer had"


async def test_resuming_takes_the_paused_runs_reviewed_values_with_it(tmp_path: Path) -> None:
    """The paused run's own subtree goes once the resuming run has begun, so nothing accumulates unread."""
    records = records_under(tmp_path)
    reruns = _Reruns()
    reruns.hold_on_run = 2
    spec = _repairable_spec(max_attempts=3)
    session = _session(StepRegistry(perform=reruns.perform), tmp_path)
    await _paused_owing_a_repair(session, reruns, records, spec)
    paused = load_waiting(session_run_log_path(session))
    assert paused is not None
    assert paused.run_id, "the paused run knows which id its reviewed values sit under"
    subtree = session_run_log_path(session).parent / "reviewed" / paused.run_id
    assert subtree.is_dir(), "the paused run captured what its gate reviewed"

    assert service.resume_with(session, records=records, topics=FakeBroadcaster(), spec=spec) is not None
    open_gate = await _open_gate(session)  # the resumed run has reached its own gate, so its log is on disk

    assert not subtree.exists(), "the paused run's subtree is gone once the run that replaced it has begun"
    resumed = load_waiting(session_run_log_path(session))
    assert resumed is not None
    assert resumed.run_id != paused.run_id, "a resume is a new run with a new id"
    assert (session_run_log_path(session).parent / "reviewed" / resumed.run_id).is_dir(), "and the value it still owes a repair over travelled with it"
    open_gate.set_result(GateVerdict(approve=True))
    task = session.active.task
    assert task is not None
    await _until(task.done, what="the resumed run never ended after its gate was approved")


async def test_a_restart_restores_the_waiting_run_still_owing_its_repair(tmp_path: Path) -> None:
    """A process death is not a fresh start for the gate: what the dead run owed and spent survives it.

    The restore mints a new run id, so the repair only survives if the value it was refused over is
    re-filed under that id too -- and the reach the dead run spent only counts if the ceiling reads
    it. Both are asserted off the log the restored run itself writes, and then off the ending its
    last reach produces.
    """
    records = records_under(tmp_path)
    reruns = _Reruns()
    reruns.hold_on_run = 2
    spec = _repairable_spec(max_attempts=2)
    session = _session(StepRegistry(perform=reruns.perform), tmp_path, session_kind="runner")
    await _paused_owing_a_repair(session, reruns, records, spec)
    paused = load_waiting(session_run_log_path(session))
    assert paused is not None
    assert [item.feedback for item in paused.retry_feedback["draft"]] == ["tighten it"], "the log a dead process leaves holds what it owed"
    assert paused.gate_generations == {"approve": 1}, "and the reach its gate had already spent"
    subtree = session_run_log_path(session).parent / "reviewed" / paused.run_id

    restarted = _session(StepRegistry(perform=reruns.perform), tmp_path, session_kind="runner")
    restore_waiting_run(restarted, records, FakeBroadcaster())
    open_gate = await _open_gate(restarted)

    restored = load_waiting(session_run_log_path(restarted))
    assert restored is not None
    assert restored.run_id != paused.run_id, "a restore is a new run with a new id"
    owed = restored.retry_feedback["draft"]
    assert [item.feedback for item in owed] == ["tighten it"], "the restored run still owes the repair the dead one did"
    assert [item.reviewed.run_id for item in owed] == [restored.run_id], "over a value re-filed under the run that restored it"
    assert not subtree.exists(), "and the dead run's own subtree went with it"

    open_gate.set_result(GateVerdict(approve=False, feedback="still not right"))
    task = restarted.active.task
    assert task is not None
    await _until(task.done, what="the restored run never ended: it took a reach the dead run had already spent")

    assert "on reach 2 of 2" in restarted.active.result, "the restart bought the gate no fresh reaches"
