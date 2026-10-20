"""RunEvidence: structural success signals, and the trust they derive. No LLM anywhere.

The rules under test are the design's moral centre. An evidence record records what
*mechanically* happened -- the run completed, these person gates released their values --
never an opinion. Trust is a derived property computed from the ledger, not a stored
field: nothing can type `approved` into frontmatter, and failures count without ever
confirming anything. Approval is a deliberate human act recorded with its author.
"""

from __future__ import annotations

import asyncio

from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any, cast

import pytest

from core.ai.workflows.contracts import (
    Approval,
    FileEvidenceLog,
    FromNode,
    FromParameter,
    Grade,
    Outcome,
    ParameterSpec,
    RunEvidence,
    RunNode,
    StepRegistry,
    Workflow,
    WorkflowResultSpec,
    approved_gates,
    spec_identity,
    trust_level,
)
from core.ai.workflows.domain.vocabulary import RequiredGateCheck
from pydantic import ValidationError
from pydantic_ai.models.test import TestModel
from pydantic_ai.tools import RunContext
from pydantic_ai.usage import RunUsage

from harness.features.running import service
from harness.features.running.domain.progress import GateVerdict
from harness.features.running.records import RunCollaborators
from harness.features.running.tools import run_workflow
from harness.session import NoSessionLog, Runtime, SessionPool
from harness.settings import RunSettings
from tests.conftest import CallLog, ReportDraft, SalesData, register_quarterly_steps
from tests.support.fakes.broadcaster import FakeBroadcaster
from tests.support.fakes.sessions import APool
from tests.support.state import a_state, records_under

if TYPE_CHECKING:
    from pathlib import Path


CONFIRMED = Outcome(status="succeeded", summary="every piece of the primary work succeeded")
"""The checked outcome of a run that did what it said. A completed run with an unknown one confirms nothing,
so every completed fixture below about filtering carries this one: the subject there is which events answer a
question, never whether a run earned its confirmation."""

UNCHECKED = Outcome(status="unknown", summary="the run did not complete, so nothing was checked")
"""The checked outcome a run that failed or stopped records."""


def _run(outcome: str = "completed", **kwargs: object) -> RunEvidence:
    fields: dict[str, object] = {"result": CONFIRMED if outcome == "completed" else UNCHECKED, "gates_auto": (), "gates_orchestrated": ()}
    return RunEvidence(workflow_id="wf.report", spec_hash="abc", outcome=outcome, **fields | kwargs)  # type: ignore[arg-type]


def test_nothing_attested_is_unverified() -> None:
    record = trust_level([])
    assert record.level == "unverified"
    assert record.runs_with_evidence == 0
    assert record.failed_runs == 0
    assert record.approved_by == ()


def test_a_completed_run_machine_confirms() -> None:
    record = trust_level([_run()])
    assert record.level == "machine-confirmed"
    assert record.runs_with_evidence == 1


def test_failures_are_counted_and_confirm_nothing() -> None:
    record = trust_level([_run("failed"), _run("failed")])
    assert record.level == "unverified"
    assert record.failed_runs == 2


def test_an_approval_is_a_human_act_with_an_author() -> None:
    record = trust_level([_run(), Approval(workflow_id="wf.report", spec_hash="abc", by="human:alex")])
    assert record.level == "approved"
    assert record.approved_by == ("human:alex",)


def test_a_structural_grade_is_folded_and_the_tier_ignores_it() -> None:
    """A grade is evidence of quality, not of completion -- the tier still only earns off `outcome`."""
    events = [
        RunEvidence(workflow_id="wf.r.v1", spec_hash="sha256:abc", workflow_version=1, outcome="completed", result=CONFIRMED, gates_auto=(), gates_orchestrated=()),
        Grade(
            workflow_id="wf.r.v1",
            spec_hash="sha256:abc",
            workflow_version=1,
            judge_kind="structural",
            judge="recall/groundtruth-research-v1",
            measure="evidence-recall",
            score=0.8,
        ),
        Grade(
            workflow_id="wf.r.v1",
            spec_hash="sha256:abc",
            workflow_version=1,
            judge_kind="structural",
            judge="recall/groundtruth-research-v1",
            measure="evidence-recall",
            score=0.6,
        ),
    ]
    record = trust_level(events, spec_hash="sha256:abc")
    assert record.structural_grades == 2
    assert record.structural_mean == 0.7
    assert record.structural_last == 0.6
    assert record.level == "machine-confirmed", "grades inform; completion still earns"


def test_a_grade_for_another_version_is_not_this_versions_evidence() -> None:
    """The same ruling the version filter makes everywhere else: unknown is not earned."""
    grade = Grade(
        workflow_id="wf.r.v2", spec_hash="abc", judge_kind="structural", judge="recall/gt-v1", measure="evidence-recall", score=1.0, workflow_version=2
    )
    record = trust_level([grade], version=1)
    assert record.structural_grades == 0


def test_the_newest_evidence_time_is_kept() -> None:
    early = _run(at=datetime(2026, 1, 1, tzinfo=UTC))
    late = _run(at=datetime(2026, 6, 1, tzinfo=UTC))
    assert trust_level([late, early]).last_evidence_at == datetime(2026, 6, 1, tzinfo=UTC)


def test_approved_gates_are_read_off_run_state(quarterly_report: Workflow) -> None:
    """An approved gate wrote its pass-through value; a waiting or rejected one wrote nothing.

    That asymmetry is the whole derivation: nothing self-reports, the state is the witness.
    """
    values: dict[str, object] = {"draft_report": ReportDraft(text="x"), "approve": ReportDraft(text="x")}
    assert approved_gates(quarterly_report, values) == ("approve",)
    assert approved_gates(quarterly_report, {"draft_report": ReportDraft(text="x")}) == ()


async def test_the_file_ledger_appends_and_filters(tmp_path: Path) -> None:
    ledger = FileEvidenceLog(tmp_path / "evidence.jsonl")
    await ledger.append(_run())
    await ledger.append(RunEvidence(workflow_id="wf.other", spec_hash="abc", outcome="completed", result=CONFIRMED, gates_auto=(), gates_orchestrated=()))
    await ledger.append(Approval(workflow_id="wf.report", spec_hash="abc", by="human:alex"))

    events = await ledger.events_for("wf.report")
    assert [type(event).__name__ for event in events] == ["RunEvidence", "Approval"]
    assert trust_level(events).level == "approved"
    assert trust_level(await ledger.events_for("wf.unknown")).level == "unverified"


async def test_a_damaged_ledger_line_is_skipped_not_fatal(tmp_path: Path) -> None:
    path = tmp_path / "evidence.jsonl"
    ledger = FileEvidenceLog(path)
    await ledger.append(_run())
    with path.open("a", encoding="utf-8") as handle:
        handle.write("{not json\n")
    await ledger.append(Approval(workflow_id="wf.report", spec_hash="abc", by="human:alex"))
    events = await ledger.events_for("wf.report")
    assert len(events) == 2, "the readable history survives one bad line"


async def test_a_launched_run_attests_its_own_completion(quarterly_report: Workflow, registry: StepRegistry, tmp_path: Path) -> None:
    """End to end through the app's launch path: wait at the gate, approve, attest.

    The observer that watched the run is the writer, and the approved gate is read off
    the same run state the session log snapshots -- nothing in this path self-reports.
    """
    records = records_under(tmp_path)
    session = Runtime(registry=registry, library=cast("Any", None), agent=cast("Any", None), session_log=NoSessionLog(), state=a_state(tmp_path))
    service.launch(session, records=records, topics=FakeBroadcaster(), spec=quarterly_report, workflow_id="wf.report", params={"quarter": "Q2"})
    for _ in range(500):
        if session.active.decision is not None:
            break
        await asyncio.sleep(0.01)
    decision = session.active.decision
    assert decision is not None, "the run must wait at its gate"
    decision.set_result(GateVerdict(approve=True))
    task = session.active.task
    assert task is not None
    await task

    events = await records.evidence.events_for("wf.report")
    attested = [event for event in events if isinstance(event, RunEvidence)]
    assert [event.outcome for event in attested] == ["completed"]
    assert attested[0].gates_approved == ("approve",), "the released gate is read off run state"


async def test_a_failed_run_attests_the_failure(quarterly_report: Workflow, call_log: CallLog, tmp_path: Path) -> None:
    """A failure counts and confirms nothing -- the ledger must record it, not swallow it."""
    records = records_under(tmp_path)
    breaking = StepRegistry()
    register_quarterly_steps(breaking, call_log, exclude=frozenset({"fetch_sales"}))

    async def fetch_sales(quarter: str) -> SalesData:
        msg = "the sales system is down"
        raise RuntimeError(msg)

    breaking.register(fetch_sales)
    session = Runtime(registry=breaking, library=cast("Any", None), agent=cast("Any", None), session_log=NoSessionLog(), state=a_state(tmp_path))
    service.launch(session, records=records, topics=FakeBroadcaster(), spec=quarterly_report, workflow_id="wf.doomed", params={"quarter": "Q2"})
    task = session.active.task
    assert task is not None
    await task

    events = await records.evidence.events_for("wf.doomed")
    assert [cast("RunEvidence", event).outcome for event in events] == ["failed"]
    assert trust_level(events).level == "unverified"


def test_trust_does_not_carry_across_a_version_the_ledger_never_saw() -> None:
    """An adaptation must not inherit the tier its parent earned. Watched live, end to end.

    The store this app uses mints one id per artifact NAME (`wf.lit-check-decision-record`), so
    saving an adapted child upserts the parent's page. The ledger keys on that id. So: run v1
    once, adapt it, save the child -- and the child, which has never executed anything, reports
    `machine-confirmed, runs_with_evidence: 1`. Reproduced against the real store.

    That defeats the sentence the whole design rests on: "there is no field a writer could set to
    skip the ladder". No field is needed. Adapt and re-save, and the ladder is skipped.

    So a tier is asked for a *version*, and a run that never recorded which version it exercised
    cannot answer for one. That is deliberately conservative: an unversioned evidence record is not
    evidence about v2, and unknown is not the same as earned -- the same ruling staleness makes.
    """
    ran_v1 = RunEvidence(workflow_id="wf.report", spec_hash="abc", workflow_version=1, outcome="completed", result=CONFIRMED, gates_auto=(), gates_orchestrated=())

    assert trust_level([ran_v1], version=1).level == "machine-confirmed"
    assert trust_level([ran_v1], version=2).level == "unverified", "v2 has never run"
    assert trust_level([ran_v1], version=2).runs_with_evidence == 0


def test_asking_for_no_version_still_folds_the_whole_ledger() -> None:
    """The unfiltered question stays askable: "has this artifact ever run successfully?"."""
    events = [
        RunEvidence(workflow_id="wf.report", spec_hash="abc", workflow_version=1, outcome="completed", result=CONFIRMED, gates_auto=(), gates_orchestrated=()),
        RunEvidence(workflow_id="wf.report", spec_hash="abc", workflow_version=2, outcome="failed", result=Outcome(status="unknown", summary="nothing was checked"), gates_auto=(), gates_orchestrated=()),
    ]
    record = trust_level(events)
    assert record.runs_with_evidence == 1
    assert record.failed_runs == 1


def test_an_evidence_record_that_never_recorded_a_version_cannot_count_for_one(tmp_path: Path) -> None:
    """A record that names no version is no evidence for one: absence of evidence is not evidence."""
    unversioned = RunEvidence(workflow_id="wf.report", spec_hash="abc", outcome="completed", result=CONFIRMED, gates_auto=(), gates_orchestrated=())
    assert trust_level([unversioned]).level == "machine-confirmed", "unfiltered, it is still a run that happened"
    assert trust_level([unversioned], version=1).level == "unverified", "but it cannot claim to be v1's run"


def test_an_approval_is_for_the_version_it_named() -> None:
    """Approving v1 is not approving whatever v1 was later adapted into."""
    approval = Approval(workflow_id="wf.report", spec_hash="abc", workflow_version=1, by="alex")
    assert trust_level([approval], version=1).level == "approved"
    assert trust_level([approval], version=2).level == "unverified"


def test_every_record_must_name_the_content_address_of_what_it_was_about() -> None:
    """A trust lookup keys on the content address, so a record without one could never be found."""
    with pytest.raises(ValidationError, match="spec_hash"):
        RunEvidence.model_validate_json('{"kind":"run","workflow_id":"wf.report","outcome":"completed"}')
    with pytest.raises(ValidationError, match="spec_hash"):
        Approval.model_validate_json('{"kind":"approval","workflow_id":"wf.report","by":"alex"}')
    with pytest.raises(ValidationError, match="spec_hash"):
        Grade(workflow_id="wf.report", spec_hash="", judge_kind="structural", judge="recall", measure="evidence-recall", score=0.9)


async def test_a_line_naming_no_content_address_is_skipped_like_a_damaged_one(tmp_path: Path) -> None:
    """The log offers no record it cannot tie to a workflow, and the lines around it still read."""
    path = tmp_path / "evidence.jsonl"
    with path.open("a", encoding="utf-8") as handle:
        handle.write('{"kind":"run","workflow_id":"wf.report","outcome":"completed"}\n')
    ledger = FileEvidenceLog(path)
    await ledger.append(Approval(workflow_id="wf.report", spec_hash="abc", by="human:alex"))

    assert [type(event).__name__ for event in await ledger.events_for("wf.report")] == ["Approval"]


_COMPLETED_RUN_WITH_AN_UNKNOWN_RESULT_JSON = (
    '{"kind":"run","workflow_id":"wf.quarterly-close","spec_hash":"abc","outcome":"completed",'
    '"result":{"status":"unknown","summary":"nothing was checked"},"gates_approved":["approve_report"],'
    '"gates_auto":[],"gates_orchestrated":[],"produced":{"collect_sales":"object"}}'
)
"""A completed run's evidence whose checked outcome is unknown, hand-typed, never dumped from today's model, so a
change to what gets defaulted fails loudly here rather than the fixture growing the field with the code."""


def test_a_completed_run_with_an_unknown_checked_outcome_reads_unconfirmed() -> None:
    """A run whose facts establish nothing cannot borrow the confirmation it never earned."""
    unchecked = RunEvidence.model_validate_json(_COMPLETED_RUN_WITH_AN_UNKNOWN_RESULT_JSON)
    assert unchecked.result.status == "unknown"

    record = trust_level([unchecked])
    assert record.level == "unverified"
    assert record.unconfirmed_runs == 1
    assert record.runs_with_evidence == 0


def test_trust_is_asked_by_what_ran_rather_than_by_where_it_was_kept() -> None:
    """The defect: an artifact that moved stores kept its evidence and lost the address it was filed under.

    Both events below are the same artifact under two addresses -- a folder id and a collection
    id. Keyed on the hash they fold into one history; keyed on either address, half of it vanishes.
    """
    moved = [
        RunEvidence(workflow_id="audit-a-claim@1", spec_hash="abc", outcome="completed", result=CONFIRMED, gates_auto=(), gates_orchestrated=()),
        RunEvidence(workflow_id="wf.audit-a-claim.v1", spec_hash="abc", outcome="completed", result=CONFIRMED, gates_auto=(), gates_orchestrated=()),
    ]
    assert trust_level(moved, spec_hash="abc").runs_with_evidence == 2
    assert trust_level(moved, spec_hash="def").level == "unverified", "a different artifact's hash matches nothing"


def test_the_empty_hash_is_refused_as_an_identity() -> None:
    """`""` names no workflow, so asking about it is refused rather than answered with nothing."""
    with pytest.raises(ValueError, match=r"^an empty spec hash is not an identity\. It names no workflow$"):
        trust_level([], spec_hash="")


def test_a_version_bump_needs_no_version_filter(quarterly_report: Workflow) -> None:
    """Version is a field of the spec, so a v4 run cannot be found under v3's hash. No second filter."""
    v3, v4 = quarterly_report, quarterly_report.model_copy(update={"version": 4})
    ran_v4 = RunEvidence(workflow_id="wf.report", spec_hash=spec_identity(v4), outcome="completed", result=CONFIRMED, gates_auto=(), gates_orchestrated=())
    assert trust_level([ran_v4], spec_hash=spec_identity(v4)).level == "machine-confirmed"
    assert trust_level([ran_v4], spec_hash=spec_identity(v3)).level == "unverified"


def test_the_two_filters_compose_rather_than_replacing_each_other() -> None:
    """The version filter and the hash filter each narrow the same fold."""
    events = [RunEvidence(workflow_id="wf.report", workflow_version=2, spec_hash="abc", outcome="completed", result=CONFIRMED, gates_auto=(), gates_orchestrated=())]
    assert trust_level(events, version=2, spec_hash="abc").runs_with_evidence == 1
    assert trust_level(events, version=1, spec_hash="abc").runs_with_evidence == 0


async def test_history_found_by_hash_survives_a_change_of_address(tmp_path: Path) -> None:
    """Eleven runs with evidence becoming unfindable is the bug. Re-key the artifact; keep the history.

    The two ids here are this repo's own re-keying: one page per NAME, then one page per version.
    """
    ledger = FileEvidenceLog(tmp_path / "evidence.jsonl")
    await ledger.append(RunEvidence(workflow_id="wf.report", spec_hash="abc", outcome="completed", result=CONFIRMED, gates_auto=(), gates_orchestrated=()))
    await ledger.append(RunEvidence(workflow_id="wf.report.v1", spec_hash="abc", outcome="completed", result=CONFIRMED, gates_auto=(), gates_orchestrated=()))

    history = await ledger.history_for(spec_hash="abc")
    assert len(history) == 2, "the run filed under the old address is this artifact's too"
    assert trust_level(history).runs_with_evidence == 2


async def test_another_artifacts_records_at_the_same_address_are_neither(tmp_path: Path) -> None:
    """A record that says what it ran and says something else is not unknown -- it is known to be other."""
    ledger = FileEvidenceLog(tmp_path / "evidence.jsonl")
    await ledger.append(RunEvidence(workflow_id="wf.report", spec_hash="other", outcome="completed", result=CONFIRMED, gates_auto=(), gates_orchestrated=()))

    assert await ledger.history_for(spec_hash="abc") == (), "it knows what it exercised, and it was not this"


async def test_the_ledger_refuses_the_empty_hash_as_an_identity(tmp_path: Path) -> None:
    """The empty hash names no workflow, so no record answers to it."""
    ledger = FileEvidenceLog(tmp_path / "evidence.jsonl")
    with pytest.raises(ValueError, match="not an identity"):
        await ledger.history_for(spec_hash="")


async def test_a_damaged_line_does_not_amnesia_a_hash_lookup_either(tmp_path: Path) -> None:
    """One bad write must not amnesia the ledger, on the new read path as much as the old one."""
    path = tmp_path / "evidence.jsonl"
    ledger = FileEvidenceLog(path)
    await ledger.append(RunEvidence(workflow_id="wf.report", spec_hash="abc", outcome="completed", result=CONFIRMED, gates_auto=(), gates_orchestrated=()))
    with path.open("a", encoding="utf-8") as handle:
        handle.write("{not json\n")
    await ledger.append(Approval(workflow_id="wf.report", spec_hash="abc", by="human:alex"))

    assert trust_level(await ledger.history_for(spec_hash="abc")).level == "approved"


async def test_a_launched_run_records_what_it_ran_not_only_where_it_was_kept(
    quarterly_report: Workflow, registry: StepRegistry, tmp_path: Path
) -> None:
    """A hash written nowhere is a hash nothing can be found by: the writer is where this has to start.

    The spec declares a result, so the run also earns a checked outcome -- confirmation now takes
    both, and a workflow that declares nothing to check can never be more than unverified.
    """
    records = records_under(tmp_path)
    declared = quarterly_report.model_copy(
        update={
            "result": WorkflowResultSpec(
                summary="the published report",
                primary_work="publish",
                required_gates=(RequiredGateCheck(gate_id="approve", accepted_sources=("person",)),),
            )
        }
    )
    session = Runtime(registry=registry, library=cast("Any", None), agent=cast("Any", None), session_log=NoSessionLog(), state=a_state(tmp_path))
    service.launch(session, records=records, topics=FakeBroadcaster(), spec=declared, workflow_id="wf.hashed", params={"quarter": "Q2"})
    for _ in range(500):
        if session.active.decision is not None:
            break
        await asyncio.sleep(0.01)
    decision = session.active.decision
    assert decision is not None
    decision.set_result(GateVerdict(approve=True))
    task = session.active.task
    assert task is not None
    await task

    ledger = records.evidence
    history = await ledger.history_for(spec_hash=spec_identity(declared))
    assert trust_level(history).level == "machine-confirmed"
    (attested,) = [event for event in history if isinstance(event, RunEvidence)]
    assert attested.result is not None
    assert attested.result.status == "succeeded"
    assert (attested.result.attempted, attested.result.succeeded) == (1, 1), "the primary work settled once, and it worked"
    assert [check.passed for check in attested.result.checks] == [True], "the person who approved it is who the result requires"


# --- the chat entry: run_workflow the TOOL, which must attest exactly as the button does ---
#
# A cold-clone field test found this plugin silent: workflows ran from conversation (browser or
# Zed) and the library's tiers never moved, because only the HTTP route wrote the ledger. The two
# tests below are the twins of the launch-path pair above, through the tool.


def _gateless_report() -> Workflow:
    """The quarterly shape minus its gate, so the tool's await never waits on a person."""
    return Workflow(
        name="quarterly-report",
        version=4,
        description="Fetch, reconcile -- no gate, so a test can await the whole run.",
        parameters={"quarter": ParameterSpec(description="Fiscal quarter", json_schema={"type": "string"})},
        nodes=(
            RunNode(id="fetch_sales", work="fetch_sales", inputs={"quarter": FromParameter(parameter="quarter")}),
            RunNode(id="fetch_costs", work="fetch_costs", inputs={"quarter": FromParameter(parameter="quarter")}),
            RunNode(
                id="reconcile", work="reconcile", inputs={"sales": FromNode(node="fetch_sales"), "costs": FromNode(node="fetch_costs")}
            ),
        ),
        output=FromNode(node="reconcile"),
    )


def _chat_ctx(session: Runtime) -> RunContext[Runtime]:
    return RunContext(deps=session, model=TestModel(), usage=RunUsage())


async def test_a_chat_run_attests_like_the_button(registry: StepRegistry, tmp_path: Path) -> None:
    """The tool's completion lands in the same ledger, signed by its own plugin."""
    records = records_under(tmp_path)
    session = Runtime(registry=registry, library=cast("Any", None), agent=cast("Any", None), session_log=NoSessionLog(), state=a_state(tmp_path))
    spec = _gateless_report()
    session.conversation.last_workflow = spec
    answer = await run_workflow(_chat_ctx(session), "draft", {"quarter": "Q2"}, run=RunCollaborators(records=records, run_settings=RunSettings(), sessions=APool(SessionPool(session)), topics=FakeBroadcaster()))
    assert "running in the background" in answer
    settlement = session.active.settlement
    assert settlement is not None
    assert "finished" in await settlement

    events = await records.evidence.events_for("draft")
    attested = [event for event in events if isinstance(event, RunEvidence)]
    assert [event.outcome for event in attested] == ["completed"]
    assert attested[0].runner == "app:chat", "the evidence says which plugin the run came through"
    assert attested[0].spec_hash == spec_identity(spec)
    assert attested[0].workflow_version == spec.version


async def test_a_refused_gate_is_on_the_record(quarterly_report: Workflow, registry: StepRegistry, tmp_path: Path) -> None:
    """Rejected-then-approved must not read like waved-through on the first look.

    A live crush test rejected a gate, watched the upstream rerun, approved the second wait --
    and found the reply said only "Gates crossed" while the ledger said only `gates_approved`.
    The refusal is evidence too: the approval object heard both clicks, so both are recorded.
    """
    records = records_under(tmp_path)
    session = Runtime(registry=registry, library=cast("Any", None), agent=cast("Any", None), session_log=NoSessionLog(), state=a_state(tmp_path))
    session.conversation.last_workflow = quarterly_report
    await run_workflow(_chat_ctx(session), "draft", {"quarter": "Q2"}, run=RunCollaborators(records=records, run_settings=RunSettings(), sessions=APool(SessionPool(session)), topics=FakeBroadcaster()))
    for verdict in (False, True):
        decision = None
        for _ in range(500):
            decision = session.active.decision
            if decision is not None and not decision.done():
                break
            await asyncio.sleep(0.01)
        assert decision is not None, "the gate never waiting"
        decision.set_result(GateVerdict(approve=verdict))
        for _ in range(500):  # wait out the restore, so the next verdict lands on the NEXT future
            if session.active.decision is not decision:
                break
            await asyncio.sleep(0.01)
    settlement = session.active.settlement
    assert settlement is not None
    answer = await settlement

    assert "Rejected first: approve (1x" in answer, "the settled account must say the gate was refused before it was crossed"
    events = await records.evidence.events_for("draft")
    record = cast("RunEvidence", events[-1])
    assert record.gates_approved == ("approve",)
    assert record.gates_rejected == {"approve": 1}


async def test_a_chat_run_failure_is_attested_and_relayed(call_log: CallLog, tmp_path: Path) -> None:
    """A failed chat run is a failed row AND a settled account the outcome turn relays -- never a dead turn."""
    records = records_under(tmp_path)
    breaking = StepRegistry()
    register_quarterly_steps(breaking, call_log, exclude=frozenset({"fetch_sales"}))

    async def fetch_sales(quarter: str) -> SalesData:
        msg = "the sales system is down"
        raise RuntimeError(msg)

    breaking.register(fetch_sales)
    session = Runtime(registry=breaking, library=cast("Any", None), agent=cast("Any", None), session_log=NoSessionLog(), state=a_state(tmp_path))
    session.conversation.last_workflow = _gateless_report()
    await run_workflow(_chat_ctx(session), "draft", {"quarter": "Q2"}, run=RunCollaborators(records=records, run_settings=RunSettings(), sessions=APool(SessionPool(session)), topics=FakeBroadcaster()))
    settlement = session.active.settlement
    assert settlement is not None
    assert "failed while running" in await settlement

    events = await records.evidence.events_for("draft")
    assert [cast("RunEvidence", event).outcome for event in events] == ["failed"]
    assert trust_level(events).level == "unverified"
