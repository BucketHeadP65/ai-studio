"""The step maintenance steps list a changed step's consumers, check them, run its cases, and land, report and hand over the plan.

The library is a `LibraryCollection` over `FakeCollection`, holding versions of the reusable step `digest` and the
consumers each test saves. The evidence log is a real file under the test's own directory. Case runs go through a
`FakeCaseRunner`, and the owner's notices through a `FakeOwnerNotifier`.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING

import pytest

from core.ai.workflows.contracts import (
    Briefing,
    FileEvidenceLog,
    FromNode,
    FromParameter,
    LibraryCollection,
    LiveTrialMarker,
    LiveTrialStep,
    MaintenanceFlag,
    Outcome,
    ParameterSpec,
    Prediction,
    RunEvidence,
    RunNode,
    Shape,
    StepRegistry,
    Workflow,
    WorkflowIntent,
    WorkflowPresentation,
    adapt,
    canonical_step_yaml,
    spec_identity,
    step_content_address,
    trust_across_versions,
    version_hashes,
)

from harness.domain.contracts import CaseReport, CaseResult, cases_address
from harness.features.evals.contracts import CaseRunRefusedError
from harness.features.evals.infrastructure.case_reports import FileCaseReports
from harness.features.maintenance.contracts import (
    STEP_MAINTENANCE_AGENT,
    ConsumerSide,
    DraftedFile,
    LandedChange,
    LandingRefusedError,
    LandingReport,
    MaintenancePlan,
    MaintenanceSteps,
    PlanDecision,
    PlanDecisions,
    PlannedChange,
    RepositoryChange,
    RepositoryVerdict,
    one_line,
)
from harness.features.maintenance.wiring import StepCaseRuns
from tests.support.fakes.case_runner import FakeCaseRunner
from tests.support.fakes.library_collection import FakeCollection
from tests.support.fakes.owner_notifier import FakeOwnerNotifier

if TYPE_CHECKING:
    from collections.abc import Mapping
    from pathlib import Path

    from core.ai.workflows.contracts import Lineage
    from pydantic import JsonValue

    from harness.adapters.contracts import StandInCalls

T0 = datetime(2026, 11, 6, 9, 0, tzinfo=UTC)
TEXT = Shape(json_schema={"type": "string"})
INSTRUCTIONS = {
    2: "Read the whole source in the style asked for and write one digest page about it.",
    3: "Read the whole source and write one digest page about it.",
    4: "Read the whole source and write one digest page that quotes each claim it makes.",
}
DIGEST = {
    2: Briefing(name="digest", instructions=INSTRUCTIONS[2], inputs={"text": TEXT, "style": TEXT}, output=TEXT),
    3: Briefing(name="digest", instructions=INSTRUCTIONS[3], inputs={"text": TEXT}, output=TEXT),
    4: Briefing(name="digest", instructions=INSTRUCTIONS[4], inputs={"text": TEXT}, output=TEXT),
    5: Briefing(name="digest", instructions="Read the whole source and write one digest page that quotes and dates each claim.", inputs={"text": TEXT}, output=TEXT),
}
SUMMARIZE = {
    1: Briefing(name="summarize", instructions="Summarize the digest page in three sentences for the weekly note.", inputs={"text": TEXT}, output=TEXT),
    2: Briefing(name="summarize", instructions="Summarize the digest page in two sentences that name every claim.", inputs={"text": TEXT}, output=TEXT),
}
V4 = "st.digest.v4"
HASH4 = step_content_address(DIGEST[4])
SUMMARIZE2 = "st.summarize.v2"
SUMMARIZE2_HASH = step_content_address(SUMMARIZE[2])
PRESENTATION = WorkflowPresentation(purpose="One weekly digest page per source.", produces="A digest page the owner reviews.")
INTENT = WorkflowIntent(request="Digest each week's sources into one page each.")
CASES: Mapping[str, JsonValue] = {"cases": [{"name": "quotes", "inputs": {"text": "A source."}}]}


async def _perform(task: Briefing, output_type: type, /, **inputs: object) -> object:
    """Nothing here performs a step: the registry only builds and validates them."""
    del task, output_type, inputs
    return "a digest page"


def consumer(name: str, step: Briefing, *, also: Briefing | None = None) -> Workflow:
    """A workflow whose node `digest` runs its copy of `step` on parameters of the same names, then `summarize` on it."""
    nodes: list[RunNode] = [RunNode(id="digest", work="digest", inputs={slot: FromParameter(parameter=slot) for slot in step.inputs})]
    if also is not None:
        nodes.append(RunNode(id="summarize", work="summarize", inputs={"text": FromNode(node="digest")}))
    return Workflow(
        name=name,
        version=1,
        description=f"Digest one source into {name}.",
        parameters={slot: ParameterSpec(description=f"the source's {slot}", json_schema={"type": "string"}) for slot in step.inputs},
        inline=(step,) if also is None else (step, also),
        nodes=tuple(nodes),
        output=FromNode(node=nodes[-1].id),
    )


class World:
    """The library, its pages, the evidence log, the case runner, the owner's notices and the steps over all of them."""

    def __init__(self, tmp_path: Path) -> None:
        self.store = FakeCollection(prune=True)
        self.library = LibraryCollection(self.store, self.store.ingest, collection=self.store.collection)
        self.log = FileEvidenceLog(tmp_path / "evidence.jsonl")
        self.notifier = FakeOwnerNotifier()
        self.cases = FakeCaseRunner()
        self.steps = MaintenanceSteps(
            library=self.library,
            history=self.log,
            notifier=self.notifier,
            cases=self.cases,
            clock=lambda: T0,
            environment={"harness": "under test"},
            current_run=lambda: "run-7",
        )
        self.registry = StepRegistry(perform=_perform)
        self.steps.register(self.registry)

    async def save_steps(self, *versions: int, cases: Mapping[int, Mapping[str, JsonValue]] | None = None) -> None:
        """Save these versions of `digest`, lowest first, each with the cases `cases` names for it."""
        for version in versions:
            await self.library.put_step(
                DIGEST[version],
                version=version,
                title="Digest",
                summary="One digest page per source.",
                applicability="Any source.",
                provenance={"saved_by": "owner:cli"},
                cases=(cases or {}).get(version),
            )

    async def save_summarize(self) -> None:
        """Save both versions of `summarize`."""
        for version, step in SUMMARIZE.items():
            await self.library.put_step(step, version=version, title="Summarize", summary="s", applicability="a", provenance={"saved_by": "owner:cli"})

    async def save(
        self,
        workflow: Workflow,
        *,
        lineage: Lineage | None = None,
        presentation: WorkflowPresentation | None = None,
        intent: WorkflowIntent | None = None,
    ) -> str:
        """Save a consumer as the owner would, titled after its name."""
        return await self.library.put(
            workflow,
            title=f"{workflow.name} title",
            summary=f"{workflow.name} summary",
            applicability=f"Use {workflow.name} for one source.",
            provenance={"saved_by": "owner:cli"},
            lineage=lineage,
            presentation=presentation,
            intent=intent,
        )

    async def plan(self, changed: str, decisions: Mapping[str, tuple[str, str]]) -> MaintenancePlan:
        """The plan `prepare_plan` drafts for `changed`, with the disposition and reason `decisions` give each consumer."""
        consumers = await self.steps.list_consumers(changed)
        draft = await self.steps.prepare_plan(consumers, await self.steps.check_contracts(consumers), await self.steps.run_step_cases(changed))
        return draft.model_copy(
            update={
                "changes": [
                    change.model_copy(update={"disposition": decisions[change.workflow_id][0], "reason": decisions[change.workflow_id][1]})
                    for change in draft.changes
                    if change.workflow_id in decisions
                ]
            }
        )


def _ran(workflow: Workflow, workflow_id: str, *, at: datetime) -> RunEvidence:
    return RunEvidence.model_validate({
        "workflow_id": workflow_id,
        "spec_hash": spec_identity(workflow),
        "workflow_version": workflow.version,
        "outcome": "completed",
        "result": Outcome(status="succeeded", summary="one digest page written"),
        "gates_auto": (),
        "gates_orchestrated": (),
        "produced": {"digest": "str"},
        "runner": "app:schedule:7",
        "at": at,
    })


def _landed(workflow_id: str, outcome: str, reason: str, *, saved_id: str = "", step_id: str = V4) -> LandedChange:
    disposition = {"adopted": "adopt", "kept": "keep", "flagged": "flag", "refused": "adopt"}[outcome]
    return LandedChange.model_validate({
        "workflow_id": workflow_id,
        "step_id": step_id,
        "disposition": disposition,
        "outcome": outcome,
        "reason": reason,
        "saved_id": saved_id,
        "notes": [],
    })


def _decided(plan: MaintenancePlan) -> PlanDecisions:
    """What a planner would answer for `plan`: each change's disposition, reason and further edits, by its ids."""
    return PlanDecisions(
        decisions=[
            PlanDecision(
                workflow_id=change.workflow_id,
                step_id=change.step_id,
                disposition=change.disposition,
                reason=change.reason,
                extra_ops=change.extra_ops,
            )
            for change in plan.changes
        ]
    )


async def _land(world: World, plan: MaintenancePlan) -> LandingReport:
    """Land `plan` with the planner's decisions taken from its own changes, `plan` standing in for the drafted plan."""
    return await world.steps.land_changes(plan, _decided(plan))


async def test_list_consumers_answers_each_behind_pair_with_its_diff_goal_and_trust_and_drops_a_declined_one(tmp_path: Path) -> None:
    world = World(tmp_path)
    await world.save_steps(3, 4)
    first = consumer("digest-sources", DIGEST[3])
    second, lineage = adapt(first, ops=[{"op": "set_description", "to": "Digest one source, briefly."}])
    await world.save(first)
    sources_id = await world.save(second, lineage=lineage)
    weekly_id = await world.save(consumer("weekly-digest", DIGEST[3]), presentation=PRESENTATION, intent=INTENT)
    declined_id = await world.save(consumer("daily-digest", DIGEST[3]))
    await world.library.record_decline(declined_id, step="digest", step_hash=HASH4, because="kept")
    current_id = await world.save(consumer("current-digest", DIGEST[4]))
    for hour in range(2):
        await world.log.append(_ran(first, "wf.digest-sources.v1", at=T0 + timedelta(hours=hour)))
    await world.log.append(_ran(second, sources_id, at=T0 + timedelta(hours=3)))

    listed = await world.steps.list_consumers(V4)

    assert [pair.workflow_id for pair in listed.pairs] == [sources_id, weekly_id]
    assert (sorted(listed.dropped), listed.left_for_next_run) == (sorted([declined_id, current_id]), []), "each dropped pair is named"
    sources, weekly = listed.pairs
    for pair in listed.pairs:
        assert (pair.state, pair.step, pair.step_id, pair.step_hash, pair.held_step_id, pair.nodes) == (
            "behind", "digest", V4, HASH4, "st.digest.v3", ["digest"],
        )
        assert any(line.startswith("-") and INSTRUCTIONS[3] in line for line in pair.diff.splitlines())
        assert any(line.startswith("+") and INSTRUCTIONS[4] in line for line in pair.diff.splitlines())
    assert (sources.trust_level, sources.trust.runs_with_evidence, sources.total_trust.runs_with_evidence) == ("machine-confirmed", 1, 3)
    assert (weekly.trust_level, weekly.trust.runs_with_evidence, weekly.total_trust.runs_with_evidence) == ("unverified", 0, 0)
    assert (weekly.presentation, weekly.intent, weekly.title) == (PRESENTATION, INTENT, "weekly-digest title")
    assert (sources.presentation, sources.intent) == (None, None)


async def test_list_consumers_takes_twenty_pairs_and_names_the_rest_for_the_next_run(tmp_path: Path) -> None:
    world = World(tmp_path)
    await world.save_steps(3, 4)
    ids = [await world.save(consumer(f"digest-{index:02}", DIGEST[3])) for index in range(21)]

    listed = await world.steps.list_consumers(V4)

    assert [pair.workflow_id for pair in listed.pairs] == ids[:20]
    assert listed.left_for_next_run == ids[20:]


async def test_a_step_named_twice_is_listed_once_at_its_highest_version(tmp_path: Path) -> None:
    world = World(tmp_path)
    await world.save_steps(2, 3, 4)
    styled_id = await world.save(consumer("styled-digest", DIGEST[2]))

    listed = await world.steps.list_consumers(f"{V4}, st.digest.v3")

    assert [(pair.workflow_id, pair.step_id, pair.held_step_id) for pair in listed.pairs] == [(styled_id, V4, "st.digest.v2")]


async def test_check_contracts_passes_a_consumer_that_fits_and_fails_one_wiring_an_input_the_new_version_dropped(tmp_path: Path) -> None:
    world = World(tmp_path)
    await world.save_steps(2, 3, 4)
    fits_id = await world.save(consumer("digest-sources", DIGEST[3]))
    styled_id = await world.save(consumer("styled-digest", DIGEST[2]))

    checks = await world.steps.check_contracts(await world.steps.list_consumers(V4))

    by_consumer = {check.workflow_id: check for check in checks}
    assert (by_consumer[fits_id].passed, by_consumer[fits_id].issues, by_consumer[fits_id].step_id) == (True, [], V4)
    assert not by_consumer[styled_id].passed
    assert any("style" in issue for issue in by_consumer[styled_id].issues), by_consumer[styled_id].issues


async def test_run_step_cases_runs_the_new_version_and_each_held_version_each_over_its_own_stand_in_list(tmp_path: Path) -> None:
    world = World(tmp_path)
    await world.save_steps(3, 4)
    await world.save(consumer("digest-sources", DIGEST[3]))
    await world.save(consumer("weekly-digest", DIGEST[3]))

    reports = await world.steps.run_step_cases(V4)

    assert [report.step_id for report in reports] == [V4, "st.digest.v3"]
    assert world.cases.performed == [V4, "st.digest.v3"]
    first, second = world.cases.lists
    assert first is not second


async def test_a_case_file_that_cannot_run_answers_a_failed_report_naming_why_and_the_other_steps_still_run(tmp_path: Path) -> None:
    world = World(tmp_path)
    await world.save_steps(3, 4, cases={4: CASES})
    await world.save(consumer("digest-sources", DIGEST[3]))
    world.cases.refusals[V4] = "the cases of st.digest.v4 cannot run: more than one case is named 'quotes'"

    reports = await world.steps.run_step_cases(V4)

    refused, held = reports
    assert (refused.step_id, refused.step_hash, refused.results, refused.no_cases) == (V4, HASH4, (), False)
    assert refused.refused == world.cases.refusals[V4]
    assert held.step_id == "st.digest.v3"
    assert world.cases.performed == ["st.digest.v3"]


async def test_a_case_run_the_registry_cannot_perform_fails_the_step(tmp_path: Path) -> None:
    world = World(tmp_path)
    await world.save_steps(3, 4)
    world.cases.failure = CaseRunRefusedError("the cases of st.digest.v4 cannot run: this registry performs no briefing")

    with pytest.raises(CaseRunRefusedError, match="performs no briefing"):
        await world.steps.run_step_cases(V4)


async def test_step_case_runs_answer_a_case_file_s_refusal_and_raise_for_a_registry_that_cannot_perform(tmp_path: Path) -> None:
    built: list[StandInCalls] = []

    async def without_a_performer(calls: StandInCalls) -> StepRegistry:
        built.append(calls)
        return StepRegistry()

    runs = StepCaseRuns(without_a_performer, FileCaseReports(tmp_path / "reports"))
    twice: Mapping[str, JsonValue] = {"cases": [{"name": "quotes", "inputs": {"text": "a"}}, {"name": "quotes", "inputs": {"text": "b"}}]}
    calls: StandInCalls = []

    refusal = runs.refusal(V4, twice)

    assert refusal is not None
    assert "more than one case is named 'quotes'" in refusal
    assert runs.refusal(V4, CASES) is None
    with pytest.raises(CaseRunRefusedError, match="performs no briefing"):
        await runs.perform(DIGEST[4], V4, CASES, calls)
    assert len(built) == 1
    assert built[0] is calls


async def test_prepare_plan_answers_one_undecided_keep_per_pair_carrying_its_check_and_its_case_reports(tmp_path: Path) -> None:
    world = World(tmp_path)
    await world.save_steps(3, 4)
    sources_id = await world.save(consumer("digest-sources", DIGEST[3]))
    weekly_id = await world.save(consumer("weekly-digest", DIGEST[3]))
    passed = CaseReport(
        step_id=V4,
        step_hash=HASH4,
        cases_hash="c4",
        results=(CaseResult(name="quotes", passed=True, errored=False, reason="every claim quoted"),),
        blocked_by=None,
        no_cases=False,
        stand_in_calls=1,
        refused=None,
    )
    world.cases.reports[V4] = passed
    consumers = await world.steps.list_consumers(V4)
    checks = await world.steps.check_contracts(consumers)
    cases = await world.steps.run_step_cases(V4)

    plan = await world.steps.prepare_plan(consumers, checks, cases)

    assert [(change.workflow_id, change.step_id, change.disposition, change.reason) for change in plan.changes] == [
        (sources_id, V4, "keep", "not decided"),
        (weekly_id, V4, "keep", "not decided"),
    ]
    assert [change.check for change in plan.changes] == checks
    for change in plan.changes:
        assert change.cases == [passed, cases[1]]
        assert change.extra_ops == ()
    assert plan.left_for_next_run == []


async def test_land_changes_adopts_on_live_trial_declines_a_kept_copy_flags_a_flagged_one_and_resolves_the_adopted_flag(tmp_path: Path) -> None:
    world = World(tmp_path)
    await world.save_steps(3, 4)
    sources_id = await world.save(consumer("digest-sources", DIGEST[3], also=SUMMARIZE[1]), presentation=PRESENTATION, intent=INTENT)
    weekly_id = await world.save(consumer("weekly-digest", DIGEST[3]))
    daily_id = await world.save(consumer("daily-digest", DIGEST[3]))
    earlier = MaintenanceFlag(
        kind="consumer_flag",
        step="digest",
        step_id=V4,
        step_hash=HASH4,
        workflow=sources_id,
        title="Consumer flag",
        summary="Decide whether digest-sources adopts digest v4.",
        evidence="An earlier run flagged it.",
        raised_by=STEP_MAINTENANCE_AGENT,
        raised_at=T0 - timedelta(days=1),
    )
    await world.library.put_flag(earlier)
    plan = await world.plan(
        V4,
        {
            sources_id: ("adopt", "It fits and quotes every claim."),
            weekly_id: ("keep", "The weekly digest keeps its own tone."),
            daily_id: ("flag", "Only the owner can say whether the daily digest should quote claims."),
        },
    )

    landed = await _land(world, plan)

    outcomes = {change.workflow_id: (change.outcome, change.saved_id) for change in landed.changes}
    assert outcomes == {sources_id: ("adopted", "wf.digest-sources.v2"), weekly_id: ("kept", ""), daily_id: ("flagged", "")}
    adopted = "wf.digest-sources.v2"
    assert world.store.written(adopted)["saved_by"] == STEP_MAINTENANCE_AGENT
    assert await world.library.fetch_live_trial(adopted) == LiveTrialMarker(previous=sources_id, steps=(LiveTrialStep(step="digest", step_hash=HASH4),))
    lineage = await world.library.fetch_lineage(adopted)
    assert lineage is not None
    assert (lineage.parent_name, lineage.parent_version, lineage.touched, lineage.predictions) == (
        "digest-sources", 1, ("digest",), (Prediction(claim="completes"),),
    )
    assert "run-7" in lineage.evidence
    child = await world.library.fetch_workflow(adopted)
    assert [step_content_address(step) for step in child.inline] == [HASH4, step_content_address(SUMMARIZE[1])]
    assert await world.library.current(sources_id) == adopted
    card = await world.library.fetch_card(adopted)
    assert card is not None
    assert (card.title, card.presentation, card.intent) == ("digest-sources title", PRESENTATION, INTENT)
    fingerprint = await world.library.fetch_fingerprint(adopted)
    assert fingerprint is not None
    assert fingerprint.environment == {"harness": "under test"}
    (kept,) = await world.library.fetch_consumer_record(weekly_id)
    assert (kept.declined, kept.declined_because) == (HASH4, "kept")
    flag = await world.library.fetch_flag("mf.consumer.daily-digest.digest")
    assert flag is not None
    assert (flag.kind, flag.status, flag.workflow, flag.step_id, flag.step_hash) == ("consumer_flag", "open", daily_id, V4, HASH4)
    assert (flag.raised_by, flag.raised_at) == (STEP_MAINTENANCE_AGENT, T0)
    assert "Only the owner can say" in flag.evidence
    assert "contract check passed" in flag.evidence
    resolved = await world.library.fetch_flag("mf.consumer.digest-sources.digest")
    assert resolved is not None
    assert (resolved.status, resolved.resolved_by) == ("resolved", STEP_MAINTENANCE_AGENT)
    assert adopted in resolved.decision_reason


async def test_a_planner_s_reason_holding_headings_and_double_percent_signs_still_lands_on_the_flag_page(tmp_path: Path) -> None:
    world = World(tmp_path)
    await world.save_steps(3, 4)
    daily_id = await world.save(consumer("daily-digest", DIGEST[3]))
    reason = "## Why\n\nThe daily tone matters.\nIt is plain\n---\n\nThe owner is 100%% sure of nothing yet, %% says the note."
    plan = await world.plan(V4, {daily_id: ("flag", reason)})

    landed = await _land(world, plan)

    assert [change.outcome for change in landed.changes] == ["flagged"]
    flag = await world.library.fetch_flag("mf.consumer.daily-digest.digest")
    assert flag is not None
    assert "## Why" in flag.evidence
    assert "It is plain\n---" in flag.evidence
    assert "100% % sure" in flag.evidence
    assert "%%" not in flag.evidence


async def test_land_changes_lands_every_other_adoption_then_fails_naming_the_consumer_whose_save_was_refused(tmp_path: Path) -> None:
    world = World(tmp_path)
    await world.save_steps(3, 4)
    sources_id = await world.save(consumer("digest-sources", DIGEST[3]))
    weekly_id = await world.save(consumer("weekly-digest", DIGEST[3]))
    plan = await world.plan(V4, {sources_id: ("adopt", "It fits."), weekly_id: ("adopt", "It fits too.")})
    world.store.refused_ids = frozenset({"wf.weekly-digest.v2"})

    with pytest.raises(LandingRefusedError) as raised:
        await _land(world, plan)

    assert weekly_id in str(raised.value)
    assert "and landed the rest" in str(raised.value)
    assert "wf.digest-sources.v2" in str(raised.value)
    assert "refused the page wf.weekly-digest.v2" in str(raised.value)
    assert await world.library.current(sources_id) == "wf.digest-sources.v2"
    assert await world.library.current(weekly_id) == weekly_id
    refused = {change.workflow_id: change for change in raised.value.report.changes}[weekly_id]
    assert refused.outcome == "refused"
    assert "refused the page wf.weekly-digest.v2" in refused.reason


async def test_a_planned_change_naming_no_step_in_the_library_is_refused_with_its_reason_and_the_others_land(tmp_path: Path) -> None:
    world = World(tmp_path)
    await world.save_steps(3, 4)
    sources_id = await world.save(consumer("digest-sources", DIGEST[3]))
    weekly_id = await world.save(consumer("weekly-digest", DIGEST[3]))
    plan = await world.plan(V4, {sources_id: ("adopt", "It fits."), weekly_id: ("adopt", "It fits too.")})
    unknown = plan.changes[0].model_copy(update={"step_id": "st.digest.v9"})
    plan = plan.model_copy(update={"changes": [unknown, plan.changes[1]]})

    with pytest.raises(LandingRefusedError) as raised:
        await _land(world, plan)

    refused, landed = raised.value.report.changes
    assert (refused.workflow_id, refused.outcome) == (sources_id, "refused")
    assert "st.digest.v9" in refused.reason
    assert (landed.workflow_id, landed.outcome, landed.saved_id) == (weekly_id, "adopted", "wf.weekly-digest.v2")
    assert await world.library.current(sources_id) == sources_id


async def test_a_consumer_a_person_saved_past_since_the_listing_is_refused_and_their_version_stays(tmp_path: Path) -> None:
    world = World(tmp_path)
    await world.save_steps(3, 4)
    first = consumer("digest-sources", DIGEST[3])
    sources_id = await world.save(first)
    plan = await world.plan(V4, {sources_id: ("adopt", "It fits.")})
    person, lineage = adapt(first, ops=[{"op": "set_description", "to": "Digest one source, briefly."}])
    newer = await world.save(person, lineage=lineage)

    with pytest.raises(LandingRefusedError) as raised:
        await _land(world, plan)

    (refused,) = raised.value.report.changes
    assert refused.outcome == "refused"
    assert newer in refused.reason
    assert f"run step-maintenance with {V4}" in refused.reason
    assert "landed" not in str(raised.value)
    assert world.store.written(newer)["saved_by"] == "owner:cli"


async def test_two_changed_steps_adopted_in_one_consumer_land_as_one_version_on_one_live_trial_covering_both(tmp_path: Path) -> None:
    world = World(tmp_path)
    await world.save_steps(3, 4)
    await world.save_summarize()
    sources_id = await world.save(consumer("digest-sources", DIGEST[3], also=SUMMARIZE[1]))
    plan = await world.plan(f"{V4},{SUMMARIZE2}", {sources_id: ("adopt", "It fits.")})

    landed = await _land(world, plan)

    assert [(change.step_id, change.outcome, change.saved_id) for change in landed.changes] == [
        (V4, "adopted", "wf.digest-sources.v2"),
        (SUMMARIZE2, "adopted", "wf.digest-sources.v2"),
    ]
    marker = await world.library.fetch_live_trial("wf.digest-sources.v2")
    assert marker == LiveTrialMarker(
        previous=sources_id, steps=(LiveTrialStep(step="digest", step_hash=HASH4), LiveTrialStep(step="summarize", step_hash=SUMMARIZE2_HASH))
    )
    lineage = await world.library.fetch_lineage("wf.digest-sources.v2")
    assert lineage is not None
    assert (lineage.parent_version, sorted(lineage.touched)) == (1, ["digest", "summarize"])
    child = await world.library.fetch_workflow("wf.digest-sources.v2")
    assert [step_content_address(step) for step in child.inline] == [HASH4, SUMMARIZE2_HASH]
    assert await world.library.locate("digest-sources", 3) is None, "one landing saves one version per consumer"
    told = await world.steps.report_changes(landed)
    assert f"wf.digest-sources.v2 adopts {V4} on live trial in place of {sources_id}." in told
    assert f"wf.digest-sources.v2 adopts {SUMMARIZE2} on live trial in place of {sources_id}." in told


async def test_a_flag_drafted_before_an_adoption_into_the_same_consumer_lands_on_the_version_the_adoption_saved(tmp_path: Path) -> None:
    world = World(tmp_path)
    await world.save_steps(3, 4)
    await world.save_summarize()
    sources_id = await world.save(consumer("digest-sources", DIGEST[3], also=SUMMARIZE[1]))
    consumers = await world.steps.list_consumers(f"{V4},{SUMMARIZE2}")
    draft = await world.steps.prepare_plan(consumers, await world.steps.check_contracts(consumers), await world.steps.run_step_cases(f"{V4},{SUMMARIZE2}"))
    decisions = PlanDecisions(
        decisions=[
            PlanDecision(workflow_id=sources_id, step_id=V4, disposition="flag", reason="Only the owner can say."),
            PlanDecision(workflow_id=sources_id, step_id=SUMMARIZE2, disposition="adopt", reason="It names every claim."),
        ]
    )

    landed = await world.steps.land_changes(draft, decisions)

    assert [(change.step_id, change.outcome) for change in landed.changes] == [(V4, "flagged"), (SUMMARIZE2, "adopted")]
    flag = await world.library.fetch_flag("mf.consumer.digest-sources.digest")
    assert flag is not None
    assert flag.workflow == "wf.digest-sources.v2", "the flag names the consumer's newest version, the one the adoption saved"


async def test_two_adoptions_into_one_consumer_whose_save_the_library_refuses_are_refused_together_each_naming_the_other(tmp_path: Path) -> None:
    world = World(tmp_path)
    await world.save_steps(3, 4)
    await world.save_summarize()
    sources_id = await world.save(consumer("digest-sources", DIGEST[3], also=SUMMARIZE[1]))
    plan = await world.plan(f"{V4},{SUMMARIZE2}", {sources_id: ("adopt", "It fits.")})
    world.store.refused_ids = frozenset({"wf.digest-sources.v2"})

    with pytest.raises(LandingRefusedError) as raised:
        await _land(world, plan)

    digest, summarize = raised.value.report.changes
    assert (digest.outcome, summarize.outcome) == ("refused", "refused")
    assert "refused the page wf.digest-sources.v2" in digest.reason
    assert f"adopted together with {SUMMARIZE2}" in digest.reason
    assert f"adopted together with {V4}" in summarize.reason
    assert await world.library.current(sources_id) == sources_id


async def test_an_adoption_into_a_version_still_on_live_trial_builds_on_it_keeps_its_baseline_and_carries_its_steps(tmp_path: Path) -> None:
    world = World(tmp_path)
    await world.save_steps(3, 4)
    await world.save_summarize()
    sources_id = await world.save(consumer("digest-sources", DIGEST[3], also=SUMMARIZE[1]))
    await _land(world, await world.plan(V4, {sources_id: ("adopt", "It quotes every claim.")}))
    on_trial = "wf.digest-sources.v2"
    await world.log.append(_ran(await world.library.fetch_workflow(on_trial), on_trial, at=T0))

    landed = await _land(world, await world.plan(SUMMARIZE2, {on_trial: ("adopt", "It names every claim.")}))

    (change,) = landed.changes
    assert (change.workflow_id, change.saved_id) == (on_trial, "wf.digest-sources.v3")
    assert await world.library.fetch_live_trial("wf.digest-sources.v3") == LiveTrialMarker(
        previous=sources_id, steps=(LiveTrialStep(step="digest", step_hash=HASH4), LiveTrialStep(step="summarize", step_hash=SUMMARIZE2_HASH))
    )
    lineage = await world.library.fetch_lineage("wf.digest-sources.v3")
    assert lineage is not None
    assert (lineage.parent_version, lineage.touched) == (2, ("summarize",))
    assert f"wf.digest-sources.v3 adopts {SUMMARIZE2} on live trial in place of {on_trial}." in await world.steps.report_changes(landed)


async def test_an_adoption_into_a_version_whose_live_trial_closed_starts_a_live_trial_of_its_own(tmp_path: Path) -> None:
    world = World(tmp_path)
    await world.save_steps(3, 4)
    await world.save_summarize()
    sources_id = await world.save(consumer("digest-sources", DIGEST[3], also=SUMMARIZE[1]))
    await _land(world, await world.plan(V4, {sources_id: ("adopt", "It quotes every claim.")}))
    closed = "wf.digest-sources.v2"
    closed_workflow = await world.library.fetch_workflow(closed)
    for hour in range(3):
        await world.log.append(_ran(closed_workflow, closed, at=T0 + timedelta(hours=hour)))

    landed = await _land(world, await world.plan(SUMMARIZE2, {closed: ("adopt", "It names every claim.")}))

    (change,) = landed.changes
    assert change.saved_id == "wf.digest-sources.v3"
    assert await world.library.fetch_live_trial("wf.digest-sources.v3") == LiveTrialMarker(
        previous=closed, steps=(LiveTrialStep(step="summarize", step_hash=SUMMARIZE2_HASH),)
    )


async def test_check_change_answers_the_validation_issues_of_an_op_that_breaks_the_wiring_and_saves_nothing(tmp_path: Path) -> None:
    world = World(tmp_path)
    await world.save_steps(3, 4)
    sources_id = await world.save(consumer("digest-sources", DIGEST[3]))
    written = len(world.store.payloads)
    rewire: dict[str, JsonValue] = {"op": "rewire", "id": "digest", "input": "text", "to": {"parameter": "missing"}}

    broken = await world.steps.check_change(sources_id, V4, extra_ops=(rewire,))
    fits = await world.steps.check_change(sources_id, V4)

    assert broken.startswith("fails")
    assert "missing" in broken
    assert fits.startswith("passes")
    assert len(world.store.payloads) == written


async def test_report_changes_tells_the_owner_once_what_changed_in_which_workflow_and_why(tmp_path: Path) -> None:
    world = World(tmp_path)
    landed = LandingReport(
        changes=[
            _landed("wf.digest-sources.v1", "adopted", "It fits and quotes every claim.", saved_id="wf.digest-sources.v2"),
            _landed("wf.weekly-digest.v1", "kept", "The weekly digest keeps its own tone."),
        ],
        left_for_next_run=["wf.daily-digest.v1"],
    )

    answer = await world.steps.report_changes(landed)

    ((title, message),) = world.notifier.sent
    assert "digest" in title
    for said in ("wf.digest-sources.v2", "It fits and quotes every claim.", "wf.weekly-digest.v1", "keeps its own tone", "wf.daily-digest.v1"):
        assert said in message
    assert "run step-maintenance by hand with the same step ids" in message, "the owner reads how to take the consumers past the cap now"
    assert message in answer


async def test_select_disagreements_answers_a_step_one_consumer_adopted_and_another_kept_and_nothing_when_all_adopted(tmp_path: Path) -> None:
    world = World(tmp_path)
    await world.save_steps(3, 4)
    sources_id = await world.save(consumer("digest-sources", DIGEST[3]))
    weekly_id = await world.save(consumer("weekly-digest", DIGEST[3]))
    consumers = await world.steps.list_consumers(V4)
    split = LandingReport(
        changes=[
            _landed(sources_id, "adopted", "It fits.", saved_id="wf.digest-sources.v2"),
            _landed(weekly_id, "kept", "The weekly digest keeps its own tone."),
        ],
        left_for_next_run=[],
    )
    agreed = LandingReport(
        changes=[
            _landed(sources_id, "adopted", "It fits.", saved_id="wf.digest-sources.v2"),
            _landed(weekly_id, "adopted", "It fits too.", saved_id="wf.weekly-digest.v2"),
        ],
        left_for_next_run=[],
    )

    (change,) = await world.steps.select_disagreements(split, consumers)
    nothing = await world.steps.select_disagreements(agreed, consumers)

    assert (change.step, change.step_id) == ("digest", V4)
    assert change.step_yaml == canonical_step_yaml(DIGEST[4]), "the drafter starts from the changed version, whole"
    assert [(side.workflow_id, side.outcome, side.reason) for side in change.adopted] == [(sources_id, "adopted", "It fits.")]
    assert [(side.workflow_id, side.outcome, side.reason) for side in change.kept] == [(weekly_id, "kept", "The weekly digest keeps its own tone.")]
    assert all(side.diff and side.nodes == ["digest"] for side in (*change.adopted, *change.kept))
    assert (change.proposal, change.step_files, change.file_edits) == ("", (), ())
    assert nothing == []


async def test_hand_over_keeps_a_draft_its_judge_kept_as_a_flag_page_drops_the_rest_with_their_reasons_and_tells_the_owner_once(
    tmp_path: Path,
) -> None:
    world = World(tmp_path)
    await world.save_steps(3, 4)

    def selected(step: str, step_id: str) -> RepositoryChange:
        side = ConsumerSide(workflow_id="wf.weekly-digest.v1", outcome="kept", reason="Its own tone.", nodes=["digest"], diff="", presentation=None, intent=None)
        return RepositoryChange(
            step=step, step_id=step_id, step_yaml=canonical_step_yaml(DIGEST[4]), adopted=[side.model_copy(update={"outcome": "adopted"})], kept=[side]
        )

    step_file = DraftedFile(path="harness/workflows/steps/digest.md", text="---\ntitle: Digest\nversion: 5\n---\n\nQuote each claim, 100%% of them, %% always.")
    edit = DraftedFile(path="templates/research/workflows/workflows/weekly-digest.md", text="## Steps\n\nwith:\n  tone: weekly")
    disagreements = [selected("digest", V4), selected("summarize", "st.summarize.v2"), selected("classify", "st.classify.v3")]
    drafts = [
        disagreements[0].model_copy(update={"proposal": "Give digest a tone parameter.", "step_files": (step_file,), "file_edits": (edit,)}),
        disagreements[1].model_copy(update={"proposal": "Split summarize in two."}),
    ]
    verdicts = [
        RepositoryVerdict(step="digest", decision="keep", reason="Both consumers keep their stated goal."),
        RepositoryVerdict(step="summarize", decision="drop", reason="The split loses the weekly tone."),
    ]

    handed = await world.steps.hand_over_repository_changes("wf.digest-sources.v2 adopts st.digest.v4.", disagreements, drafts, verdicts)

    assert handed.report == "wf.digest-sources.v2 adopts st.digest.v4."
    assert handed.flags == ["mf.change.digest"]
    dropped = {item.step: item.reason for item in handed.dropped}
    assert dropped["summarize"] == "The split loses the weekly tone."
    assert "no draft" in dropped["classify"]
    assert handed.problems == []
    flag = await world.library.fetch_flag("mf.change.digest")
    assert flag is not None
    assert (flag.kind, flag.status, flag.step, flag.step_id, flag.step_hash, flag.workflow) == ("repository_change", "open", "digest", V4, HASH4, "")
    assert "Both consumers keep their stated goal." in flag.evidence
    for said in ("Give digest a tone parameter.", step_file.path, "title: Digest", edit.path, "tone: weekly", "100% % of them"):
        assert said in flag.proposed_change
    ((_, message),) = world.notifier.sent
    assert "mf.change.digest" in message


async def test_repository_verdict_answers_an_undecided_verdict_for_the_step(tmp_path: Path) -> None:
    world = World(tmp_path)

    verdict = await world.steps.repository_verdict("digest")

    assert (verdict.step, verdict.decision) == ("digest", "undecided")


async def test_plan_decisions_answers_no_decision_for_the_plan(tmp_path: Path) -> None:
    world = World(tmp_path)

    decisions = await world.steps.plan_decisions(MaintenancePlan(changes=[], left_for_next_run=[]))

    assert decisions == PlanDecisions(decisions=[])


async def test_a_drafted_change_no_decision_names_writes_nothing_and_the_owner_reads_when_it_is_offered_again(tmp_path: Path) -> None:
    world = World(tmp_path)
    await world.save_steps(3, 4)
    sources_id = await world.save(consumer("digest-sources", DIGEST[3]))
    weekly_id = await world.save(consumer("weekly-digest", DIGEST[3]))
    consumers = await world.steps.list_consumers(V4)
    draft = await world.steps.prepare_plan(consumers, await world.steps.check_contracts(consumers), await world.steps.run_step_cases(V4))
    decisions = PlanDecisions(decisions=[PlanDecision(workflow_id=sources_id, step_id=V4, disposition="adopt", reason="It fits.")])

    landed = await world.steps.land_changes(draft, decisions)

    assert [(change.workflow_id, change.outcome, change.reason) for change in landed.changes] == [
        (sources_id, "adopted", "It fits."),
        (weekly_id, "not_decided", "not decided, so nothing was written, and step maintenance offers it again when it next runs for its step"),
    ]
    (record,) = await world.library.fetch_consumer_record(weekly_id)
    assert (record.declined, record.declined_because) == (None, None), "a pair not decided writes no decline"
    assert [pair.workflow_id for pair in (await world.steps.list_consumers(V4)).pairs] == [weekly_id]
    told = await world.steps.report_changes(landed)
    assert f"{weekly_id} with {V4} was not decided, so nothing was written" in told
    assert f"with the step's next version, or when you run step-maintenance with {V4}." in told


async def test_the_owner_s_line_starts_the_reason_after_its_period_with_a_capital(tmp_path: Path) -> None:
    world = World(tmp_path)
    kept = _landed("wf.weekly-digest.v1", "kept", "the weekly digest keeps its own tone.")
    flagged = _landed("wf.daily-digest.v1", "flagged", "only the owner can say.")

    told = await world.steps.report_changes(LandingReport(changes=[kept, flagged], left_for_next_run=[]))

    assert f"wf.weekly-digest.v1 keeps its copy instead of {V4}. The weekly digest keeps its own tone." in told
    assert f"wf.daily-digest.v1 waits for your decision on {V4}. Only the owner can say." in told


async def test_a_decision_naming_no_drafted_change_is_refused_and_every_drafted_change_still_lands(tmp_path: Path) -> None:
    world = World(tmp_path)
    await world.save_steps(3, 4)
    sources_id = await world.save(consumer("digest-sources", DIGEST[3]))
    weekly_id = await world.save(consumer("weekly-digest", DIGEST[3]))
    plan = await world.plan(V4, {sources_id: ("adopt", "It fits."), weekly_id: ("keep", "Its own tone.")})
    invented = PlanDecision(workflow_id="wf.daily-digest.v1", step_id=V4, disposition="adopt", reason="It fits as well.")

    with pytest.raises(LandingRefusedError) as raised:
        await world.steps.land_changes(plan, PlanDecisions(decisions=[*_decided(plan).decisions, invented]))

    adopted, kept, refused = raised.value.report.changes
    assert [(adopted.workflow_id, adopted.outcome), (kept.workflow_id, kept.outcome)] == [(sources_id, "adopted"), (weekly_id, "kept")]
    assert (refused.workflow_id, refused.step_id, refused.disposition, refused.outcome) == ("wf.daily-digest.v1", V4, "adopt", "refused")
    assert "no drafted change" in refused.reason
    assert "wf.daily-digest.v1" in str(raised.value)


async def test_a_drafted_change_decided_twice_lands_neither_decision_and_the_others_land(tmp_path: Path) -> None:
    world = World(tmp_path)
    await world.save_steps(3, 4)
    sources_id = await world.save(consumer("digest-sources", DIGEST[3]))
    weekly_id = await world.save(consumer("weekly-digest", DIGEST[3]))
    plan = await world.plan(V4, {sources_id: ("adopt", "It fits."), weekly_id: ("keep", "Its own tone.")})
    again = PlanDecision(workflow_id=sources_id, step_id=V4, disposition="keep", reason="On second thought it does not fit.")

    with pytest.raises(LandingRefusedError) as raised:
        await world.steps.land_changes(plan, PlanDecisions(decisions=[*_decided(plan).decisions, again]))

    refused, kept = raised.value.report.changes
    assert (refused.workflow_id, refused.outcome) == (sources_id, "refused")
    assert "decided it 2 times" in refused.reason
    assert (kept.workflow_id, kept.outcome) == (weekly_id, "kept")
    assert await world.library.current(sources_id) == sources_id, "neither of two decisions for one change lands"


def _tampered(plan: MaintenancePlan, index: int, **update: object) -> MaintenancePlan:
    """`plan` with the change at `index` changed as a model might have changed it."""
    changes: list[PlannedChange] = list(plan.changes)
    changes[index] = changes[index].model_copy(update=update)
    return plan.model_copy(update={"changes": changes})


async def test_a_malformed_edit_operation_is_refused_with_its_reason_and_check_change_answers_it_as_a_failed_check(tmp_path: Path) -> None:
    world = World(tmp_path)
    await world.save_steps(3, 4)
    sources_id = await world.save(consumer("digest-sources", DIGEST[3]))
    weekly_id = await world.save(consumer("weekly-digest", DIGEST[3]))
    malformed: dict[str, JsonValue] = {"op": "set_output", "to": {"bogus": "wire"}}
    plan = await world.plan(V4, {sources_id: ("adopt", "It fits."), weekly_id: ("adopt", "It fits too.")})

    with pytest.raises(LandingRefusedError) as raised:
        await _land(world, _tampered(plan, 0, extra_ops=(malformed,)))
    answer = await world.steps.check_change(sources_id, V4, extra_ops=(malformed,))

    refused, landed = raised.value.report.changes
    assert (refused.workflow_id, refused.outcome) == (sources_id, "refused")
    assert "edit operation does not apply" in refused.reason
    assert (landed.workflow_id, landed.outcome) == (weekly_id, "adopted")
    assert answer.startswith("fails")
    assert "edit operation does not apply" in answer


async def test_a_planned_change_naming_a_step_version_older_than_the_newest_is_refused_and_the_others_land(tmp_path: Path) -> None:
    world = World(tmp_path)
    await world.save_steps(3, 4)
    sources_id = await world.save(consumer("digest-sources", DIGEST[3]))
    weekly_id = await world.save(consumer("weekly-digest", DIGEST[3]))
    plan = await world.plan(V4, {sources_id: ("adopt", "It fits."), weekly_id: ("adopt", "It fits too.")})

    with pytest.raises(LandingRefusedError) as raised:
        await _land(world, _tampered(plan, 0, step_id="st.digest.v3"))

    refused, landed = raised.value.report.changes
    assert (refused.workflow_id, refused.outcome) == (sources_id, "refused")
    assert "st.digest.v3 is not the newest version of digest" in refused.reason
    assert (landed.workflow_id, landed.outcome) == (weekly_id, "adopted")
    assert await world.library.current(sources_id) == sources_id


async def test_a_planned_change_to_a_consumer_that_declines_the_step_version_is_refused_and_the_others_land(tmp_path: Path) -> None:
    world = World(tmp_path)
    await world.save_steps(3, 4)
    sources_id = await world.save(consumer("digest-sources", DIGEST[3]))
    weekly_id = await world.save(consumer("weekly-digest", DIGEST[3]))
    plan = await world.plan(V4, {sources_id: ("adopt", "It fits."), weekly_id: ("adopt", "It fits too.")})
    await world.library.record_decline(sources_id, step="digest", step_hash=HASH4, because="reverted")

    with pytest.raises(LandingRefusedError) as raised:
        await _land(world, plan)

    refused, landed = raised.value.report.changes
    assert (refused.workflow_id, refused.outcome) == (sources_id, "refused")
    assert f"{sources_id} declines {V4}" in refused.reason
    assert (landed.workflow_id, landed.outcome) == (weekly_id, "adopted")


async def test_an_adoption_whose_edits_change_another_inline_step_is_refused_and_check_change_says_so_and_still_checks_it(tmp_path: Path) -> None:
    world = World(tmp_path)
    await world.save_steps(3, 4)
    sources_id = await world.save(consumer("digest-sources", DIGEST[3], also=SUMMARIZE[1]))
    weekly_id = await world.save(consumer("weekly-digest", DIGEST[3]))
    rewrite: dict[str, JsonValue] = {"op": "replace_inline_step", "name": "summarize", "with": SUMMARIZE[2].model_dump(mode="json")}
    plan = await world.plan(V4, {sources_id: ("adopt", "It fits."), weekly_id: ("adopt", "It fits too.")})

    with pytest.raises(LandingRefusedError) as raised:
        await _land(world, _tampered(plan, 0, extra_ops=(rewrite,)))
    answer = await world.steps.check_change(sources_id, V4, extra_ops=(rewrite,))

    refused, landed = raised.value.report.changes
    assert (refused.workflow_id, refused.outcome) == (sources_id, "refused")
    assert "replace_inline_step" in refused.reason
    assert "rewire, add_parameter and set_output" in refused.reason
    assert (landed.workflow_id, landed.outcome) == (weekly_id, "adopted")
    assert answer.startswith("passes the contract check")
    assert "The landing refuses it as an adoption" in answer
    assert "replace_inline_step" in answer


async def test_list_consumers_takes_the_newest_saved_version_of_a_named_step(tmp_path: Path) -> None:
    world = World(tmp_path)
    await world.save_steps(2, 3, 4)
    styled_id = await world.save(consumer("styled-digest", DIGEST[2]))

    listed = await world.steps.list_consumers("st.digest.v3")

    assert [(pair.workflow_id, pair.step_id) for pair in listed.pairs] == [(styled_id, V4)]


async def test_a_consumer_past_the_cap_in_two_changed_steps_is_left_for_the_next_run_once(tmp_path: Path) -> None:
    world = World(tmp_path)
    await world.save_steps(3, 4)
    await world.save_summarize()
    ids = [await world.save(consumer(f"digest-{index:02}", DIGEST[3], also=SUMMARIZE[1])) for index in range(21)]

    listed = await world.steps.list_consumers(f"{V4},st.summarize.v2")

    assert len(listed.pairs) == 20
    assert sorted(listed.left_for_next_run) == sorted(ids)
    assert len(listed.left_for_next_run) == len(ids)


async def test_a_flag_a_person_already_resolved_for_the_step_version_is_left_alone_and_the_landing_says_so(tmp_path: Path) -> None:
    world = World(tmp_path)
    await world.save_steps(3, 4)
    daily_id = await world.save(consumer("daily-digest", DIGEST[3]))
    plan = await world.plan(V4, {daily_id: ("flag", "Only the owner can say.")})
    decided = MaintenanceFlag(
        kind="consumer_flag",
        step="digest",
        step_id=V4,
        step_hash=HASH4,
        workflow=daily_id,
        title="Consumer flag",
        summary="Decide whether daily-digest adopts digest v4.",
        evidence="An earlier run flagged it.",
        raised_by=STEP_MAINTENANCE_AGENT,
        raised_at=T0 - timedelta(days=1),
    )
    await world.library.put_flag(decided)
    await world.library.resolve_flag("mf.consumer.daily-digest.digest", by="owner", reason="The daily digest keeps its copy.")

    landed = await _land(world, plan)

    (flagged,) = landed.changes
    assert flagged.outcome == "flagged"
    assert any("already resolved" in note for note in flagged.notes), flagged.notes
    stored = await world.library.fetch_flag("mf.consumer.daily-digest.digest")
    assert stored is not None
    assert (stored.status, stored.resolved_by) == ("resolved", "owner")


async def test_step_case_runs_build_no_stand_in_registry_for_a_step_without_cases_or_with_a_kept_report(tmp_path: Path) -> None:
    built: list[StandInCalls] = []

    async def counted(calls: StandInCalls) -> StepRegistry:
        built.append(calls)
        return StepRegistry()

    reports = FileCaseReports(tmp_path / "reports")
    runs = StepCaseRuns(counted, reports)
    kept = CaseReport(
        step_id="st.digest.v1",
        step_hash=HASH4,
        cases_hash=cases_address(CASES),
        results=(CaseResult(name="quotes", passed=True, errored=False, reason="every claim quoted"),),
        blocked_by=None,
        no_cases=False,
        stand_in_calls=0,
        refused=None,
    )
    reports.put(kept)

    without = await runs.perform(DIGEST[4], V4, None, [])
    again = await runs.perform(DIGEST[4], V4, CASES, [])

    assert without.no_cases
    assert again == kept.model_copy(update={"step_id": V4})
    assert built == []


def test_one_line_keeps_a_model_written_name_on_one_line_without_backticks_and_with_each_double_percent_split() -> None:
    assert one_line("steps/`digest`.md\n  and 100%% %%sure") == "steps/digest.md and 100% % % %sure"


REFUSED_EDITS: tuple[dict[str, JsonValue], ...] = (
    {"op": "add_node", "node": {"kind": "run", "id": "extra", "work": "digest", "inputs": {"text": {"parameter": "text"}}}},
    {"op": "replace_node", "id": "digest", "with": {"kind": "run", "id": "digest", "work": "digest", "inputs": {"text": {"parameter": "text"}}}},
    {"op": "remove_node", "id": "digest"},
    {"op": "set_description", "to": "Digest one source, as an orchestrator gate charge would say."},
    {"op": "add_inline_step", "with": SUMMARIZE[1].model_dump(mode="json")},
    {"op": "remove_inline_step", "name": "digest"},
    {"op": "replace_inline_step", "name": "digest", "with": DIGEST[3].model_dump(mode="json")},
)


@pytest.mark.parametrize("edit", REFUSED_EDITS, ids=[str(edit["op"]) for edit in REFUSED_EDITS])
async def test_an_adoption_carrying_an_edit_other_than_rewire_add_parameter_or_set_output_is_refused_and_check_change_says_so(
    tmp_path: Path, edit: dict[str, JsonValue]
) -> None:
    world = World(tmp_path)
    await world.save_steps(3, 4)
    sources_id = await world.save(consumer("digest-sources", DIGEST[3]))
    weekly_id = await world.save(consumer("weekly-digest", DIGEST[3]))
    plan = await world.plan(V4, {sources_id: ("adopt", "It fits."), weekly_id: ("adopt", "It fits too.")})

    with pytest.raises(LandingRefusedError) as raised:
        await _land(world, _tampered(plan, 0, extra_ops=(edit,)))
    answer = await world.steps.check_change(sources_id, V4, extra_ops=(edit,))

    refused, landed = raised.value.report.changes
    assert (refused.workflow_id, refused.outcome) == (sources_id, "refused")
    assert str(edit["op"]) in refused.reason
    assert "rewire, add_parameter and set_output" in refused.reason
    assert (landed.workflow_id, landed.outcome) == (weekly_id, "adopted")
    assert "The landing refuses it as an adoption" in answer
    assert str(edit["op"]) in answer
    assert await world.library.current(sources_id) == sources_id


async def test_an_adoption_that_rewires_a_node_to_a_parameter_it_adds_lands_and_check_change_says_it_would(tmp_path: Path) -> None:
    world = World(tmp_path)
    await world.save_steps(3, 4)
    sources_id = await world.save(consumer("digest-sources", DIGEST[3]))
    wiring: tuple[dict[str, JsonValue], ...] = (
        {"op": "add_parameter", "name": "source", "with": {"description": "the whole source", "json_schema": {"type": "string"}}},
        {"op": "rewire", "id": "digest", "input": "text", "to": {"parameter": "source"}},
    )
    plan = await world.plan(V4, {sources_id: ("adopt", "It fits once it reads the whole source.")})

    answer = await world.steps.check_change(sources_id, V4, extra_ops=wiring)
    landed = await _land(world, _tampered(plan, 0, extra_ops=wiring))

    assert answer.startswith("passes")
    assert "The landing would adopt it." in answer
    ((change),) = landed.changes
    assert (change.outcome, change.saved_id) == ("adopted", "wf.digest-sources.v2")
    child = await world.library.fetch_workflow("wf.digest-sources.v2")
    assert "source" in child.parameters
    (node,) = child.nodes
    assert isinstance(node, RunNode)
    assert node.inputs["text"] == FromParameter(parameter="source")


async def test_check_change_says_the_landing_refuses_an_older_step_version_and_a_declined_one(tmp_path: Path) -> None:
    world = World(tmp_path)
    await world.save_steps(3, 4)
    sources_id = await world.save(consumer("digest-sources", DIGEST[3]))
    weekly_id = await world.save(consumer("weekly-digest", DIGEST[3]))
    await world.library.record_decline(weekly_id, step="digest", step_hash=HASH4, because="reverted")

    older = await world.steps.check_change(sources_id, "st.digest.v3")
    declined = await world.steps.check_change(weekly_id, V4)

    assert "The landing refuses it as an adoption: st.digest.v3 is not the newest version of digest" in older
    assert f"The landing refuses it as an adoption: {weekly_id} declines {V4}" in declined


async def test_an_adoption_after_a_revert_is_numbered_above_the_archived_version_which_keeps_its_marker_and_its_runs(tmp_path: Path) -> None:
    world = World(tmp_path)
    await world.save_steps(3, 4)
    sources = consumer("digest-sources", DIGEST[3])
    sources_id = await world.save(sources)
    await _land(world, await world.plan(V4, {sources_id: ("adopt", "It quotes every claim.")}))
    live_trial_id = "wf.digest-sources.v2"
    live_trial = await world.library.fetch_workflow(live_trial_id)
    marker = await world.library.fetch_live_trial(live_trial_id)
    await world.log.append(_ran(sources, sources_id, at=T0 - timedelta(days=1)))
    await world.log.append(_ran(live_trial, live_trial_id, at=T0))
    assert await world.library.revert(live_trial_id) == sources_id
    await world.save_steps(5)

    landed = await _land(world, await world.plan("st.digest.v5", {sources_id: ("adopt", "It dates each claim too.")}))

    (change,) = landed.changes
    assert (change.outcome, change.saved_id) == ("adopted", "wf.digest-sources.v3")
    assert "archived_at" in world.store.pages[live_trial_id]["frontmatter"]
    assert await world.library.fetch_workflow(live_trial_id) == live_trial
    assert await world.library.fetch_live_trial(live_trial_id) == marker
    newest = await world.library.fetch_workflow("wf.digest-sources.v3")
    assert newest.inline == (DIGEST[5],)
    hashes = await version_hashes(newest, versions=world.library, fetch_workflow=world.library.fetch_workflow)
    trust = trust_across_versions(await world.log.history_for_any(spec_hashes=hashes.keys()), hashes, spec_identity(newest))
    assert trust.total.runs_with_evidence == 2, "the run of the archived version and the run of the version it replaced both count"
    assert dict(trust.versions).keys() == {1, 2, 3}


async def test_a_refused_landing_tells_the_owner_once_what_landed_and_what_was_refused_before_it_fails(tmp_path: Path) -> None:
    world = World(tmp_path)
    await world.save_steps(3, 4)
    sources_id = await world.save(consumer("digest-sources", DIGEST[3]))
    weekly_id = await world.save(consumer("weekly-digest", DIGEST[3]))
    daily_id = await world.save(consumer("daily-digest", DIGEST[3]))
    plan = await world.plan(
        V4, {sources_id: ("adopt", "It fits."), weekly_id: ("adopt", "It fits too."), daily_id: ("keep", "The daily tone stays.")}
    )
    world.store.refused_ids = frozenset({"wf.weekly-digest.v2"})

    with pytest.raises(LandingRefusedError) as raised:
        await _land(world, plan)

    ((title, message),) = world.notifier.sent
    assert "refused" in title
    assert f"wf.digest-sources.v2 adopts {V4} on live trial in place of {sources_id}." in message
    assert f"{weekly_id} was refused {V4}" in message
    assert f"{daily_id} keeps its copy instead of {V4}." in message
    assert "The owner was told." in str(raised.value)


@pytest.mark.parametrize(
    ("failure", "delivers", "said"),
    [(ConnectionError("the notification centre is not running"), True, "The notice to the owner failed: the notification centre is not running"),
     (None, False, "The notice to the owner was not delivered.")],
    ids=["it raises", "it is not delivered"],
)
async def test_a_refused_landing_whose_notice_does_not_reach_the_owner_still_fails_and_says_so(
    tmp_path: Path, failure: Exception | None, delivers: bool, said: str
) -> None:
    world = World(tmp_path)
    world.notifier.failure = failure
    world.notifier.delivers = delivers
    await world.save_steps(3, 4)
    sources_id = await world.save(consumer("digest-sources", DIGEST[3]))
    plan = await world.plan(V4, {sources_id: ("adopt", "It fits.")})
    world.store.refused_ids = frozenset({"wf.digest-sources.v2"})

    with pytest.raises(LandingRefusedError) as raised:
        await _land(world, plan)

    assert len(world.notifier.sent) == 1
    assert str(raised.value).endswith(said)


async def test_a_landing_without_refusals_leaves_the_one_notice_to_report_changes(tmp_path: Path) -> None:
    world = World(tmp_path)
    await world.save_steps(3, 4)
    sources_id = await world.save(consumer("digest-sources", DIGEST[3]))

    landed = await _land(world, await world.plan(V4, {sources_id: ("adopt", "It fits.")}))

    assert world.notifier.sent == []
    await world.steps.report_changes(landed)
    assert len(world.notifier.sent) == 1


def _rejected_plan(step_id: str, *, raised_at: datetime = T0 - timedelta(days=1)) -> MaintenanceFlag:
    return MaintenanceFlag(
        kind="plan_rejected",
        step="digest",
        step_id=step_id,
        step_hash=step_content_address(DIGEST[int(step_id.rsplit(".v", 1)[1])]),
        title="Plan for digest rejected",
        summary="Decide how the step reaches the workflows that hold digest.",
        evidence="The judge rejected the plan three times.",
        raised_by=STEP_MAINTENANCE_AGENT,
        raised_at=raised_at,
    )


@pytest.mark.parametrize("disposition", ["adopt", "keep", "flag"])
async def test_a_landing_for_a_step_resolves_the_open_plan_rejected_flag_of_an_earlier_version(tmp_path: Path, disposition: str) -> None:
    world = World(tmp_path)
    await world.save_steps(3, 4)
    sources_id = await world.save(consumer("digest-sources", DIGEST[3]))
    await world.library.put_flag(_rejected_plan("st.digest.v3"))

    await _land(world, await world.plan(V4, {sources_id: (disposition, "Decided.")}))

    flag = await world.library.fetch_flag("mf.rejected.digest")
    assert flag is not None
    assert (flag.status, flag.resolved_by) == ("resolved", STEP_MAINTENANCE_AGENT)
    assert V4 in flag.decision_reason


async def test_a_landing_leaves_a_plan_rejected_flag_a_person_resolved_as_they_left_it(tmp_path: Path) -> None:
    world = World(tmp_path)
    await world.save_steps(3, 4)
    sources_id = await world.save(consumer("digest-sources", DIGEST[3]))
    await world.library.put_flag(_rejected_plan(V4))
    await world.library.resolve_flag("mf.rejected.digest", by="owner", reason="I will handle digest by hand.")

    await _land(world, await world.plan(V4, {sources_id: ("keep", "Decided.")}))

    flag = await world.library.fetch_flag("mf.rejected.digest")
    assert flag is not None
    assert (flag.status, flag.resolved_by, flag.decision_reason) == ("resolved", "owner", "I will handle digest by hand.")


async def test_a_landing_whose_every_change_was_refused_leaves_the_plan_rejected_flag_open(tmp_path: Path) -> None:
    world = World(tmp_path)
    await world.save_steps(3, 4)
    sources_id = await world.save(consumer("digest-sources", DIGEST[3]))
    await world.library.put_flag(_rejected_plan("st.digest.v3"))
    plan = await world.plan(V4, {sources_id: ("adopt", "It fits.")})
    world.store.refused_ids = frozenset({"wf.digest-sources.v2"})

    with pytest.raises(LandingRefusedError):
        await _land(world, plan)

    flag = await world.library.fetch_flag("mf.rejected.digest")
    assert flag is not None
    assert flag.status == "open"
