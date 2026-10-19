"""The third gate kind: the host's own orchestrator decides, and the artifact says so.

The point of the verb is what it makes *sayable*. Running unattended was already possible by
flipping a run-level switch that waves every gate through, which works and is worse: the stored
artifact still promises a human, and nothing in the file changes when nobody is asked. An
orchestrator gate states in the artifact that a machine clears it, carries the brief it is cleared
against, and refuses cold on a run that has no orchestrator rather than passing anyway.

What is asserted here is mostly the unglamorous half -- the rejection loop actually rerunning the
parent, a blank brief refused before anything runs, an old evidence record still parsing -- because
that is where a gate nobody is watching goes wrong quietly.
"""

from __future__ import annotations

from dataclasses import dataclass, field

import pytest

from core.ai.workflows.contracts import (
    FromNode,
    FromParameter,
    GateDecision,
    JudgeGate,
    MapNode,
    OrchestratorGate,
    Outcome,
    ParameterSpec,
    PersonGate,
    RunEvidence,
    RunNode,
    SaveRefusedError,
    StepRegistry,
    Workflow,
    WorkflowState,
    approved_gates,
    build_graph,
    load_spec_document,
    orchestrated_gates,
    run_workflow,
    trust_level,
    validate,
    verify_savable,
)
from core.ai.workflows.domain.document import dump_spec_document
from core.ai.workflows.domain.validator import IssueType
from core.ai.workflows.exceptions import WorkflowInvalidError
from tests.unit.workflows.conftest import ApproveAll, CallLog, ReportDraft, ReportUrl, SalesData

CHARGE = "Check the draft states the net figure and names the quarter it covers, and reject anything reading as a placeholder."


@dataclass
class ScriptedOrchestrator:
    """The host's decider, scripted: one verdict per scripted call, then approvals for ever.

    Records what it was handed at every gate, which is the assertion surface for what an
    orchestrator gate actually briefs its decider with.
    """

    verdicts: list[GateDecision] = field(default_factory=list)
    heard: list[tuple[str, str, object]] = field(default_factory=list)

    async def __call__(self, gate_id: str, charge: str, value: object, /) -> GateDecision:
        self.heard.append((gate_id, charge, value))
        if len(self.heard) <= len(self.verdicts):
            return self.verdicts[len(self.heard) - 1]
        return GateDecision(approve=True)


@dataclass
class Recorder:
    """Every start and finish, in order -- what a journal or a progress pane would draw."""

    events: list[tuple[str, str]] = field(default_factory=list)

    def started(self, node_id: str) -> None:
        self.events.append(("start", node_id))

    def finished(self, node_id: str, *, failed: bool = False) -> None:
        self.events.append(("fail" if failed else "finish", node_id))


@pytest.fixture
def orchestrator() -> ScriptedOrchestrator:
    return ScriptedOrchestrator()


@pytest.fixture
def deciding(registry: StepRegistry, orchestrator: ScriptedOrchestrator) -> StepRegistry:
    """The quarterly-report steps, on a run that has an orchestrator to ask."""
    registry.decide_gate = orchestrator
    return registry


def _orchestrated(quarterly_report: Workflow, **overrides: object) -> Workflow:
    """The quarterly report with its person gate replaced by an orchestrator gate, same id and wiring."""
    node: dict[str, object] = {
        "kind": "orchestrator-gate",
        "id": "approve",
        "charge": CHARGE,
        "reviews": {"node": "draft_report"},
        "rejected_to": "draft_report",
    }
    node.update(overrides)
    return quarterly_report.model_copy(
        update={"nodes": (*quarterly_report.nodes[:4], OrchestratorGate.model_validate(node), quarterly_report.nodes[5])}
    )


# --- the grammar -------------------------------------------------------------------------


def test_the_new_kind_round_trips_through_a_document(quarterly_report: Workflow) -> None:
    """A saved workflow says which gate kind it holds, and reads back saying the same thing."""
    spec = _orchestrated(quarterly_report)

    reloaded = load_spec_document(dump_spec_document(spec)).workflow

    gate = reloaded.nodes[4]
    assert isinstance(gate, OrchestratorGate)
    assert gate.kind == "orchestrator-gate"
    assert gate.charge == CHARGE
    assert gate.reviews == FromNode(node="draft_report")
    assert gate.rejected_to == "draft_report"
    assert reloaded == spec


def test_a_charge_too_short_to_be_a_brief_is_refused_at_parse_time(quarterly_report: Workflow) -> None:
    """The same floor a declared step's instructions carry: the charge IS the whole briefing."""
    with pytest.raises(ValueError, match="charge"):
        _orchestrated(quarterly_report, charge="looks fine?")


def test_an_orchestrator_gated_spec_validates_and_saves_where_there_is_an_orchestrator(
    quarterly_report: Workflow, deciding: StepRegistry
) -> None:
    report = validate(_orchestrated(quarterly_report), deciding)

    assert report.ok, report.issues
    verify_savable(_orchestrated(quarterly_report), deciding)  # the write gate agrees, and raises if it does not


# --- running -----------------------------------------------------------------------------


async def test_an_approved_gate_passes_its_value_through(
    quarterly_report: Workflow, deciding: StepRegistry, orchestrator: ScriptedOrchestrator, call_log: CallLog
) -> None:
    """Approval releases the reviewed value under the gate's own id -- same as the sibling gates."""
    result = await run_workflow(_orchestrated(quarterly_report), deciding, params={"quarter": "Q3"}, approval=ApproveAll())

    assert call_log.calls.count("publish") == 1
    assert isinstance(result, ReportUrl)
    gate_id, charge, reviewed = orchestrator.heard[0]
    assert (gate_id, charge) == ("approve", CHARGE)
    assert reviewed == ReportDraft(text="net: 400")


async def test_a_rejection_reruns_the_parent_and_the_second_decision_approves(
    quarterly_report: Workflow, deciding: StepRegistry, orchestrator: ScriptedOrchestrator, call_log: CallLog
) -> None:
    """The loop, end to end: reject with feedback, `rejected_to` runs again, the next verdict releases it."""
    orchestrator.verdicts = [GateDecision(approve=False, feedback="Name the quarter this covers.")]

    result = await run_workflow(_orchestrated(quarterly_report), deciding, params={"quarter": "Q3"}, approval=ApproveAll())

    assert call_log.calls.count("draft_report") == 2, call_log.calls
    assert call_log.calls.count("fetch_sales") == 1, "a rejection rewinds to the gate's target, not to the top"
    assert call_log.calls.count("publish") == 1
    assert len(orchestrator.heard) == 2
    assert isinstance(result, ReportUrl)


async def test_the_gate_reports_starting_and_finishing_like_its_siblings(
    quarterly_report: Workflow, deciding: StepRegistry, orchestrator: ScriptedOrchestrator
) -> None:
    """A journal and a progress pane need no new vocabulary: the gate is a node that starts and finishes.

    Rejected once, so the transitions cover the rerun too -- a watcher left showing the gate as
    running after it has ruled is exactly what `_observed` exists to prevent.
    """
    orchestrator.verdicts = [GateDecision(approve=False, feedback="Not yet.")]
    watcher = Recorder()

    graph = build_graph(_orchestrated(quarterly_report), deciding, approval=ApproveAll(), observer=watcher)
    await graph.run(state=WorkflowState(params={"quarter": "Q3"}))

    assert [kind for kind, node in watcher.events if node == "approve"] == ["start", "finish", "start", "finish"]


# --- refusals ----------------------------------------------------------------------------


def test_a_run_with_no_orchestrator_refuses_the_spec_cold(quarterly_report: Workflow, registry: StepRegistry) -> None:
    """No decider, no gate: a promise the artifact made that this run cannot keep is said out loud."""
    spec = _orchestrated(quarterly_report)

    report = validate(spec, registry)

    assert not report.ok
    refusal = [issue for issue in report.issues if issue.kind is IssueType.NO_ORCHESTRATOR]
    assert refusal, report.issues
    assert refusal[0].where == "approve"
    assert "It has no orchestrator" in refusal[0].detail
    assert "never waved through" in refusal[0].detail


def test_nothing_compiles_or_saves_without_an_orchestrator(quarterly_report: Workflow, registry: StepRegistry) -> None:
    """The refusal reaches both doors, not just the report nobody read."""
    spec = _orchestrated(quarterly_report)

    with pytest.raises(WorkflowInvalidError, match="no orchestrator"):
        build_graph(spec, registry, approval=ApproveAll())
    with pytest.raises(SaveRefusedError):
        verify_savable(spec, registry)


def test_a_charge_made_of_whitespace_is_refused_cold(quarterly_report: Workflow, deciding: StepRegistry) -> None:
    """Long enough for the length floor, empty for the reader -- the decider would rule on nothing."""
    report = validate(_orchestrated(quarterly_report, charge=" " * 60), deciding)

    assert not report.ok
    blank = [issue for issue in report.issues if issue.kind is IssueType.BLANK_CHARGE]
    assert blank, report.issues
    assert blank[0].where == "approve.charge"


def test_rejecting_to_a_node_the_gate_never_influenced_is_refused(quarterly_report: Workflow, deciding: StepRegistry) -> None:
    """The rule every gate obeys: a rejection rewinds the gate's own history."""
    report = validate(_orchestrated(quarterly_report, rejected_to="publish"), deciding)

    assert not report.ok
    assert any(issue.kind is IssueType.BAD_REJECTED_TO and issue.where == "approve" for issue in report.issues)


def test_rejecting_to_a_node_that_does_not_exist_is_refused(quarterly_report: Workflow, deciding: StepRegistry) -> None:
    report = validate(_orchestrated(quarterly_report, rejected_to="nowhere"), deciding)

    assert not report.ok
    assert any(issue.kind is IssueType.BAD_REJECTED_TO and "unknown node 'nowhere'" in issue.detail for issue in report.issues)


# --- the evidence ------------------------------------------------------------------------


def test_a_legacy_evidence_record_without_gates_orchestrated_still_parses() -> None:
    legacy = RunEvidence.model_validate({"kind": "run", "workflow_id": "wf.report", "spec_hash": "abc", "outcome": "completed"})

    assert legacy.gates_orchestrated == ()


def test_an_orchestrated_run_is_attested_and_still_not_a_human_approval() -> None:
    """Machine-confirmed is what an orchestrated run earns once its own work checks out, and nobody's approval."""
    run = RunEvidence(
        workflow_id="wf.report",
        spec_hash="abc",
        outcome="completed",
        gates_orchestrated=("approve",),
        result=Outcome(status="succeeded", summary="the report was published"),
    )

    record = trust_level([run])

    assert run.gates_approved == ()
    assert record.runs_with_evidence == 1
    assert record.level == "machine-confirmed"


def test_an_orchestrated_run_that_checked_out_as_nothing_confirms_nothing() -> None:
    """Passing a gate is not doing the work: an orchestrator releasing a value earns no confirmation on its own."""
    run = RunEvidence(
        workflow_id="wf.report",
        spec_hash="abc",
        outcome="completed",
        gates_orchestrated=("approve",),
        result=Outcome(status="unknown", summary="the primary work never settled"),
    )

    record = trust_level([run])

    assert record.runs_with_evidence == 0
    assert record.unconfirmed_runs == 1
    assert record.level == "unverified"


async def test_who_passed_each_gate_is_read_off_the_run_state(
    quarterly_report: Workflow, deciding: StepRegistry, call_log: CallLog
) -> None:
    """The two derivations sit side by side and disagree about the same gate, which is the point."""
    spec = _orchestrated(quarterly_report)
    state_values: dict[str, object] = {"draft_report": ReportDraft(text="net: 400"), "approve": ReportDraft(text="net: 400")}

    assert orchestrated_gates(spec, state_values) == ("approve",)
    assert approved_gates(spec, state_values) == ()
    assert approved_gates(quarterly_report, state_values) == ("approve",)
    assert not call_log.calls


# --- the whole grammar in one artifact ---------------------------------------------------


async def test_a_spec_holding_every_node_kind_compiles_and_runs(orchestrator: ScriptedOrchestrator) -> None:
    """Five kinds, one graph: the exhaustiveness check that a new verb did not break an old one."""
    ran: list[str] = []

    async def fetch_sales(quarter: str) -> SalesData:
        ran.append("fetch_sales")
        return SalesData(quarter=quarter, total=1000)

    async def draft_report(sales: SalesData) -> ReportDraft:
        ran.append("draft_report")
        return ReportDraft(text=f"{sales.quarter}: {sales.total}")

    async def judge_report(report: ReportDraft) -> bool:
        ran.append("judge_report")
        return bool(report.text)

    async def sections(report: ReportDraft) -> list[str]:
        ran.append("sections")
        return report.text.split(": ")

    async def polish(text: str) -> str:
        ran.append("polish")
        return text.upper()

    async def publish_lines(lines: list[str]) -> ReportUrl:
        ran.append("publish_lines")
        return ReportUrl(url=" ".join(lines))

    registry = StepRegistry(decide_gate=orchestrator)
    for step in (fetch_sales, draft_report, judge_report, sections, polish, publish_lines):
        registry.register(step)

    spec = Workflow(
        name="every-kind",
        version=1,
        description="One artifact holding a run, a map, a person gate, a judge and an orchestrator gate.",
        parameters={"quarter": ParameterSpec(description="Fiscal quarter", json_schema={"type": "string"})},
        nodes=(
            RunNode(id="fetched", work="fetch_sales", inputs={"quarter": FromParameter(parameter="quarter")}),
            RunNode(id="drafted", work="draft_report", inputs={"sales": FromNode(node="fetched")}),
            JudgeGate(id="judged", work="judge_report", reviews=FromNode(node="drafted"), rejected_to="drafted"),
            PersonGate(id="approved", reviews=FromNode(node="judged"), rejected_to="drafted"),
            OrchestratorGate(id="vetted", charge=CHARGE, reviews=FromNode(node="approved"), rejected_to="drafted"),
            RunNode(id="split", work="sections", inputs={"report": FromNode(node="vetted")}),
            MapNode(id="polished", turns=("polish",), over=FromNode(node="split"), each="text"),
            RunNode(id="published", work="publish_lines", inputs={"lines": FromNode(node="polished")}),
        ),
        output=FromNode(node="published"),
    )
    assert validate(spec, registry).ok, validate(spec, registry).issues

    result = await run_workflow(spec, registry, params={"quarter": "Q3"}, approval=ApproveAll())

    assert result == ReportUrl(url="Q3 1000")
    assert ran.count("polish") == 2
    assert orchestrated_gates(spec, {"vetted": ReportDraft(text="Q3: 1000")}) == ("vetted",)


# --- what the decision record says --------------------------------------------------------


@dataclass
class ScriptedApproval:
    """A person gate's answer, scripted: one decision per reach, then approvals for ever."""

    decisions: list[GateDecision] = field(default_factory=list)
    reaches: int = 0

    async def review(self, gate_id: str, value: object, *, note: str = "") -> GateDecision:
        del gate_id, value, note
        self.reaches += 1
        if self.reaches <= len(self.decisions):
            return self.decisions[self.reaches - 1]
        return GateDecision(approve=True)


async def test_an_orchestrator_gate_records_its_own_decisions_and_hands_on_the_exact_repair(
    quarterly_report: Workflow, deciding: StepRegistry, orchestrator: ScriptedOrchestrator
) -> None:
    """A machine cleared it, and the record says so -- never `person`, whichever gate kind it sat on."""
    orchestrator.verdicts = [GateDecision(approve=False, feedback="Name the quarter this covers.", requested_repair="Add the quarter.")]
    state = WorkflowState(params={"quarter": "Q3"})

    await build_graph(_orchestrated(quarterly_report), deciding, approval=ApproveAll()).run(state=state)

    assert [(fact.source, fact.verdict, fact.review_generation) for fact in state.gate_decisions] == [
        ("orchestrator", "reject", 1),
        ("orchestrator", "approve", 2),
    ]
    owed = state.retry_feedback["draft_report"]
    assert [(item.source, item.attempt, item.requested_repair) for item in owed] == [("orchestrator", 1, "Add the quarter.")]
    assert state.reviewed_artifacts.read(owed[0].reviewed) == {"text": "net: 400"}


async def test_a_deadline_nobody_answered_is_recorded_as_the_refuser_it_was(
    quarterly_report: Workflow, registry: StepRegistry
) -> None:
    """A gate that timed out reruns its target like any rejection, and never as a person's word."""
    approval = ScriptedApproval(decisions=[GateDecision(approve=False, feedback="Nobody decided this gate.", source="timeout")])
    state = WorkflowState(params={"quarter": "Q3"})

    await build_graph(quarterly_report, registry, approval=approval).run(state=state)

    assert [fact.source for fact in state.gate_decisions] == ["timeout", "person"]
    owed = state.retry_feedback["draft_report"][0]
    assert owed.source == "timeout"
    assert owed.requested_repair == "Nobody decided this gate.", "with nothing else said, the words are the whole instruction"


async def test_automode_is_recorded_as_the_bypass_it_is(quarterly_report: Workflow, registry: StepRegistry) -> None:
    """A gate nobody was asked about is a fact of its own kind, never an approval by a person."""
    approval = ScriptedApproval(decisions=[GateDecision(approve=True, source="auto")])
    state = WorkflowState(params={"quarter": "Q3"})

    await build_graph(quarterly_report, registry, approval=approval).run(state=state)

    assert [(fact.source, fact.verdict) for fact in state.gate_decisions] == [("auto", "approve")]
    assert approved_gates(quarterly_report, state.values) == ("approve",), "the value is released, whoever released it"


async def test_a_bypass_may_pass_a_gate_and_may_not_refuse_one(quarterly_report: Workflow, registry: StepRegistry) -> None:
    """Automode decides nothing, so a refusal attributed to it would file a review nobody did.

    The refusal it files is nothing either: the run's own decision facts must not carry the
    rejection the run refuses to act on, or the evidence and the archive record would both report
    a review that never happened.
    """
    approval = ScriptedApproval(decisions=[GateDecision(approve=False, feedback="no", source="auto")])
    state = WorkflowState(params={"quarter": "Q3"})

    with pytest.raises(ValueError, match="automode"):
        await build_graph(quarterly_report, registry, approval=approval).run(state=state)

    assert state.gate_decisions == [], "nothing automode refused is on the record as a decision"
