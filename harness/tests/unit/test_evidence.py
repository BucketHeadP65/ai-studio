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
"""The checked outcome of a run that did what it said. A completed run without one confirms nothing,
so every fixture below about filtering carries it: the subject there is which events answer a
question, never whether a run earned its confirmation."""


def _run(outcome: str = "completed", **kwargs: object) -> RunEvidence:
    checked = {"result": CONFIRMED} if outcome == "completed" else {}
    return RunEvidence(workflow_id="wf.report", outcome=outcome, **checked | kwargs)  # type: ignore[arg-type]


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
    record = trust_level([_run(), Approval(workflow_id="wf.report", by="human:alex")])
    assert record.level == "approved"
    assert record.approved_by == ("human:alex",)


def test_a_structural_grade_is_folded_and_the_tier_ignores_it() -> None:
    """A grade is evidence of quality, not of completion -- the tier still only earns off `outcome`."""
    events = [
        RunEvidence(workflow_id="wf.r.v1", spec_hash="sha256:abc", workflow_version=1, outcome="completed", result=CONFIRMED),
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
        workflow_id="wf.r.v2", judge_kind="structural", judge="recall/gt-v1", measure="evidence-recall", score=1.0, workflow_version=2
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
    await ledger.append(RunEvidence(workflow_id="wf.other", outcome="completed", result=CONFIRMED))
    await ledger.append(Approval(workflow_id="wf.report", by="human:alex"))

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
    await ledger.append(Approval(workflow_id="wf.report", by="human:alex"))
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
    ran_v1 = RunEvidence(workflow_id="wf.report", workflow_version=1, outcome="completed", result=CONFIRMED)

    assert trust_level([ran_v1], version=1).level == "machine-confirmed"
    assert trust_level([ran_v1], version=2).level == "unverified", "v2 has never run"
    assert trust_level([ran_v1], version=2).runs_with_evidence == 0


def test_asking_for_no_version_still_folds_the_whole_ledger() -> None:
    """The unfiltered question stays askable: "has this artifact ever run successfully?"."""
    events = [
        RunEvidence(workflow_id="wf.report", workflow_version=1, outcome="completed", result=CONFIRMED),
        RunEvidence(workflow_id="wf.report", workflow_version=2, outcome="failed"),
    ]
    record = trust_level(events)
    assert record.runs_with_evidence == 1
    assert record.failed_runs == 1


def test_an_evidence_record_that_never_recorded_a_version_cannot_count_for_one(tmp_path: Path) -> None:
    """Older records predate the field, and absence of evidence is not evidence."""
    legacy = RunEvidence(workflow_id="wf.report", outcome="completed", result=CONFIRMED)
    assert trust_level([legacy]).level == "machine-confirmed", "unfiltered, it is still a run that happened"
    assert trust_level([legacy], version=1).level == "unverified", "but it cannot claim to be v1's run"


def test_an_approval_is_for_the_version_it_named() -> None:
    """Approving v1 is not approving whatever v1 was later adapted into."""
    approval = Approval(workflow_id="wf.report", workflow_version=1, by="alex")
    assert trust_level([approval], version=1).level == "approved"
    assert trust_level([approval], version=2).level == "unverified"


def test_a_record_written_before_content_addressing_still_parses(tmp_path: Path) -> None:
    """Legacy records predate content addressing and must still parse; a required field would make history unreadable."""
    legacy = RunEvidence.model_validate_json('{"kind":"run","workflow_id":"wf.report","outcome":"completed"}')
    assert legacy.spec_hash == "", "empty means the record does not know what it ran -- not that it matches nothing"
    assert Approval.model_validate_json('{"kind":"approval","workflow_id":"wf.report","by":"alex"}').spec_hash == ""


_LEGACY_COMPLETED_RUN_JSON = (
    '{"kind":"run","workflow_id":"wf.quarterly-close","outcome":"completed",'
    '"gates_approved":["approve_report"],"produced":{"collect_sales":"object"}}'
)
"""A completed run's evidence exactly as it would have been written before the checked outcome
existed -- hand-typed, never dumped from today's model, so a change to what gets defaulted would
fail loudly here rather than the fixture quietly growing the field along with the code."""


def test_a_completed_run_recorded_before_the_checked_outcome_existed_reads_unconfirmed() -> None:
    """A run that predates `result` cannot borrow the confirmation it never earned -- unknown, honestly."""
    legacy = RunEvidence.model_validate_json(_LEGACY_COMPLETED_RUN_JSON)
    assert legacy.result is None

    record = trust_level([legacy])
    assert record.level == "unverified"
    assert record.unconfirmed_runs == 1
    assert record.runs_with_evidence == 0


def test_trust_is_asked_by_what_ran_rather_than_by_where_it_was_kept() -> None:
    """The defect: an artifact that moved stores kept its evidence and lost the address it was filed under.

    Both events below are the same artifact under two addresses -- a folder id and a collection
    id. Keyed on the hash they fold into one history; keyed on either address, half of it vanishes.
    """
    moved = [
        RunEvidence(workflow_id="audit-a-claim@1", spec_hash="abc", outcome="completed", result=CONFIRMED),
        RunEvidence(workflow_id="wf.audit-a-claim.v1", spec_hash="abc", outcome="completed", result=CONFIRMED),
    ]
    assert trust_level(moved, spec_hash="abc").runs_with_evidence == 2
    assert trust_level(moved, spec_hash="def").level == "unverified", "a different artifact's hash matches nothing"


def test_a_record_that_never_said_what_it_ran_cannot_count_for_a_hash() -> None:
    """The same ruling the version filter makes: unknown is not earned, and it must not read as it."""
    legacy = RunEvidence(workflow_id="wf.report", outcome="completed", result=CONFIRMED)
    assert trust_level([legacy]).level == "machine-confirmed", "unfiltered, it is still a run that happened"
    assert trust_level([legacy], spec_hash="abc").level == "unverified", "but it cannot claim to be this artifact's run"


def test_the_empty_hash_is_refused_as_an_identity() -> None:
    """Asking for `""` would hand back every record that knows nothing and call the fold confident.

    Reachable by accident: a caller passing `some_attestation.spec_hash` straight through hits it
    the first time it meets a legacy record.
    """
    with pytest.raises(ValueError, match="not an identity"):
        trust_level([], spec_hash="")


def test_a_version_bump_needs_no_version_filter(quarterly_report: Workflow) -> None:
    """Version is a field of the spec, so a v4 run cannot be found under v3's hash. No second filter."""
    v3, v4 = quarterly_report, quarterly_report.model_copy(update={"version": 4})
    ran_v4 = RunEvidence(workflow_id="wf.report", spec_hash=spec_identity(v4), outcome="completed", result=CONFIRMED)
    assert trust_level([ran_v4], spec_hash=spec_identity(v4)).level == "machine-confirmed"
    assert trust_level([ran_v4], spec_hash=spec_identity(v3)).level == "unverified"


def test_the_two_filters_compose_rather_than_replacing_each_other() -> None:
    """The version filter stays: it is the only question a legacy record can still answer."""
    events = [RunEvidence(workflow_id="wf.report", workflow_version=2, spec_hash="abc", outcome="completed", result=CONFIRMED)]
    assert trust_level(events, version=2, spec_hash="abc").runs_with_evidence == 1
    assert trust_level(events, version=1, spec_hash="abc").runs_with_evidence == 0


async def test_history_found_by_hash_survives_a_change_of_address(tmp_path: Path) -> None:
    """Eleven runs with evidence becoming unfindable is the bug. Re-key the artifact; keep the history.

    The two ids here are this repo's own re-keying: one page per NAME, then one page per version.
    """
    ledger = FileEvidenceLog(tmp_path / "evidence.jsonl")
    await ledger.append(RunEvidence(workflow_id="wf.report", spec_hash="abc", outcome="completed", result=CONFIRMED))
    await ledger.append(RunEvidence(workflow_id="wf.report.v1", spec_hash="abc", outcome="completed", result=CONFIRMED))

    history = await ledger.history_for(spec_hash="abc", workflow_id="wf.report.v1")
    assert len(history.matched) == 2, "the run filed under the old address is this artifact's too"
    assert history.basis == "content"
    assert trust_level(history.matched).runs_with_evidence == 2


async def test_a_grade_round_trips_beside_an_old_style_evidence_line(tmp_path: Path) -> None:
    """The third event kind is a JSONL line like any other, and it does not disturb the lines around it."""
    path = tmp_path / "evidence.jsonl"
    with path.open("a", encoding="utf-8") as handle:
        handle.write('{"kind":"run","workflow_id":"wf.report","outcome":"completed"}\n')
    ledger = FileEvidenceLog(path)
    await ledger.append(
        Grade(
            workflow_id="wf.report",
            spec_hash="abc",
            judge_kind="structural",
            judge="recall/groundtruth-research-v1",
            measure="evidence-recall",
            score=0.9,
        )
    )

    events = await ledger.events_for("wf.report")
    assert [type(event).__name__ for event in events] == ["RunEvidence", "Grade"]

    history = await ledger.history_for(spec_hash="abc", workflow_id="wf.report")
    assert [type(event).__name__ for event in history.matched] == ["Grade"]
    assert [type(event).__name__ for event in history.legacy] == ["RunEvidence"]


async def test_legacy_records_are_offered_apart_from_confirmed_ones_never_merged(tmp_path: Path) -> None:
    """Merging would launder: an unversioned record of unknown provenance would count as this artifact's run.

    Dropping them would orphan the history the user actually has. So both come back, in separate
    buckets, and the caller decides what to say about each.
    """
    ledger = FileEvidenceLog(tmp_path / "evidence.jsonl")
    await ledger.append(RunEvidence(workflow_id="wf.report", outcome="completed", result=CONFIRMED))
    await ledger.append(RunEvidence(workflow_id="wf.report", spec_hash="abc", outcome="completed", result=CONFIRMED))

    history = await ledger.history_for(spec_hash="abc", workflow_id="wf.report")
    assert len(history.matched) == 1
    assert len(history.legacy) == 1
    assert trust_level(history.matched).runs_with_evidence == 1, "the fold sees only what is confirmed"


async def test_nothing_recorded_is_distinguishable_from_a_history_nobody_can_confirm(tmp_path: Path) -> None:
    """The silent half of the defect: an empty lookup and an unconfirmable one both folded to a confident zero.

    Both still read `unverified`, because a tier is a fold over confirmed evidence and there is
    none -- but `basis` says which of the two situations produced it, so the view can stop
    reporting "never run" about an artifact whose history merely moved.
    """
    ledger = FileEvidenceLog(tmp_path / "evidence.jsonl")
    await ledger.append(RunEvidence(workflow_id="wf.report", outcome="completed", result=CONFIRMED))

    unconfirmable = await ledger.history_for(spec_hash="abc", workflow_id="wf.report")
    assert unconfirmable.basis == "address-only"
    assert unconfirmable.matched == ()
    assert len(unconfirmable.legacy) == 1

    nothing = await ledger.history_for(spec_hash="abc", workflow_id="wf.elsewhere")
    assert nothing.basis == "none"
    assert nothing.legacy == ()


async def test_another_artifacts_records_at_the_same_address_are_neither(tmp_path: Path) -> None:
    """A record that says what it ran and says something else is not unknown -- it is known to be other."""
    ledger = FileEvidenceLog(tmp_path / "evidence.jsonl")
    await ledger.append(RunEvidence(workflow_id="wf.report", spec_hash="other", outcome="completed", result=CONFIRMED))

    history = await ledger.history_for(spec_hash="abc", workflow_id="wf.report")
    assert history.matched == ()
    assert history.legacy == (), "it knows what it exercised, and it was not this"
    assert history.basis == "none"


async def test_a_hash_only_lookup_asks_about_no_address_at_all(tmp_path: Path) -> None:
    """The caller that has a spec and no store: no address means no legacy bucket to widen into."""
    ledger = FileEvidenceLog(tmp_path / "evidence.jsonl")
    await ledger.append(RunEvidence(workflow_id="wf.report", outcome="completed", result=CONFIRMED))
    await ledger.append(RunEvidence(workflow_id="wf.report", spec_hash="abc", outcome="completed", result=CONFIRMED))

    history = await ledger.history_for(spec_hash="abc")
    assert len(history.matched) == 1
    assert history.legacy == ()


async def test_the_ledger_refuses_the_empty_hash_as_an_identity(tmp_path: Path) -> None:
    """Otherwise every legacy record in the file answers to it, and the fold reads confident."""
    ledger = FileEvidenceLog(tmp_path / "evidence.jsonl")
    await ledger.append(RunEvidence(workflow_id="wf.report", outcome="completed", result=CONFIRMED))
    with pytest.raises(ValueError, match="not an identity"):
        await ledger.history_for(spec_hash="", workflow_id="wf.report")


async def test_a_damaged_line_does_not_amnesia_a_hash_lookup_either(tmp_path: Path) -> None:
    """One bad write must not amnesia the ledger, on the new read path as much as the old one."""
    path = tmp_path / "evidence.jsonl"
    ledger = FileEvidenceLog(path)
    await ledger.append(RunEvidence(workflow_id="wf.report", spec_hash="abc", outcome="completed", result=CONFIRMED))
    with path.open("a", encoding="utf-8") as handle:
        handle.write("{not json\n")
    await ledger.append(Approval(workflow_id="wf.report", spec_hash="abc", by="human:alex"))

    history = await ledger.history_for(spec_hash="abc", workflow_id="wf.report")
    assert trust_level(history.matched).level == "approved"


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
    history = await ledger.history_for(spec_hash=spec_identity(declared), workflow_id="wf.hashed")
    assert trust_level(history.matched).level == "machine-confirmed"
    assert history.basis == "content"
    (attested,) = [event for event in history.matched if isinstance(event, RunEvidence)]
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
