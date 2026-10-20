"""An archived run is graded on what it recorded, and never on what a reader would like it to mean.

Every row here is a fact an archive can hold: a status, a set of counts, a gate's decision, an
intent beside the workflow, a spend ledger with an unpriced row in it. What comes out is four
independent assessments, and the one thing none of them can be is a success nothing established.

The development dataset is read from the template that owns its vocabulary, through a path this
test hands in. Nothing in the app knows where a template keeps its evaluation data.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import TYPE_CHECKING, Literal

import pytest

from core.ai.artifacts import ArtifactRef
from core.ai.evaluation.contracts import (
    Assessment,
    BudgetRefusedError,
    BudgetScope,
    BudgetSnapshot,
    EvaluationError,
    EvaluationInput,
    FileEvaluationBudgetRecord,
    OwnerReview,
    OwnerReviewRequest,
    Provenance,
    ResourceUsage,
    ReviewedTaskOutcome,
    RunIdentity,
    TaskCase,
    TaskDataset,
    TaskOutcome,
    content_fingerprint,
    has_unresolved_charge,
)
from core.ai.evaluation.domain.reviews import owner_review_request_fingerprint
from core.ai.workflows.contracts import WorkflowIntent
from engram.evals.outcomes.contracts import CaseSeal, LabelExposure
from pydantic_ai.models.function import FunctionModel

from harness.features.evals.domain.benchmark import (
    BenchmarkCustody,
    BenchmarkInputs,
    BenchmarkRequest,
    DevelopmentReplay,
    OutcomeStateReport,
    SealedComparison,
    benchmark_cycle_id,
    freeze_benchmark,
)
from harness.features.evals.domain.outcomes import (
    CaseLabels,
    WorkflowAutomationCase,
    WorkflowAutomationDataset,
    evidence_support,
    require_stamped,
    structural_validity,
    task_success,
    workflow_design,
)
from harness.features.evals.domain.pilot import (
    WORKFLOW_AUTOMATION_DEVELOPMENT_LIMITS,
    FrozenPilot,
    PilotBaseline,
    PilotHandoff,
    PilotInputs,
    PilotParticipant,
    PilotSeal,
    PilotTrial,
    freeze_pilot,
    trial_usage,
)
from harness.features.evals.infrastructure.files import (
    file_report,
    load_dataset_stamp,
    load_stamped_dataset,
    load_workflow_automation_dataset,
)
from harness.features.evals.internal_contracts import BenchmarkArms, BenchmarkModel, ComposedBenchmark
from harness.features.evals.service.benchmark import BenchmarkMount, budgeted, model_type_of, run_outcome_benchmark
from harness.features.evals.service.pilot import ArchivedRunOutcomeQueries, WorkflowAutomationTaskRunner, recorded_usage
from harness.features.running.spend import RunSpend, SpendRow, SpendTotals
from tests.support.fakes.archive import FakeRunArchive
from tests.support.fakes.benchmark import (
    FakeOwnerReviews,
    ProviderNamingModel,
    RogueArm,
    RogueGrader,
    ScriptedArm,
    ScriptedGrader,
    answers,
    ask,
)
from tests.support.fakes.outcome_runs import FakePilotRuns, a_gate_check, an_archived_run, an_outcome
from tests.support.fakes.spend import FakeSpendLog

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

    from core.ai.evaluation.contracts import BudgetCycleState, EvaluationLimits
    from core.ai.workflows.contracts import OutcomeStatus
    from pydantic_ai.messages import ModelResponse
    from pydantic_ai.models import Model

    from harness.features.evals.domain.benchmark import BenchmarkCases, BenchmarkFreeze, BenchmarkTrialRecord, UsageSource
    from harness.features.evals.infrastructure.budgeted_model import BudgetedModel
    from harness.features.evals.internal_contracts import BenchmarkArm, BenchmarkGrader
    from tests.support.fakes.benchmark import Play

EVALS = Path(__file__).resolve().parents[3] / "templates" / "repo-docs" / "evals"
DATASET = EVALS / "workflow-automation-development.v1.json"
STAMP = EVALS / "workflow-automation-development.v1.stamp.json"

IDENTITY = RunIdentity(
    model="function-double",
    model_version="1",
    prompt_fingerprint="a" * 64,
    implementation_fingerprint="b" * 64,
    conditions_fingerprint="c" * 64,
    grader_fingerprint="d" * 64,
)
WHOLE_INTENT = WorkflowIntent(
    request="File each open question from this week's reviews.",
    expected_result="One page per open question, under the questions folder.",
    success_example="Three questions came out of Monday's review and three pages exist.",
    empty_input_behavior="No reviews, no pages, and the run says so.",
    failure_behavior="It stops and names the question it could not file.",
)


def _provenance() -> Provenance:
    return _dataset().cases[0].provenance


def _dataset():
    return load_workflow_automation_dataset(DATASET)


def _case(case_id: str = "wa-dev-04-success", *, permits_no_work: bool = False) -> WorkflowAutomationCase:
    return WorkflowAutomationCase(
        case_id=case_id,
        family="task_outcome",
        visibility="author_visible",
        split="train",
        request="File each open question from this week's reviews.",
        provenance=_provenance(),
        labels=CaseLabels(
            success_criterion="Every question the run was handed settled as written.",
            expected_task_success="pass",
            permits_no_work=permits_no_work,
        ),
    )


@pytest.mark.parametrize(
    ("status", "permits_no_work", "expected"),
    [
        pytest.param("succeeded", False, "pass", id="succeeded passes"),
        pytest.param("noop", True, "pass", id="a permitted noop passes"),
        pytest.param("noop", False, "fail", id="a noop the case did not permit fails"),
        pytest.param("partial", False, "fail", id="partial cannot pass"),
        pytest.param("partial", True, "fail", id="partial cannot pass even where no work was permitted"),
        pytest.param("failed", False, "fail", id="failed cannot pass"),
        pytest.param("unknown", False, "unknown", id="unknown is not a verdict"),
        pytest.param("unknown", True, "unknown", id="unknown stays unknown where no work was permitted"),
    ],
)
def test_task_success_reads_the_status_and_the_cases_own_criterion(status: OutcomeStatus, permits_no_work: bool, expected: str) -> None:
    labels = _case(permits_no_work=permits_no_work).labels
    assert task_success(an_outcome(status), labels).status == expected


def test_the_rework_a_run_took_is_named_beside_the_work_it_settled() -> None:
    reworked = an_outcome("succeeded", attempted=3, succeeded=3)
    plain = task_success(reworked, _case().labels).reason
    assert "rework" not in plain, "a run that needed none says nothing about rework"
    with_repairs = task_success(reworked.model_copy(update={"retry_attempts": 4}), _case().labels).reason
    assert "4 rework(s)" in with_repairs
    assert "3 succeeded" in with_repairs, "rework is named beside the settled work, never folded into it"


def test_a_graded_outcome_surfaces_the_rework_the_archived_run_recorded() -> None:
    record = an_archived_run(result=an_outcome("succeeded", attempted=2, succeeded=2).model_copy(update={"retry_attempts": 4}))
    outcome = ArchivedRunOutcomeQueries(FakeRunArchive([record])).outcome_for(record.run_id, _case(), intent=WHOLE_INTENT)
    assert outcome is not None
    assert "4 rework(s)" in outcome.task_success.reason


@pytest.mark.parametrize(
    ("record", "expected"),
    [
        pytest.param(an_archived_run(result=an_outcome("succeeded")), "pass", id="a completed run whose counts add up"),
        pytest.param(an_archived_run(result=an_outcome("failed", attempted=3, succeeded=1, failed=1)), "fail", id="counts that do not add up"),
        pytest.param(an_archived_run(result=an_outcome("unknown"), outcome="stopped"), "fail", id="an execution that never completed"),
    ],
)
def test_structural_validity_reads_the_shape_of_the_record(record, expected: str) -> None:
    assert structural_validity(record).status == expected


@pytest.mark.parametrize(
    ("checks", "expected"),
    [
        pytest.param((), "not_applicable", id="a result requiring no acceptance asks nothing"),
        pytest.param((a_gate_check(passed=True),), "pass", id="the required gate accepted"),
        pytest.param((a_gate_check(passed=False),), "fail", id="the required gate refused"),
        pytest.param((a_gate_check(passed=None),), "unknown", id="the check could not be put"),
        pytest.param((a_gate_check(passed=None, required=False),), "not_applicable", id="an optional gate is not a required one"),
    ],
)
def test_evidence_support_reads_the_required_gates_alone(checks, expected: str) -> None:
    assert evidence_support(an_archived_run(result=an_outcome("succeeded", checks=checks))).status == expected


@pytest.mark.parametrize(
    ("intent", "declares_result", "expected"),
    [
        pytest.param(WHOLE_INTENT, True, "pass", id="a stated request and a declared result"),
        pytest.param(None, True, "unknown", id="no intent was ever recorded"),
        pytest.param(WHOLE_INTENT, False, "fail", id="the workflow declares no result"),
        pytest.param(WorkflowIntent(request="File the questions."), True, "fail", id="an intent still owing its answers"),
    ],
)
def test_workflow_design_is_graded_from_the_intent_and_the_declared_result(intent, declares_result: bool, expected: str) -> None:
    record = an_archived_run(result=an_outcome("succeeded"), declares_result=declares_result)
    assert workflow_design(record, intent).status == expected


def test_a_run_that_succeeded_is_not_thereby_well_designed() -> None:
    record = an_archived_run(result=an_outcome("succeeded"), declares_result=False)
    assert task_success(record.result, _case().labels).status == "pass"
    assert workflow_design(record, WHOLE_INTENT).status == "fail"


def test_the_reader_grades_four_dimensions_apart_and_leaves_acceptance_to_a_review() -> None:
    record = an_archived_run(result=an_outcome("succeeded", checks=(a_gate_check(),)))
    reader = ArchivedRunOutcomeQueries(FakeRunArchive([record]))
    outcome = reader.outcome_for(record.run_id, _case(), intent=WHOLE_INTENT)
    assert outcome is not None
    assert (outcome.structural_validity.status, outcome.workflow_design.status) == ("pass", "pass")
    assert (outcome.evidence_support.status, outcome.task_success.status) == ("pass", "pass")
    assert outcome.task_id == "wa-dev-04-success"
    assert "owner_acceptance" not in outcome.model_dump(), "acceptance is a separate event, never part of a graded outcome"


def test_the_reader_answers_nothing_for_a_run_the_archive_does_not_hold() -> None:
    assert ArchivedRunOutcomeQueries(FakeRunArchive()).outcome_for("run-nobody-archived", _case()) is None


def test_an_unpriced_response_leaves_the_runs_cost_unknown() -> None:
    row = SpendRow(
        step="draft",
        at="2026-09-16T09:01:00+00:00",
        model_name="a-model-nothing-sells",
        provider_name="openai",
        input_tokens=100,
        output_tokens=50,
        cache_read_tokens=0,
        cache_write_tokens=0,
        cost=None,
    )
    unpriced = RunSpend(
        run_id="run-1",
        workflow_id="wf.workflow-automation-pilot",
        started_at="2026-09-16T09:00:00+00:00",
        runner="app:harness",
        session_id="sess-pilot",
        totals=SpendTotals().plus(row),
        steps={},
    )
    usage = recorded_usage(FakeSpendLog([unpriced]), "run-1")
    assert usage.cost_usd is None, "a run with an unpriced response has an unknown cost, never a cheaper one"
    assert usage.input_tokens == 100


def test_a_priced_run_reports_what_it_cost() -> None:
    priced = RunSpend(
        run_id="run-2",
        workflow_id="wf.workflow-automation-pilot",
        started_at="2026-09-16T09:00:00+00:00",
        runner="app:harness",
        session_id="sess-pilot",
        totals=SpendTotals(input_tokens=10, output_tokens=5, cost_usd=Decimal("0.25")),
        steps={},
    )
    assert recorded_usage(FakeSpendLog([priced]), "run-2").cost_usd == pytest.approx(0.25)


async def test_the_runner_reads_the_run_the_journey_left_and_holds_no_labels() -> None:
    record = an_archived_run(result=an_outcome("succeeded", summary="three questions filed"))
    trial = PilotTrial(case_id="wa-dev-04-success", repetition=0, run_id=record.run_id, trial_id="trial-1", intent=WHOLE_INTENT)
    runner = WorkflowAutomationTaskRunner(FakePilotRuns([trial]), FakeRunArchive([record]))
    execution = await runner(EvaluationInput(case_id="wa-dev-04-success", input="File the questions."), IDENTITY, 0)
    assert execution.output == "three questions filed"
    assert (execution.run_id, execution.trial_id) == (record.run_id, "trial-1")
    assert execution.result is not None
    assert execution.intent == WHOLE_INTENT
    assert [row.code for row in execution.diagnostics] == ["execution_outcome", "checked_outcome"]


async def test_a_case_whose_journey_never_ran_is_a_failed_trial() -> None:
    runner = WorkflowAutomationTaskRunner(FakePilotRuns(), FakeRunArchive())
    with pytest.raises(EvaluationError, match="no pilot trial is recorded"):
        await runner(EvaluationInput(case_id="wa-dev-04-success", input="File the questions."), IDENTITY, 0)


async def test_a_trial_whose_run_left_the_archive_is_a_failed_trial() -> None:
    trial = PilotTrial(case_id="wa-dev-04-success", repetition=0, run_id="run-swept", trial_id="trial-1")
    runner = WorkflowAutomationTaskRunner(FakePilotRuns([trial]), FakeRunArchive())
    with pytest.raises(EvaluationError, match="the archive no longer holds"):
        await runner(EvaluationInput(case_id="wa-dev-04-success", input="File the questions."), IDENTITY, 0)


def test_the_development_dataset_is_author_visible_and_covers_every_family() -> None:
    dataset = _dataset()
    assert {case.visibility for case in dataset.cases} == {"author_visible"}
    assert {case.split for case in dataset.cases} == {"train"}, "development cases are never a held-out split"
    assert {case.family for case in dataset.cases} == {
        "workflow_design",
        "task_outcome",
        "answer_support",
        "budget_admission",
        "execution_failure",
    }
    permitting = [case.case_id for case in dataset.cases if case.labels.permits_no_work]
    assert permitting == ["wa-dev-05-permitted-noop", "wa-dev-24-documentation-analysis-nothing-to-fold"], (
        "adding a case where doing nothing passes is a decision, so it is named here"
    )
    assert all(
        case.labels.expected_result_status == "noop" for case in dataset.cases if case.labels.permits_no_work
    ), "a case permitting no work expects a noop"
    assert any(case.labels.expected_answer == "unsupported" for case in dataset.cases), "at least one case the evidence does not settle"


def test_no_case_label_reaches_execution() -> None:
    dataset = _dataset()
    for case in dataset.cases:
        given = case.execution_input
        assert set(given.model_dump()) == {"case_id", "input"}
        assert given.input == case.request
        labels = case.labels
        assert labels.success_criterion not in given.input
        assert labels.notes == "" or labels.notes not in given.input
        assert case.task_case.success_criterion == labels.success_criterion, "the criterion travels with the grading side only"


def test_the_dataset_stamp_covers_this_version_and_claims_no_holdout() -> None:
    dataset = _dataset()
    stamp = load_dataset_stamp(STAMP)
    require_stamped(dataset, stamp)
    assert stamp.visibility == "author_visible"
    assert stamp.held_out_case_ids == ()
    assert stamp.independent_holdout_verified is None


def test_loading_the_dataset_with_its_stamp_reads_the_same_cases() -> None:
    assert load_stamped_dataset(DATASET, STAMP).cases == _dataset().cases


def test_loading_the_dataset_with_a_stamp_for_another_version_is_refused(tmp_path: Path) -> None:
    moved = load_dataset_stamp(STAMP).model_copy(update={"version": "v2"})
    stamp_path = tmp_path / "moved.stamp.json"
    stamp_path.write_text(moved.model_dump_json(), encoding="utf-8")
    with pytest.raises(ValueError, match="the stamp names"):
        load_stamped_dataset(DATASET, stamp_path)


def test_a_stamp_for_another_version_is_refused() -> None:
    stamp = load_dataset_stamp(STAMP).model_copy(update={"version": "v2"})
    with pytest.raises(ValueError, match="the stamp names"):
        require_stamped(_dataset(), stamp)


def test_the_development_set_declares_the_kinds_of_work_it_covers() -> None:
    """Each kind of work is the set's own word for it, declared in the file, and the stamp covers exactly those."""
    dataset = _dataset()
    declared = dataset.task_families
    stamped = load_dataset_stamp(STAMP).task_families

    assert declared, "the file declares no kinds of work"
    assert len(declared) == len(set(declared)), "a kind of work is named once"
    assert declared == stamped, "the stamp lists the same kinds of work, in the same order"
    for family in declared:
        assert [case.case_id for case in dataset.cases if case.task_family == family], f"{family} names no case"


def _as_written(dataset: WorkflowAutomationDataset, **edits: object) -> dict[str, object]:
    """This dataset as a file would carry it, with one key rewritten."""
    return {**dataset.model_dump(mode="json"), **edits}


def test_a_case_naming_a_kind_of_work_the_file_does_not_declare_is_refused() -> None:
    """The set closes its own vocabulary. A misspelling is refused by the file, with no stamp involved."""
    dataset = _dataset()
    cases = [case.model_dump(mode="json") for case in dataset.cases]
    cases[0] = {**cases[0], "task_family": "documentation analysys"}

    with pytest.raises(ValueError, match="does not declare: documentation analysys"):
        WorkflowAutomationDataset.model_validate(_as_written(dataset, cases=cases))


def test_a_kind_of_work_declared_twice_is_refused() -> None:
    dataset = _dataset()

    with pytest.raises(ValueError, match="duplicate task families"):
        WorkflowAutomationDataset.model_validate(_as_written(dataset, task_families=[*dataset.task_families, dataset.task_families[0]]))


def test_a_file_declaring_a_kind_of_work_no_case_uses_is_still_the_files_own_claim() -> None:
    """Declaring one nothing uses is allowed and the stamp then has to say so too, which is the cross-check."""
    dataset = _dataset()
    widened = WorkflowAutomationDataset.model_validate(_as_written(dataset, task_families=[*dataset.task_families, "something nobody wrote yet"]))

    with pytest.raises(ValueError, match="different kinds of work"):
        require_stamped(widened, load_dataset_stamp(STAMP))


def test_a_stamp_naming_a_kind_of_work_the_cases_do_not_is_refused() -> None:
    invented = (*load_dataset_stamp(STAMP).task_families, "something nobody wrote a case for")
    stamp = load_dataset_stamp(STAMP).model_copy(update={"task_families": invented})

    with pytest.raises(ValueError, match="different kinds of work"):
        require_stamped(_dataset(), stamp)


def test_every_case_carries_a_kind_of_work_or_none_and_execution_sees_neither() -> None:
    """A case about the journey itself names no kind of work, and no case's name reaches a run."""
    dataset = _dataset()

    assert any(case.task_family is None for case in dataset.cases), "the journey cases name no kind of work"
    for case in dataset.cases:
        assert case.task_family is None or case.task_family in dataset.task_families
        assert "task_family" not in case.execution_input.model_dump()


# -- freezing a pilot, and handing on what it has actually measured --------------------


PILOT_INPUTS = PilotInputs(
    model="deterministic",
    model_version="1",
    prompt="Design the automation the person asked for, and say what a finished run leaves them holding.",
    implementation="harness.features.evals.outcome_runner",
    tools=("search", "fetch", "write_note"),
    configuration="in-process engine, gates held for a person, one priced call in flight",
    grader="harness.features.evals.service.pilot.ArchivedRunOutcomeQueries",
)
BASELINE = PilotBaseline(description="The same work done by hand, from the reviews, once a week.")
LIMITATIONS = ("Author-visible cases only, so nothing here generalizes beyond the set they were written against.",)
SEAL = PilotSeal(baseline=BASELINE, limitations=LIMITATIONS, metered_roles=("designer", "worker"))


def _frozen(inputs: PilotInputs = PILOT_INPUTS, seal: PilotSeal = SEAL) -> FrozenPilot:
    return freeze_pilot(_dataset(), inputs, cycle_id="cycle-wa-pilot", seal=seal)


def test_a_frozen_pilot_hashes_each_part_over_its_own_content() -> None:
    """The same inputs freeze the same way, and a changed prompt changes the prompt hash alone."""
    first = _frozen()

    assert _frozen() == first, "freezing the same pilot twice is the same seal"
    assert first.dataset_fingerprint == content_fingerprint(_dataset().model_dump_json())
    assert first.limits == WORKFLOW_AUTOMATION_DEVELOPMENT_LIMITS
    assert first.identity.model == "deterministic"

    reworded = _frozen(PILOT_INPUTS.model_copy(update={"prompt": "Design the automation, and say nothing else."}))

    assert reworded.identity.prompt_fingerprint != first.identity.prompt_fingerprint
    assert reworded.identity.implementation_fingerprint == first.identity.implementation_fingerprint
    assert reworded.tool_fingerprint == first.tool_fingerprint


def test_a_frozen_pilots_tool_hash_is_over_the_set_and_not_its_order() -> None:
    reordered = _frozen(PILOT_INPUTS.model_copy(update={"tools": ("write_note", "search", "fetch")}))
    fewer = _frozen(PILOT_INPUTS.model_copy(update={"tools": ("search", "fetch")}))

    assert reordered.tool_fingerprint == _frozen().tool_fingerprint, "the same tools, listed in another order"
    assert fewer.tool_fingerprint != _frozen().tool_fingerprint, "a tool taken away is a different pilot"


def test_a_frozen_pilot_must_name_what_it_cannot_show() -> None:
    with pytest.raises(ValueError, match="at least 1 item"):
        PilotSeal(baseline=BASELINE, limitations=(), metered_roles=("worker",))


def test_a_seal_says_what_metered_it_or_the_written_reason_nothing_did() -> None:
    """One answer or the other, never both and never neither, so a seal cannot be quiet about it."""
    metered = _frozen()
    unmetered = _frozen(seal=PilotSeal(baseline=BASELINE, limitations=LIMITATIONS, unmetered_reason="read by hand"))

    assert metered.metered is True
    assert metered.metered_roles == ("designer", "worker")
    assert unmetered.metered is False
    assert unmetered.unmetered_reason == "read by hand"

    with pytest.raises(ValueError, match="names the roles it metered or the written reason"):
        PilotSeal(baseline=BASELINE, limitations=LIMITATIONS)
    with pytest.raises(ValueError, match="names the roles it metered or the written reason"):
        PilotSeal(baseline=BASELINE, limitations=LIMITATIONS, metered_roles=("worker",), unmetered_reason="both at once")


def test_a_baseline_nobody_measured_carries_no_number() -> None:
    assert BASELINE.observations is None, "a described baseline is not a measured one"
    assert PilotBaseline(description="Twelve recorded runs of the manual pass.", observations=12).observations == 12


def test_a_handoff_with_nothing_measured_says_exactly_that() -> None:
    """An unmeasured pilot reports nothing rather than a measured zero."""
    handoff = PilotHandoff(frozen=_frozen())

    assert handoff.measured is False
    assert handoff.outcomes == ()
    assert handoff.output_refs == ()
    assert handoff.correction_seconds is None, "nobody corrected anything, because nothing ran"
    assert handoff.usage.requests == 0
    assert handoff.usage.cost_usd == 0, "no rows at all is the one case where nothing is unknown"


def _reviewed(run_id: str, *, correction_seconds: float | None, cost_usd: float | None = 0.5) -> ReviewedTaskOutcome:
    passed = Assessment(status="pass", reason="the run did what the case asked")
    return ReviewedTaskOutcome(
        outcome=TaskOutcome(
            run_id=run_id,
            task_id="wa-dev-04-success",
            structural_validity=passed,
            workflow_design=passed,
            evidence_support=passed,
            task_success=passed,
            correction_seconds=correction_seconds,
            artifact_refs=(ArtifactRef(id="page", label="The page", run_id=run_id, node_id="filed", value_digest="a" * 64, media_type="text/markdown"),),
            usage=ResourceUsage(requests=2, cost_usd=cost_usd),
        ),
        owner_acceptance=Assessment(status="unknown", reason="nobody has reviewed this run"),
    )


def test_a_handoff_totals_correction_time_only_when_every_row_recorded_one() -> None:
    both = PilotHandoff(frozen=_frozen(), outcomes=(_reviewed("run-1", correction_seconds=30), _reviewed("run-2", correction_seconds=90)))
    one_missing = PilotHandoff(
        frozen=_frozen(), outcomes=(_reviewed("run-1", correction_seconds=30), _reviewed("run-2", correction_seconds=None))
    )

    assert both.correction_seconds == 120
    assert one_missing.correction_seconds is None, "a row nobody timed is unknown, never zero"


def test_a_handoff_names_the_values_the_measured_runs_actually_fixed() -> None:
    handoff = PilotHandoff(frozen=_frozen(), outcomes=(_reviewed("run-1", correction_seconds=30), _reviewed("run-2", correction_seconds=0)))

    assert [ref.run_id for ref in handoff.output_refs] == ["run-1", "run-2"]
    assert handoff.usage.requests == 4
    assert handoff.usage.cost_usd == 1.0


def test_a_handoff_leaves_the_cost_unknown_while_any_measured_row_is() -> None:
    handoff = PilotHandoff(
        frozen=_frozen(),
        outcomes=(_reviewed("run-1", correction_seconds=0), _reviewed("run-2", correction_seconds=0, cost_usd=None)),
    )

    assert handoff.usage.cost_usd is None, "an unpriced row makes the total unknown, not cheaper"
    assert handoff.usage.requests == 4, "what is known stays known"


def _snapshot(*, requests: int, spent: str, overshoot: str = "0", unresolved: bool = False) -> BudgetSnapshot:
    return BudgetSnapshot(
        scope=BudgetScope(cycle_id="cycle-wa-pilot", trial_id="trial-1"),
        trial_requests=requests,
        trial_attempts=1,
        trial_measured_model_spent_usd=Decimal(spent),
        cycle_measured_model_spent_usd=Decimal(spent),
        trial_measured_model_overshoot_usd=Decimal(overshoot),
        cycle_measured_model_overshoot_usd=Decimal(0),
        unresolved_charge=unresolved,
        deadline_at=datetime(2026, 9, 16, 9, 3, tzinfo=UTC),
    )


def test_a_trials_usage_is_the_records_own_requests_and_charge_beside_the_runs_tokens() -> None:
    usage = trial_usage(_snapshot(requests=4, spent="1.25"), ResourceUsage(input_tokens=900, output_tokens=300, cost_usd=99.0))

    assert (usage.input_tokens, usage.output_tokens) == (900, 300), "tokens come from the only record that counts them"
    assert usage.requests == 4, "requests come from the record that admitted them"
    assert usage.cost_usd == 1.25, "and so does the charge, never the deployment's own view of it"


def test_a_trial_on_a_cycle_holding_an_unresolved_charge_has_an_unknown_cost() -> None:
    usage = trial_usage(_snapshot(requests=4, spent="1.25", unresolved=True), ResourceUsage())

    assert usage.cost_usd is None, "part of a charge nothing could price is not the charge"
    assert usage.requests == 4


# -- the post-pilot benchmark's preflight table, one row at a time ---------------------


BENCH_START = datetime(2026, 9, 16, 9, 0, tzinfo=UTC)
IMPLEMENTATION_FROZEN = datetime(2026, 9, 16, 8, 0, tzinfo=UTC)
CASES_SEALED = datetime(2026, 9, 16, 8, 30, tzinfo=UTC)
IMPLEMENTATION_AUTHORS = ("the implementer",)
CUSTODIAN = "the custodian"

BENCHMARK_INPUTS = BenchmarkInputs(
    model="deterministic",
    model_version="1",
    workflow="wf.workflow-automation.v1",
    prompt="Design the automation the person asked for, and say what a finished run leaves them holding.",
    tools=("search", "fetch"),
    sources=("repo-docs@snapshot-1",),
    grader="the custodian's own grader",
)
SEALED_PROVENANCE = Provenance(source="the custodian", kind="recorded", review="owner", reviewer=CUSTODIAN)


def _sealed_case(case_id: str) -> TaskCase:
    return TaskCase(
        case_id=case_id,
        input=f"Do the work {case_id} asks for.",
        success_criterion=f"The answer does what {case_id} asked.",
        split="test",
        provenance=SEALED_PROVENANCE,
    )


def _sealed_dataset(case_ids: tuple[str, ...] = ("sealed-01",)) -> TaskDataset:
    return TaskDataset(name="wa-sealed", version="v1", cases=tuple(_sealed_case(case_id) for case_id in case_ids))


def _seal(case_id: str, *, implementation_fingerprint: str, authored_by: str = CUSTODIAN) -> CaseSeal:
    return CaseSeal(
        case_id=case_id,
        visibility="independently_withheld",
        authored_by=(authored_by,),
        sealed_by=CUSTODIAN,
        sealed_at=CASES_SEALED,
        implementation_fingerprint=implementation_fingerprint,
        label_fingerprint=content_fingerprint(f"label of {case_id}"),
        source_snapshot_fingerprint=content_fingerprint("repo-docs@snapshot-1"),
    )


def _custody(case_ids: tuple[str, ...], *, implementation_fingerprint: str, exposures: tuple[LabelExposure, ...] = ()) -> BenchmarkCustody:
    return BenchmarkCustody(
        custodian=CUSTODIAN,
        implementation_frozen_at=IMPLEMENTATION_FROZEN,
        implementation_authors=IMPLEMENTATION_AUTHORS,
        seals=tuple(_seal(case_id, implementation_fingerprint=implementation_fingerprint) for case_id in case_ids),
        exposures=exposures,
    )


class _Bench:
    """One benchmark ready to run: the record it measures on, the world it runs, and what it was asked.

    A row mutates whichever of these its own condition is about and then runs. Everything a row does
    not touch is a legitimate sealed benchmark on stand-in models, so each row's arrangement is the
    only thing separating it from a run that would have gone through.
    """

    def __init__(self, root: Path) -> None:
        self.root = root
        self.reports = root / "reports"
        self.ledger = FileEvaluationBudgetRecord(root / "budget")
        self.now = BENCH_START
        self.limits: EvaluationLimits | None = WORKFLOW_AUTOMATION_DEVELOPMENT_LIMITS
        self.charge_usd: Decimal | None = Decimal("0.01")
        self.settle_unresolved = False
        self.inputs = BENCHMARK_INPUTS
        self.repetitions = 2
        self.case_ids: tuple[str, ...] = ("sealed-01",)
        self.live_models = False
        self.verdicts: dict[str, bool] = {}
        self.baseline_plays: list[Play] = []
        self.candidate_plays: list[Play] = []
        self.candidate_checked: OutcomeStatus = "succeeded"
        self.reviews = FakeOwnerReviews()
        self.composed_calls = 0
        self.arms: BenchmarkArms | None = None
        self.drifted_grader = False
        self.identical_arms = False
        self.unmetered_baseline = False
        self.declared_models: tuple[BenchmarkModel, ...] | None = None
        self.baseline_over: BenchmarkModel | None = None
        self.grader_source: UsageSource = "this_record"
        self.candidate_declares_nothing = False
        self.rogue_baseline: Model | None = None
        self.rogue_grader: Model | None = None
        self.baseline_raises = ""
        self.grader_raises = ""
        self.grader_trips_on_slot: int | None = None
        self.grader_declares: tuple[BenchmarkModel, ...] | None = None
        self.declare_grader = True
        self.run_engine: Literal["in_process", "temporal"] = "in_process"
        self.temporal_address = "localhost:7233"
        self.cases: BenchmarkCases = SealedComparison(dataset_path=root / "sealed.json", custody_path=root / "custody.json")
        self.write_cases()

    def write_cases(self) -> None:
        """Put the sealed dataset and a clean custody record on disk, as a custodian would file them."""
        dataset = _sealed_dataset(self.case_ids)
        (self.root / "sealed.json").write_text(dataset.model_dump_json(indent=2), encoding="utf-8")
        custody = _custody(self.case_ids, implementation_fingerprint=self.freeze.workflow_fingerprint)
        (self.root / "custody.json").write_text(custody.model_dump_json(indent=2), encoding="utf-8")

    def clock(self) -> datetime:
        """The instant this benchmark reads, which a row's own play may move."""
        return self.now

    def charge(self, response: ModelResponse) -> Decimal | None:
        """What this benchmark's responses cost, or `None` for a charge nothing can name.

        The parts-less probe the preflight prices is answered separately from a real response, so a
        row can have a resolver that prices at preflight and still cannot price what came back.
        """
        return None if self.settle_unresolved and response.parts else self.charge_usd

    @property
    def freeze(self) -> BenchmarkFreeze:
        """What this benchmark would freeze as it stands, which is what names its cycle."""
        return freeze_benchmark(self.inputs, root=self.ledger.state_identity(), limits=self.limits)

    @property
    def cycle_id(self) -> str:
        """The one cycle this benchmark spends under, derivable before anything is built."""
        return benchmark_cycle_id(self.freeze, _sealed_dataset(self.case_ids).fingerprint)

    @property
    def participant(self) -> PilotParticipant:
        """This process against the record's own root, which is what the preflight checks it on."""
        identity = self.ledger.state_identity()
        return PilotParticipant(
            name="the benchmark process",
            canonical_state_root=identity.canonical_state_root,
            device_id=identity.device_id,
            cooperative_locking=True,
        )

    def a_model(self) -> Model:
        return ProviderNamingModel(function=answers) if self.live_models else FunctionModel(function=answers)

    async def compose(self, cycle_id: str) -> ComposedBenchmark:
        """This benchmark's own world, mounted on the record, as a deployment's composer would build it."""
        self.composed_calls += 1
        mount = BenchmarkMount(record=self.ledger, cycle_id=cycle_id, charge=self.charge, clock=self.clock)
        baseline_model = budgeted(self.a_model(), mount, path="baseline:double", role="worker")
        grader_model = budgeted(self.a_model(), mount, path="grader:double", role="grader")
        candidate_model = budgeted(self.a_model(), mount, path="candidate:double", role="designer")
        if self.unmetered_baseline:
            baseline_model = BenchmarkModel(path="baseline:double", role="worker", model=self.a_model())
        baseline_arm: BenchmarkArm = ScriptedArm(self.baseline_over or baseline_model, name="baseline", plays=self.baseline_plays)
        if self.rogue_baseline is not None:
            baseline_arm = RogueArm((baseline_model,), asks=self.rogue_baseline, name="baseline", raises=self.baseline_raises)
        candidate_arm: BenchmarkArm = ScriptedArm(candidate_model, name="candidate", plays=self.candidate_plays, checked=self.candidate_checked)
        if self.candidate_declares_nothing:
            candidate_arm = RogueArm((), asks=self.a_model(), name="candidate")
        grader: BenchmarkGrader = ScriptedGrader(grader_model, verdicts=self.verdicts, trips_on_slot=self.grader_trips_on_slot, elsewhere=self.a_model())
        if self.rogue_grader is not None:
            declares = (grader_model,) if self.grader_declares is None else self.grader_declares
            grader = RogueGrader(declares, asks=self.rogue_grader, source=self.grader_source, raises=self.grader_raises)
        self.arms = BenchmarkArms(baseline=baseline_arm, candidate=candidate_arm, grader=grader)
        built = (baseline_model, grader_model, candidate_model) if self.declare_grader else (baseline_model, candidate_model)
        models = built if self.declared_models is None else self.declared_models
        identity = RunIdentity(
            model="deterministic",
            model_version="1",
            prompt_fingerprint=self.freeze.prompt_fingerprint,
            implementation_fingerprint=content_fingerprint("the simple single-run baseline"),
            conditions_fingerprint=content_fingerprint("in-process, stand-in models"),
            grader_fingerprint=self.freeze.grader_fingerprint,
        )
        candidate = identity.model_copy(update={"implementation_fingerprint": self.freeze.workflow_fingerprint})
        if self.drifted_grader:
            candidate = candidate.model_copy(update={"grader_fingerprint": content_fingerprint("some other grader")})
        return ComposedBenchmark(
            models=models,
            arms=self.arms,
            baseline=identity,
            candidate=identity if self.identical_arms else candidate,
            reviews=self.reviews,
        )

    async def run(self) -> OutcomeStateReport:
        """Run this benchmark as it now stands and hand back the report it filed."""
        request = BenchmarkRequest(
            objective="task-success",
            cases=self.cases,
            inputs=self.inputs,
            report_root=self.reports,
            limits=self.limits,
            participants=(self.participant,),
            run_engine=self.run_engine,
            temporal_address=self.temporal_address,
            repetitions=self.repetitions,
        )
        return await run_outcome_benchmark(request, self.ledger, self.compose, charge=self.charge, clock=self.clock)

    async def cycle_state(self) -> BudgetCycleState | None:
        """What the record holds for this benchmark's own cycle, read back from disk."""
        return await self.ledger.read(self.cycle_id)

    def slot(self, report: OutcomeStateReport, case_id: str, repetition: int, variant: str) -> BenchmarkTrialRecord:
        """The one filed slot this case, repetition and arm produced."""
        found = [row for row in report.trials if row.case_id == case_id and row.repetition == repetition and row.variant == variant]
        assert len(found) == 1, f"expected exactly one filed slot for {case_id}-{repetition}-{variant}, found {len(found)}"
        return found[0]


def _arm(bench: _Bench, name: str) -> ScriptedArm:
    assert bench.arms is not None, "the composer was never asked for a world"
    arm = bench.arms.baseline if name == "baseline" else bench.arms.candidate
    assert isinstance(arm, ScriptedArm), "the arms are the scripted doubles"
    return arm


def _refusals(report: OutcomeStateReport) -> list[str]:
    return [note.refusal for note in report.refusals]


async def _development_only(bench: _Bench) -> None:
    bench.cases = DevelopmentReplay(dataset_path=DATASET, stamp_path=STAMP)


async def _expect_development_only(bench: _Bench, report: OutcomeStateReport) -> None:
    assert report.empirical_status == "blocked"
    assert _refusals(report) == ["development_dataset_only"]
    assert "author-visible" in report.refusals[0].detail
    assert report.model_kind == "unbuilt", "no model was composed at all, which is not the same as running on stand-ins"
    assert not report.paid_benchmark_performed
    assert bench.composed_calls == 0, "no world was built, so no model was ever reached for"
    assert await bench.cycle_state() is None, "no cycle was opened, so nothing could have been admitted"
    assert (bench.reports / f"{report.report_id}.json").is_file(), "the blocked report is on disk under its own name"


async def _seal_mismatch_and_author_exposure(bench: _Bench) -> None:
    bench.case_ids = ("sealed-01", "sealed-02")
    bench.write_cases()
    exposure = LabelExposure(
        exposure_id="exposure-1",
        case_id="sealed-02",
        actor=IMPLEMENTATION_AUTHORS[0],
        role="implementer",
        purpose="read the labels while tuning",
        at=CASES_SEALED,
        label_fingerprint=content_fingerprint("label of sealed-02"),
    )
    custody = _custody(bench.case_ids, implementation_fingerprint=content_fingerprint("some other implementation"), exposures=(exposure,))
    (bench.root / "custody.json").write_text(custody.model_dump_json(indent=2), encoding="utf-8")


async def _expect_seal_mismatch_and_author_exposure(bench: _Bench, report: OutcomeStateReport) -> None:
    assert _refusals(report) == ["custody_unverified"]
    assert report.custody is not None
    assert report.custody.independent_holdout_verified is False
    assert report.custody.verified_case_ids == (), "no case passed every custody rule, so none is attributable"
    assert "fingerprint mismatch" in report.refusals[0].detail
    assert "exposure" in report.refusals[0].detail
    assert bench.composed_calls == 0, "execution never started: the world was not even built"
    assert await bench.cycle_state() is None


async def _live_model_without_limits(bench: _Bench) -> None:
    bench.live_models = True
    bench.limits = None


async def _expect_live_model_without_limits(bench: _Bench, report: OutcomeStateReport) -> None:
    assert _refusals(report) == ["limits_absent"]
    assert report.model_kind == "live", "the models were read as reaching a provider, not declared to"
    assert _arm(bench, "baseline").slots == [], "execution never started"
    assert await bench.cycle_state() is None, "no cycle was opened without a threshold on it"


async def _live_model_without_a_price(bench: _Bench) -> None:
    bench.live_models = True
    bench.charge_usd = None


async def _expect_live_model_without_a_price(bench: _Bench, report: OutcomeStateReport) -> None:
    assert _refusals(report) == ["pricing_unknown"]
    assert "worker" in report.refusals[0].detail, "the baseline's role could not be priced"
    assert "grader" in report.refusals[0].detail, "and neither could the grader's"
    assert _arm(bench, "baseline").slots == [], "refused before the first request, not after it"
    assert await bench.cycle_state() is None, "the cycle that could not record what it spends was never opened"


async def _settle(bench: _Bench, trial_id: str, request_id: str, amount: str) -> None:
    """Put one already-settled charge on `trial_id`, the way an earlier sitting would have left it."""
    scope = BudgetScope(cycle_id=bench.cycle_id, trial_id=trial_id)
    await bench.ledger.begin_trial(scope, at=BENCH_START)
    admitted = await bench.ledger.admit(scope, request_id, "worker", at=BENCH_START)
    await bench.ledger.settle(admitted, Decimal(amount), at=BENCH_START)


async def _both_thresholds_already_reached(bench: _Bench) -> None:
    await bench.ledger.begin_cycle(bench.cycle_id, WORKFLOW_AUTOMATION_DEVELOPMENT_LIMITS, pricing_known=True, at=BENCH_START)
    await _settle(bench, "an-earlier-trial", "an-earlier-request", "8.00")
    await _settle(bench, "sealed-01-0-baseline", "this-trials-earlier-request", "2.00")


async def _expect_both_thresholds_already_reached(bench: _Bench, report: OutcomeStateReport) -> None:
    assert report.empirical_status == "inconclusive"
    assert bench.slot(report, "sealed-01", 0, "baseline").refusal == "trial_cost_stop", "this trial had already spent its own USD 2"
    others = {bench.slot(report, "sealed-01", repetition, variant).refusal for repetition, variant in ((0, "candidate"), (1, "baseline"), (1, "candidate"))}
    assert others == {"cycle_cost_stop"}, "every fresh trial met the cycle's own already-spent USD 10"
    assert report.graded_case_ids == (), "nothing was admitted, so nothing was graded"
    state = await bench.cycle_state()
    assert state is not None
    admitted = [row.admission.request_id for row in state.requests]
    assert admitted == ["an-earlier-request", "this-trials-earlier-request"], "no later request was admitted at all"


async def _a_crossing_call(bench: _Bench) -> None:
    bench.charge_usd = Decimal("2.75")


async def _expect_a_crossing_call(bench: _Bench, report: OutcomeStateReport) -> None:
    crossed = bench.slot(report, "sealed-01", 0, "baseline")
    assert crossed.measured_model_spent_usd == Decimal("2.75"), "the actual charge of the crossing call is recorded, not the threshold"
    assert crossed.measured_model_overshoot_usd == Decimal("0.75"), "and how far past the USD 2 trial threshold it went"
    assert crossed.refusal == "trial_cost_stop", "the next call on that trial was refused"
    assert report.cycle_measured_model_spent_usd == Decimal("11.00"), "four crossing calls, each charged what it actually cost"
    assert report.cycle_measured_model_overshoot_usd == Decimal("1.00"), "and how far past the USD 10 cycle threshold they went together"
    state = await bench.cycle_state()
    assert state is not None
    settled = [row.charge_usd for row in state.requests if row.admission.scope.trial_id == "sealed-01-0-baseline"]
    assert settled == [Decimal("2.75")], "one call was charged and no later call on that trial was"


async def _hard_trial_limits(bench: _Bench) -> None:
    async def sixteen_requests(model: BudgetedModel) -> int:
        asked = WORKFLOW_AUTOMATION_DEVELOPMENT_LIMITS.trial_request_limit - 1
        for _ in range(asked):
            await ask(model)
        return asked

    async def four_attempts(model: BudgetedModel) -> int:
        for attempt in range(WORKFLOW_AUTOMATION_DEVELOPMENT_LIMITS.trial_attempt_limit + 1):
            await model.record.begin_attempt(model.scope, f"attempt-{attempt}", at=bench.clock())
        return 0

    async def past_the_deadline(model: BudgetedModel) -> int:
        del model
        bench.now = BENCH_START + timedelta(seconds=WORKFLOW_AUTOMATION_DEVELOPMENT_LIMITS.trial_deadline_seconds + 1)
        return 0

    bench.baseline_plays = [sixteen_requests, four_attempts]
    bench.candidate_plays = [past_the_deadline]


async def _expect_hard_trial_limits(bench: _Bench, report: OutcomeStateReport) -> None:
    assert bench.slot(report, "sealed-01", 0, "baseline").refusal == "request_limit"
    assert bench.slot(report, "sealed-01", 0, "candidate").refusal == "deadline"
    assert bench.slot(report, "sealed-01", 1, "baseline").refusal == "attempt_limit"
    state = await bench.cycle_state()
    assert state is not None
    admitted = [row for row in state.requests if row.admission.scope.trial_id == "sealed-01-0-baseline"]
    assert len(admitted) == WORKFLOW_AUTOMATION_DEVELOPMENT_LIMITS.trial_request_limit, "exactly sixteen, and the seventeenth refused"


async def _an_uncertain_charge(bench: _Bench) -> None:
    bench.settle_unresolved = True


async def _expect_an_uncertain_charge(bench: _Bench, report: OutcomeStateReport) -> None:
    assert report.unresolved_charge is True
    assert report.cycle_measured_model_spent_usd is None, "part of an unresolvable charge is not the charge"
    assert bench.slot(report, "sealed-01", 0, "baseline").unresolved_charge is True
    after_restart = FileEvaluationBudgetRecord(bench.root / "budget")
    state = await after_restart.read(bench.cycle_id)
    assert state is not None, "the cycle is on disk, so a new process reads the same unresolved state"
    assert has_unresolved_charge(state) is True
    with pytest.raises(BudgetRefusedError) as refused:
        await after_restart.admit(BudgetScope(cycle_id=bench.cycle_id, trial_id="sealed-01-0-baseline"), "after-restart", "worker", at=BENCH_START)
    assert refused.value.refusal == "unresolved_charge", "a new process is blocked by the same unresolved charge"


async def _the_grader_makes_a_request(bench: _Bench) -> None:
    del bench


async def _expect_the_grader_makes_a_request(bench: _Bench, report: OutcomeStateReport) -> None:
    state = await bench.cycle_state()
    assert state is not None
    roles = [row.admission.role for row in state.requests if row.admission.scope.trial_id == "sealed-01-0-baseline"]
    assert roles == ["worker", "grader"], "the grader's request is on the very trial it graded"
    assert bench.slot(report, "sealed-01", 0, "baseline").requests == 2, "and the filed slot counts both of them"
    assert len(state.requests) == 4 * 2, "every slot's arm and grader are on the one cycle record"


async def _identity_drift(bench: _Bench) -> None:
    bench.drifted_grader = True


async def _expect_identity_drift(bench: _Bench, report: OutcomeStateReport) -> None:
    assert _refusals(report) == ["identity_drift"]
    assert "grader differs between the arms" in report.refusals[0].detail
    assert "candidate grader is not the grader the seal froze" in report.refusals[0].detail
    assert _arm(bench, "baseline").slots == [], "an incomparable pair is refused rather than compared"
    assert await bench.cycle_state() is None


async def _a_complete_sealed_run(bench: _Bench) -> None:
    bench.case_ids = ("sealed-01", "sealed-02")
    bench.write_cases()
    bench.verdicts = {"sealed-02": False}
    bench.reviews = FakeOwnerReviews(
        {
            "candidate-sealed-01-0": (_owner_review("review-accepted", "candidate-sealed-01-0", "accepted"),),
            "candidate-sealed-02-0": (_owner_review("review-rejected", "candidate-sealed-02-0", "rejected"),),
        }
    )


async def _expect_a_complete_sealed_run(bench: _Bench, report: OutcomeStateReport) -> None:
    assert report.empirical_status == "measured"
    assert report.model_kind == "test_double", "measured on stand-ins is apparatus output, and the report says so"
    assert not report.paid_benchmark_performed
    assert sorted(report.graded_case_ids) == ["sealed-01", "sealed-02"]
    assert report.ungraded_case_ids == ()
    assert len(report.trials) == len(bench.case_ids) * bench.repetitions * 2, "every planned slot was filed"
    acceptance = {row.run_id: row.acceptance for row in report.owner_acceptance}
    assert acceptance["candidate-sealed-01-0"] == "accepted"
    assert acceptance["candidate-sealed-02-0"] == "rejected", "a rejected outcome is preserved, not dropped"
    assert acceptance["baseline-sealed-01-0"] == "unknown", "a run nobody reviewed stays unknown"
    assert report.statistical_claim == "descriptive_only"
    assert report.recommendation == "none"
    assert {row.case_id for row in report.trials} == set(bench.case_ids)
    filed = OutcomeStateReport.model_validate_json((bench.reports / f"{report.report_id}.json").read_text(encoding="utf-8"))
    grades = {(row.case_id, row.repetition, row.variant): row.grade for row in filed.trials}
    assert all(grade is not None for grade in grades.values()), "every slot that was graded carries its grade on disk"
    failed = sorted(slot for slot, grade in grades.items() if grade is not None and not grade.success)
    assert failed == [("sealed-02", repetition, variant) for repetition in (0, 1) for variant in ("baseline", "candidate")], (
        "sealed-02 failed in every slot, and the filed record says so"
    )
    assert filed.verdict == "rejected", "equal success on both arms is no gain"
    assert filed.verdict_reasons == ("no task-success gain",)
    assert (filed.baseline_success_rate, filed.candidate_success_rate) == (0.5, 0.5)
    assert filed.regressions == ()
    assert filed.baseline_usage is not None
    assert filed.candidate_usage is not None
    assert filed.baseline_usage.requests == 8, "four baseline slots, each an arm request and a grader request"


def _owner_review(review_id: str, run_id: str, outcome: Literal["accepted", "rejected"]) -> OwnerReview:
    request = OwnerReviewRequest(
        review_id=review_id,
        actor="owner",
        outcome=outcome,
        reason=f"the owner {outcome} this run",
        artifact_refs=(
            ArtifactRef(id="page", label="The page", run_id=run_id, node_id="filed", value_digest="a" * 64, media_type="text/markdown"),
        ),
    )
    return OwnerReview(
        **request.model_dump(),
        source="tests",
        at=BENCH_START,
        request_fingerprint=owner_review_request_fingerprint(run_id, "tests", request),
    )


@dataclass(frozen=True)
class _PreflightRow:
    """One row of the preflight table: the condition it creates, and the behavior it then requires."""

    condition: str
    arrange: Callable[[_Bench], Awaitable[None]]
    expect: Callable[[_Bench, OutcomeStateReport], Awaitable[None]]


PREFLIGHT_TABLE = (
    _PreflightRow("development dataset only", _development_only, _expect_development_only),
    _PreflightRow("seal mismatch or author exposure", _seal_mismatch_and_author_exposure, _expect_seal_mismatch_and_author_exposure),
    _PreflightRow("non-test model lacks any positive limit", _live_model_without_limits, _expect_live_model_without_limits),
    _PreflightRow("live model price unknown", _live_model_without_a_price, _expect_live_model_without_a_price),
    _PreflightRow("trial or cycle dollar stop threshold already reached", _both_thresholds_already_reached, _expect_both_thresholds_already_reached),
    _PreflightRow("a crossing call exceeds the trial or cycle threshold", _a_crossing_call, _expect_a_crossing_call),
    _PreflightRow("trial reaches 180 seconds, 16 requests or 3 attempts", _hard_trial_limits, _expect_hard_trial_limits),
    _PreflightRow("response charge is uncertain", _an_uncertain_charge, _expect_an_uncertain_charge),
    _PreflightRow("grader makes a model request", _the_grader_makes_a_request, _expect_the_grader_makes_a_request),
    _PreflightRow("model or grader identity drifts", _identity_drift, _expect_identity_drift),
    _PreflightRow("complete sealed run", _a_complete_sealed_run, _expect_a_complete_sealed_run),
)
"""The eleven conditions a governed outcome benchmark is defined by, and what each one requires of it."""


@pytest.mark.parametrize("row", PREFLIGHT_TABLE, ids=[row.condition for row in PREFLIGHT_TABLE])
async def test_the_preflight_table(row: _PreflightRow, tmp_path: Path) -> None:
    """Each row arranges exactly its own condition and then asserts what the command must do about it.

    Everything a row does not arrange is a legitimate sealed benchmark on stand-in models, so a row
    that passes has isolated its own condition rather than met a refusal something else caused.
    """
    bench = _Bench(tmp_path)

    await row.arrange(bench)
    report = await bench.run()

    await row.expect(bench, report)
    assert (bench.reports / f"{report.report_id}.json").is_file(), "every sitting files its own report, refused or not"


async def test_a_failed_candidate_run_with_a_glowing_summary_does_not_pass(tmp_path: Path) -> None:
    """The grader judges text, and the run's own checked status decides whether its pass can stand."""
    bench = _Bench(tmp_path)
    bench.candidate_checked = "failed"

    report = await bench.run()

    assert report.empirical_status == "measured"
    grader = bench.arms.grader if bench.arms is not None else None
    assert isinstance(grader, ScriptedGrader)
    assert grader.graded == ["sealed-01"] * 4, "the grader was asked for every slot and passed every one"
    for repetition in (0, 1):
        candidate = bench.slot(report, "sealed-01", repetition, "candidate").grade
        assert candidate is not None
        assert candidate.success is False, "a failed run cannot pass however well its summary reads"
        assert "checked status is failed" in candidate.reason
        baseline = bench.slot(report, "sealed-01", repetition, "baseline").grade
        assert baseline is not None
        assert baseline.success is True, "the baseline carries no checked status, so its grade stands"
    assert report.candidate_success_rate == 0.0
    assert report.regressions == ("sealed-01",)
    assert report.verdict == "rejected"


async def test_a_model_path_nobody_mounted_refuses_the_benchmark(tmp_path: Path) -> None:
    """The refusal is over the objects the composer handed back, never over a claim about them.

    The composer here declares the same three paths and hands back a baseline model with nothing on
    it. Nothing was said about metering either way, and the preflight still finds the gap.
    """
    bench = _Bench(tmp_path)
    bench.unmetered_baseline = True

    report = await bench.run()

    assert _refusals(report) == ["unmetered_model_path"]
    assert "baseline:double" in report.refusals[0].detail
    assert _arm(bench, "baseline").slots == [], "nothing ran outside the record"
    assert await bench.cycle_state() is None


async def test_a_temporal_deployment_on_another_host_refuses_the_benchmark(tmp_path: Path) -> None:
    """A worker this process never composed makes requests no record of ours can see."""
    bench = _Bench(tmp_path)
    bench.run_engine = "temporal"
    bench.temporal_address = "temporal.example.internal:7233"

    report = await bench.run()

    assert _refusals(report) == ["remote_temporal_worker"]
    assert "another host" in report.refusals[0].detail
    assert await bench.cycle_state() is None


async def test_a_local_temporal_deployment_is_not_refused_for_being_temporal(tmp_path: Path) -> None:
    """The rule is about where the workers are, not about which engine runs them."""
    bench = _Bench(tmp_path)
    bench.run_engine = "temporal"

    report = await bench.run()

    assert "remote_temporal_worker" not in _refusals(report)
    assert report.empirical_status == "measured"


async def test_comparing_one_identity_with_itself_is_refused(tmp_path: Path) -> None:
    """Two arms carrying one identity is not a comparison, and a slot per arm would collide."""
    bench = _Bench(tmp_path)
    bench.identical_arms = True

    report = await bench.run()

    assert _refusals(report) == ["arms_identical"]
    assert await bench.cycle_state() is None


async def test_a_report_is_written_once_and_a_second_sitting_cannot_take_its_name(tmp_path: Path) -> None:
    """An immutable record that can be rewritten records nothing, so the second write is refused."""
    bench = _Bench(tmp_path)
    report = await bench.run()

    with pytest.raises(FileExistsError):
        file_report(bench.reports, report)

    assert (bench.reports / f"{report.report_id}.json").read_text(encoding="utf-8").count('"report_id"') == 1


async def test_a_benchmark_whose_sealed_cases_are_not_on_disk_writes_a_blocked_report(tmp_path: Path) -> None:
    """Missing legitimate cases is the designed path: a blocked report, and no paid step at all."""
    bench = _Bench(tmp_path)
    bench.cases = SealedComparison(dataset_path=tmp_path / "nothing.json", custody_path=tmp_path / "no-custody.json")

    report = await bench.run()

    assert _refusals(report) == ["custody_absent"]
    assert report.empirical_status == "blocked"
    assert report.custody is None, "there was no custody record to audit"
    assert bench.composed_calls == 0
    assert await bench.cycle_state() is None


async def test_every_filed_slot_is_on_disk_before_a_verdict_is_folded(tmp_path: Path) -> None:
    """The journal is the record a verdict is folded from, so it is complete before the fold."""
    bench = _Bench(tmp_path)

    report = await bench.run()

    journal = bench.reports / f"{report.report_id}.trials"
    filed = sorted(path.name for path in journal.glob("*.json"))
    assert filed == ["sealed-01-0-baseline.json", "sealed-01-0-candidate.json", "sealed-01-1-baseline.json", "sealed-01-1-candidate.json"]
    assert len(report.trials) == len(filed), "the report carries exactly what the journal holds"


async def test_arms_that_ask_a_model_the_benchmark_never_declared_are_refused(tmp_path: Path) -> None:
    """Classifying the declared objects proves nothing unless the arms ask through those very objects.

    Here every declared path is correctly mounted and would classify clean, and the arms ask through
    a separate model nothing mounted. Reading only the declaration reports a live measured benchmark
    with every slot at zero requests while real requests go out unmetered.
    """
    bench = _Bench(tmp_path)
    bench.live_models = True
    bench.baseline_over = BenchmarkModel(path="baseline:elsewhere", role="worker", model=bench.a_model())

    report = await bench.run()

    assert _refusals(report) == ["undeclared_model_path"]
    assert "baseline:elsewhere is requested through and was never declared" in report.refusals[0].detail
    assert "baseline:double is declared and no arm requests through it" in report.refusals[0].detail
    assert report.empirical_status == "blocked"
    assert _arm(bench, "baseline").slots == [], "no request was made outside the record"
    assert await bench.cycle_state() is None


async def test_a_composition_that_declares_no_model_at_all_is_refused(tmp_path: Path) -> None:
    """An empty declaration is a composer that lost a path, and it must not read as a stand-in.

    Every benchmark makes at least the grader's request, so nothing to classify means the classifier
    was handed nothing, not that nothing reaches a provider.
    """
    bench = _Bench(tmp_path)
    bench.live_models = True
    bench.limits = None
    bench.declared_models = ()

    report = await bench.run()

    assert _refusals(report) == ["no_declared_model_path"]
    assert model_type_of(()) == "live", "nothing declared fails closed rather than reading as a stand-in"
    assert report.model_kind == "live"
    assert _arm(bench, "baseline").slots == [], "nothing ran under a benchmark that declared nothing"
    assert await bench.cycle_state() is None


async def test_an_arm_that_asks_through_a_model_the_record_never_saw_is_refused(tmp_path: Path) -> None:
    """Naming the declared path and asking somewhere else is the one thing a statement cannot catch.

    The arm's own request count is honest, because a real runner takes it from whatever made the
    requests. Held against what the record admitted for that trial, the gap is readable from outside
    the arm, and it is the only thing about behaviour that is.
    """
    bench = _Bench(tmp_path)
    bench.rogue_baseline = bench.a_model()

    report = await bench.run()

    assert [note.refusal for note in report.refusals] == ["unrecorded_model_requests"]
    assert "the baseline arm reports 2 model request(s) and this record admitted 0" in report.refusals[0].detail
    assert report.empirical_status == "inconclusive", "a sitting that spent where nothing could see is never measured"
    assert "sealed-01" in report.ungraded_case_ids
    assert bench.slot(report, "sealed-01", 0, "baseline").failure != "", "the slot is filed with what refused it"


async def test_a_grader_that_asks_through_a_model_the_record_never_saw_is_refused(tmp_path: Path) -> None:
    """The same reading of the grader, whose requests belong to the trial it graded."""
    bench = _Bench(tmp_path)
    bench.rogue_grader = bench.a_model()

    report = await bench.run()

    assert [note.refusal for note in report.refusals] == ["unrecorded_model_requests"]
    assert "the grader reports 1 model request(s) and this record admitted 0" in report.refusals[0].detail
    assert report.empirical_status == "inconclusive"
    state = await bench.cycle_state()
    assert state is not None
    assert [row.admission.role for row in state.requests] == ["worker"], "one arm request reached the record, and the run stopped there"


async def test_a_composition_that_declares_no_grader_path_is_refused(tmp_path: Path) -> None:
    """The case that needs nobody to lie, and the repository invites it by design.

    Both arms are declared and correctly mounted. The grader is a custodian's own object that says,
    truthfully, that it asks through none of the declared paths and reports another record's figures,
    and it builds its own model. Every statement is accurate and every other rule passes, so the
    grader itself has to be asked whether it stands behind a declared grader-role path.
    """
    bench = _Bench(tmp_path)
    bench.rogue_grader = bench.a_model()
    bench.grader_declares = ()
    bench.grader_source = "another_record"
    bench.declare_grader = False

    report = await bench.run()

    assert _refusals(report) == ["no_declared_grader_path"]
    assert "does not ask through any declared grader-role path" in report.refusals[0].detail
    assert report.empirical_status == "blocked"
    assert await bench.cycle_state() is None, "nothing opened, so the grader never spent at all"


async def test_declaring_nothing_must_say_whose_record_measured_it(tmp_path: Path) -> None:
    """The cheapest way past every check costs no lie at all, so the exemption has to be claimed.

    One declaring no path is exempt from the reading that holds its own figures against this record.
    That exemption is real and the shipped workflow arm needs it. One that declares nothing while
    claiming this record's figures would only be read after its first slot had already spent, so it
    is refused before any slot runs.
    """
    bench = _Bench(tmp_path)
    bench.candidate_declares_nothing = True

    report = await bench.run()

    assert _refusals(report) == ["unstated_usage_source"]
    assert "the candidate declares no model path and does not report another record's usage" in report.refusals[0].detail
    assert report.empirical_status == "blocked"
    assert await bench.cycle_state() is None


async def test_declared_paths_and_another_records_figures_cannot_both_be_claimed(tmp_path: Path) -> None:
    """The two states are the whole of what one may be, and nothing sits between them."""
    bench = _Bench(tmp_path)
    bench.rogue_grader = bench.a_model()
    bench.grader_source = "another_record"

    report = await bench.run()

    assert _refusals(report) == ["unstated_usage_source"]
    assert "declares model paths and reports another record's usage" in report.refusals[0].detail
    assert await bench.cycle_state() is None


async def test_a_grader_behind_no_declared_grader_path_is_refused_however_the_arms_declare(tmp_path: Path) -> None:
    """The rule asks the grader, not the declaration, so an extra entry on an arm cannot satisfy it.

    Here the declaration carries a mounted grader-role path and an arm names it, so a rule that only
    read the declaration would pass. The grader stands behind none of it.
    """
    bench = _Bench(tmp_path)
    bench.rogue_grader = bench.a_model()
    bench.grader_declares = ()
    bench.grader_source = "another_record"

    report = await bench.run()

    assert _refusals(report) == ["no_declared_grader_path"]
    assert await bench.cycle_state() is None, "nothing opened, so the grader never spent at all"


async def test_an_arm_that_spends_and_then_fails_is_read_like_one_that_returns(tmp_path: Path) -> None:
    """One raise would otherwise skip the reading entirely, which is a third way past it for free.

    The failure carries the usage spent before it, which is what the failure path is for, and it is
    read against the record the same way a returned execution is.
    """
    bench = _Bench(tmp_path)
    bench.rogue_baseline = bench.a_model()
    bench.baseline_raises = "the provider hung up"

    report = await bench.run()

    assert [note.refusal for note in report.refusals] == ["unrecorded_model_requests"]
    assert "the baseline arm reports 2 model request(s) and this record admitted 0" in report.refusals[0].detail
    assert report.empirical_status == "inconclusive", "a sitting that spent where nothing could see is never measured"


async def test_a_grader_that_spends_and_then_fails_is_read_like_one_that_returns(tmp_path: Path) -> None:
    """The same reading on the grader's own failure path."""
    bench = _Bench(tmp_path)
    bench.rogue_grader = bench.a_model()
    bench.grader_raises = "the judge hung up"

    report = await bench.run()

    assert [note.refusal for note in report.refusals] == ["unrecorded_model_requests"]
    assert "the grader reports 1 model request(s) and this record admitted 0" in report.refusals[0].detail
    assert report.empirical_status == "inconclusive"


async def test_a_refusal_on_the_last_slot_still_keeps_the_sitting_off_measured(tmp_path: Path) -> None:
    """Every case can be graded from another slot and the sitting still is not a measurement.

    The refusal fires on the final slot, so nothing is left ungraded and the only thing separating
    this report from `measured` is the refusal itself.
    """
    bench = _Bench(tmp_path)
    bench.grader_trips_on_slot = 3

    report = await bench.run()

    assert report.ungraded_case_ids == (), "every case was graded by an earlier slot"
    assert [note.refusal for note in report.refusals] == ["unrecorded_model_requests"]
    assert report.empirical_status == "inconclusive", "a refusal alone keeps a sitting off measured"
