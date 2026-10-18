"""An adaptation says why it was made and claims what a later run would show. Only the claim is graded.

Variation without a claim is not selection: the diff records what changed and the ledger records what
happened, and nothing ever connects the two. So a `Lineage` carries two more things -- prose evidence
for the person who asks why this version exists, and a structural `Prediction` a later run falsifies.

The split is the whole design. Prose cannot be graded, so it is read and never scored; the claim is
data drawn only from what an `RunEvidence` records, so grading is a fold over the ledger with no model
anywhere in the verdict. And the verdict is tri-state, because a version nothing has run yet has not
missed anything -- unknown is not earned, in either direction.
"""

from __future__ import annotations

import json

from typing import TYPE_CHECKING, Any

import pytest
import yaml

from core.ai.workflows.contract_port import ADAPT_TOOL, SAVE_TOOL
from core.ai.workflows.contracts import (
    Approval,
    LibraryCollection,
    Lineage,
    Prediction,
    RunEvidence,
    StepRegistry,
    Workflow,
    WorkflowContract,
    WorkflowPresentation,
    adapt,
    grade,
    load_spec_document,
    prediction_record,
    save,
)
from core.ai.workflows.domain.document import dump_spec_document
from core.ai.workflows.infrastructure.folder import FOLDER, FolderLibrary, folder_toolset
from pydantic import ValidationError
from pydantic_ai.models.test import TestModel
from pydantic_ai.tools import RunContext
from pydantic_ai.usage import RunUsage

from harness.features.authoring.api_models import AdaptRequest
from harness.features.browsing.controller import BrowsingController
from harness.session import NoSessionLog, Runtime, SessionPool
from harness.settings import SelfAuthoringSettings
from tests.support.authoring import a_controller
from tests.support.fakes.sessions import FakeSessionPool
from tests.support.state import a_state, records_under

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence
    from pathlib import Path


def _completed(version: int, **kwargs: object) -> RunEvidence:
    """One completed run of a version, as the observer writes it."""
    return RunEvidence(workflow_id="wf.report", workflow_version=version, outcome="completed", **kwargs)  # type: ignore[arg-type]


def _child(quarterly_report: Workflow, *predictions: Prediction, evidence: str = "") -> Lineage:
    """A child of the report with the claims its adapter made about it."""
    _, lineage = adapt(
        quarterly_report, ops=({"op": "set_description", "to": "Now with commentary."},), evidence=evidence, predictions=predictions
    )
    return lineage


# --- evidence is read, never scored ---------------------------------------------------------------


def test_an_adaptation_records_why_it_was_made(quarterly_report: Workflow) -> None:
    """The diff says what moved and nothing said why, so a v4 in the library answers no question a reader has."""
    lineage = _child(quarterly_report, evidence="The publish step kept re-sending drafts, so the gate now answers.")
    assert lineage.evidence.startswith("The publish step")


def test_evidence_changes_no_verdict(quarterly_report: Workflow) -> None:
    """Prose is for intelligence, machinery is for grading -- two adaptations differing only in prose grade alike."""
    claim = Prediction(claim="produces", subject="publish")
    events = (_completed(4, produced={"publish": "ReportUrl"}),)
    with_prose = grade(_child(quarterly_report, claim, evidence="because the old one was noisy"), events, version=4)
    without = grade(_child(quarterly_report, claim), events, version=4)
    assert [outcome.holds for outcome in with_prose] == [outcome.holds for outcome in without]


def test_a_lineage_round_trips_with_its_evidence_and_its_claims(quarterly_report: Workflow) -> None:
    lineage = _child(quarterly_report, Prediction(claim="produces", subject="publish", shape="ReportUrl"), evidence="why")
    assert Lineage.model_validate_json(lineage.model_dump_json()) == lineage


# --- the vocabulary is closed, and a claim that names nothing is refused at the plugin ---------------


def test_a_claim_outside_the_vocabulary_is_refused(quarterly_report: Workflow) -> None:
    """A prediction nothing in the ledger could answer is not a prediction; the plugin is where that is said."""
    del quarterly_report
    with pytest.raises(ValidationError):
        Prediction(claim="gets_faster", subject="publish")  # type: ignore[arg-type]


def test_a_claim_about_a_node_has_to_name_one() -> None:
    """`produces` with no subject grades against every node and none of them: it can neither hold nor fail."""
    with pytest.raises(ValidationError, match="subject"):
        Prediction(claim="produces")


def test_only_a_produces_claim_can_name_a_shape() -> None:
    """A gate has no shape and a parameter has no shape, so a `shape` on either is a claim nothing reads."""
    with pytest.raises(ValidationError, match="shape"):
        Prediction(claim="approves", subject="approve", shape="ReportUrl")


# --- grading: tri-state, folded off the ledger, no model anywhere ---------------------------------


def test_a_claim_about_a_version_nothing_has_run_is_unmeasurable_rather_than_missed(quarterly_report: Workflow) -> None:
    """Unknown is not earned, and it is not a failure either: an unrun version has missed nothing."""
    lineage = _child(quarterly_report, Prediction(claim="produces", subject="publish"))
    (outcome,) = grade(lineage, (), version=4)
    assert outcome.holds is None
    assert "no completed run" in outcome.observed


def test_a_completed_run_that_produced_the_node_makes_the_claim_hold(quarterly_report: Workflow) -> None:
    lineage = _child(quarterly_report, Prediction(claim="produces", subject="publish"))
    (outcome,) = grade(lineage, (_completed(4, produced={"publish": "ReportUrl"}),), version=4)
    assert outcome.holds is True
    assert "ReportUrl" in outcome.observed


def test_a_completed_run_that_produced_something_else_falsifies_the_claim(quarterly_report: Workflow) -> None:
    """The half that makes it a prediction rather than a note: the ledger can say no."""
    lineage = _child(quarterly_report, Prediction(claim="produces", subject="publish"))
    (outcome,) = grade(lineage, (_completed(4, produced={"draft_report": "ReportDraft"}),), version=4)
    assert outcome.holds is False
    assert "draft_report" in outcome.observed, "a miss has to say what the run did produce"


def test_a_shaped_claim_is_falsified_by_the_wrong_shape(quarterly_report: Workflow) -> None:
    """Shapes, never values: two honest runs differ in what they say and must not differ in kind."""
    lineage = _child(quarterly_report, Prediction(claim="produces", subject="publish", shape="ReportUrl"))
    (outcome,) = grade(lineage, (_completed(4, produced={"publish": "ReportDraft"}),), version=4)
    assert outcome.holds is False
    assert "ReportDraft" in outcome.observed


def test_a_gate_a_person_released_makes_an_approves_claim_hold(quarterly_report: Workflow) -> None:
    lineage = _child(quarterly_report, Prediction(claim="approves", subject="approve"))
    (outcome,) = grade(lineage, (_completed(4, gates_approved=("approve",), produced={"publish": "ReportUrl"}),), version=4)
    assert outcome.holds is True


def test_a_gate_no_completed_run_released_falsifies_it(quarterly_report: Workflow) -> None:
    lineage = _child(quarterly_report, Prediction(claim="approves", subject="approve"))
    (outcome,) = grade(lineage, (_completed(4, produced={"publish": "ReportUrl"}),), version=4)
    assert outcome.holds is False


def test_a_parameter_a_run_was_given_makes_an_accepts_claim_hold(quarterly_report: Workflow) -> None:
    lineage = _child(quarterly_report, Prediction(claim="accepts", subject="quarter"))
    (outcome,) = grade(lineage, (_completed(4, params={"quarter": "Q2"}),), version=4)
    assert outcome.holds is True
    assert "quarter" in outcome.observed


def test_a_parameter_no_run_was_ever_given_falsifies_it(quarterly_report: Workflow) -> None:
    lineage = _child(quarterly_report, Prediction(claim="accepts", subject="region"))
    (outcome,) = grade(lineage, (_completed(4, params={"quarter": "Q2"}),), version=4)
    assert outcome.holds is False


def test_a_completes_claim_holds_on_one_completed_run(quarterly_report: Workflow) -> None:
    lineage = _child(quarterly_report, Prediction(claim="completes"))
    (outcome,) = grade(lineage, (_completed(4),), version=4)
    assert outcome.holds is True


def test_a_version_that_only_ever_failed_falsifies_a_completes_claim(quarterly_report: Workflow) -> None:
    """A claim that can only ever hit is not falsifiable, and `completes` is the one the ledger can refute directly.

    Every other claim reads a completed run's evidence, so a failed run tells it nothing -- the run may
    have died before reaching the node. This one is about the failure itself, so runs that ran and did
    not complete are exactly the evidence against it.
    """
    lineage = _child(quarterly_report, Prediction(claim="completes"))
    failed = RunEvidence(workflow_id="wf.report", workflow_version=4, outcome="failed")
    (outcome,) = grade(lineage, (failed, failed), version=4)
    assert outcome.holds is False
    assert "2" in outcome.observed, "the count of runs that did not complete is what a person acts on"


def test_a_failed_run_measures_nothing_about_what_a_version_produces(quarterly_report: Workflow) -> None:
    """A run that fell over never reached the node, so its silence is not evidence against the claim."""
    lineage = _child(quarterly_report, Prediction(claim="produces", subject="publish"))
    failed = RunEvidence(workflow_id="wf.report", workflow_version=4, outcome="failed", produced={"fetch_sales": "SalesData"})
    (outcome,) = grade(lineage, (failed,), version=4)
    assert outcome.holds is None


def test_a_run_of_a_different_version_grades_nothing(quarterly_report: Workflow) -> None:
    """A ledger keyed on a name-scoped id hands back the parent's runs; a child must not be graded on them."""
    lineage = _child(quarterly_report, Prediction(claim="produces", subject="publish"))
    (outcome,) = grade(lineage, (_completed(3, produced={"publish": "ReportUrl"}),), version=4)
    assert outcome.holds is None


def test_a_run_that_never_said_which_version_it_exercised_is_not_evidence(quarterly_report: Workflow) -> None:
    """Same ruling the tier makes: an evidence record that cannot say what it ran counts for no version."""
    lineage = _child(quarterly_report, Prediction(claim="produces", subject="publish"))
    anonymous = RunEvidence(workflow_id="wf.report", outcome="completed", produced={"publish": "ReportUrl"})
    (outcome,) = grade(lineage, (anonymous,), version=4)
    assert outcome.holds is None


def test_an_approval_grades_nothing(quarterly_report: Workflow) -> None:
    """A person approving an artifact is not a run of it, and cannot confirm a claim about one."""
    lineage = _child(quarterly_report, Prediction(claim="completes"))
    (outcome,) = grade(lineage, (Approval(workflow_id="wf.report", workflow_version=4, by="someone"),), version=4)
    assert outcome.holds is None


def test_a_verdict_is_never_a_field_anybody_writes(quarterly_report: Workflow) -> None:
    """The ladder rule, one model over: a stored verdict is a verdict a writer could award itself."""
    lineage = _child(quarterly_report, Prediction(claim="completes"))
    assert "holds" not in Prediction.model_fields
    assert not {"holds", "hits", "hit_rate"} & set(Lineage.model_fields)
    with pytest.raises(ValidationError):
        Lineage.model_validate({**lineage.model_dump(mode="json"), "hit_rate": 1.0})


def test_an_adaptation_that_claimed_nothing_grades_to_nothing(quarterly_report: Workflow) -> None:
    assert grade(_child(quarterly_report), (_completed(4),), version=4) == ()


def test_every_claim_is_graded_in_the_order_it_was_made(quarterly_report: Workflow) -> None:
    lineage = _child(quarterly_report, Prediction(claim="completes"), Prediction(claim="produces", subject="publish"))
    outcomes = grade(lineage, (_completed(4),), version=4)
    assert [outcome.prediction.claim for outcome in outcomes] == ["completes", "produces"]


# --- the fold: the adapter is scored, and never does the scoring ----------------------------------


def test_the_fold_counts_hits_misses_and_unmeasurable_separately(quarterly_report: Workflow) -> None:
    """Three states, three counters: folding unmeasurable into misses would report a lie about the adapter."""
    lineage = _child(
        quarterly_report,
        Prediction(claim="completes"),
        Prediction(claim="produces", subject="publish"),
        Prediction(claim="approves", subject="approve"),
    )
    record = prediction_record(grade(lineage, (_completed(4, produced={"draft_report": "ReportDraft"}),), version=4))
    assert (record.hits, record.misses) == (1, 2)
    assert record.unmeasurable == 0
    assert record.hit_rate == pytest.approx(1 / 3)


def test_an_unmeasured_claim_is_left_out_of_the_rate(quarterly_report: Workflow) -> None:
    """A claim nothing has tested must not drag a rate down; it is not in the denominator at all.

    A run that recorded no shapes is the observer's silence rather than the node's, so the claim about
    the node is untested even though the run completed -- the ruling the birth check already makes.
    """
    lineage = _child(quarterly_report, Prediction(claim="completes"), Prediction(claim="produces", subject="publish"))
    record = prediction_record(grade(lineage, (_completed(4),), version=4))
    assert (record.hits, record.misses, record.unmeasurable) == (1, 0, 1)
    assert record.hit_rate == 1.0


def test_a_record_with_nothing_measured_has_no_rate_rather_than_a_zero(quarterly_report: Workflow) -> None:
    """`0.0` reads as an adapter that is always wrong; the truth is that nothing has been measured yet."""
    lineage = _child(quarterly_report, Prediction(claim="produces", subject="publish"))
    record = prediction_record(grade(lineage, (), version=4))
    assert record.hit_rate is None
    assert record.measured == 0


def test_the_fold_spans_every_artifact_it_is_given(quarterly_report: Workflow) -> None:
    """The signal is on the adapter, not on one adaptation, so the fold takes outcomes from as many as you have."""
    ran = (_completed(4, produced={"draft_report": "ReportDraft"}),)
    held = grade(_child(quarterly_report, Prediction(claim="completes")), ran, version=4)
    missed = grade(_child(quarterly_report, Prediction(claim="produces", subject="publish")), ran, version=4)
    record = prediction_record((*held, *missed))
    assert (record.hits, record.misses) == (1, 1)
    assert record.hit_rate == pytest.approx(0.5)


# --- both libraries: the claims survive the round trip, and an empty set survives being dropped ------


def _dropping_empties(recorded: Mapping[str, object]) -> dict[str, object]:
    """A record as a library that keeps no empty container hands it back."""
    return {key: value for key, value in recorded.items() if value not in ("", [], {})}


async def test_a_folder_keeps_the_claims_and_the_prose_beside_the_diff(tmp_path: Path, quarterly_report: Workflow) -> None:
    """A claim the model can write and the library cannot keep is a claim nothing will ever grade."""
    library = FolderLibrary(tmp_path / "workflows")
    child, lineage = adapt(
        quarterly_report,
        ops=({"op": "remove_node", "id": "publish"}, {"op": "set_output", "to": {"node": "approve"}}),
        evidence="Publishing kept firing on drafts nobody had read, so the gate answers instead.",
        predictions=(Prediction(claim="completes"), Prediction(claim="approves", subject="approve")),
    )
    child_id = await library.put(child, title="Q", summary="s", applicability="a", provenance={"saved_by": "test"}, lineage=lineage)

    kept = await library.fetch_lineage(child_id)
    assert kept is not None
    assert kept.evidence.startswith("Publishing kept firing")
    assert kept.predictions == lineage.predictions


async def test_a_folder_record_whose_empty_claims_went_missing_still_reads_as_an_adaptation(
    tmp_path: Path, quarterly_report: Workflow
) -> None:
    """The trap the empty stamp and the empty `touched` both fell into, one model further out.

    An adaptation may honestly claim nothing and say nothing about why, and a library that keeps no
    empty container hands the record back without either key. Required, both would fail to validate
    and the child would read as an artifact nobody adapted -- with its parent link and its diff
    sitting right there in the file.
    """
    library = FolderLibrary(tmp_path / "workflows")
    child, lineage = adapt(quarterly_report, ops=({"op": "set_description", "to": "Now with commentary."},))
    child_id = await library.put(child, title="Q", summary="s", applicability="a", provenance={"saved_by": "test"}, lineage=lineage)

    path = next((tmp_path / "workflows").glob("*-v4.md"))
    frontmatter = dict(load_spec_document(path.read_text(encoding="utf-8")).frontmatter)
    recorded = frontmatter["lineage"]
    assert isinstance(recorded, dict)
    frontmatter["lineage"] = _dropping_empties(recorded)
    assert "predictions" not in frontmatter["lineage"], "the library really did drop them"
    assert "evidence" not in frontmatter["lineage"]
    path.write_text(dump_spec_document(child, frontmatter=frontmatter), encoding="utf-8")

    kept = await library.fetch_lineage(child_id)
    assert kept is not None, "an adaptation that claimed nothing is still an adaptation"
    assert kept.predictions == ()
    assert kept.evidence == ""


class _OnePage:
    """The two reads the collection makes, over the one page a test wrote."""

    def __init__(self, frontmatter: Mapping[str, object] | None = None) -> None:
        self.frontmatter = frontmatter

    async def fetch(self, sql: str, /, *args: object) -> Sequence[Mapping[str, Any]]:
        """No parent page, so a save links nothing forward."""
        del sql, args
        return ()

    async def fetchval(self, sql: str, /, *args: object) -> str | Mapping[str, Any] | None:
        """The page's frontmatter document, whatever a test seeded it with."""
        del sql, args
        return self.frontmatter  # type: ignore[return-value]


async def test_the_collection_keeps_the_claims_and_the_prose_beside_the_diff(quarterly_report: Workflow) -> None:
    """Same invariant in the library the app runs on: what a model claimed comes back as what it claimed."""
    written: dict[str, Mapping[str, Any]] = {}

    async def ingest(payload: Mapping[str, Any], *, collection: str) -> None:
        del collection
        written["frontmatter"] = payload["frontmatter"]

    child, lineage = adapt(
        quarterly_report,
        ops=({"op": "set_description", "to": "Now with commentary."},),
        evidence="The summary read as a verdict, so it says what it left out.",
        predictions=(Prediction(claim="produces", subject="draft_report", shape="ReportDraft"),),
    )
    await LibraryCollection(_OnePage(), ingest).put(
        child, title="Q", summary="s", applicability="a", provenance={"saved_by": "test"}, lineage=lineage
    )

    reading = LibraryCollection(_OnePage(written["frontmatter"]), ingest)
    kept = await reading.fetch_lineage("wf.quarterly-report.v4")
    assert kept is not None
    assert kept.evidence.startswith("The summary read as a verdict")
    assert kept.predictions == (Prediction(claim="produces", subject="draft_report", shape="ReportDraft"),)


async def test_a_collection_record_whose_empty_claims_were_dropped_still_reads_as_an_adaptation(quarterly_report: Workflow) -> None:
    """Engram's ingest keeps no empty container, so an adaptation that claimed nothing loses both keys."""
    written: dict[str, Mapping[str, Any]] = {}

    async def ingest_dropping_empties(payload: Mapping[str, Any], *, collection: str) -> None:
        del collection
        frontmatter = dict(payload["frontmatter"])
        written["frontmatter"] = {**frontmatter, "lineage": _dropping_empties(frontmatter["lineage"])}

    child, lineage = adapt(quarterly_report, ops=({"op": "set_description", "to": "Now with commentary."},))
    await LibraryCollection(_OnePage(), ingest_dropping_empties).put(
        child, title="Q", summary="s", applicability="a", provenance={"saved_by": "test"}, lineage=lineage
    )
    assert "predictions" not in written["frontmatter"]["lineage"], "the library really did drop them"
    assert "evidence" not in written["frontmatter"]["lineage"]

    reading = LibraryCollection(_OnePage(written["frontmatter"]), ingest_dropping_empties)
    kept = await reading.fetch_lineage("wf.quarterly-report.v4")
    assert kept is not None, "an adaptation that claimed nothing is still an adaptation"
    assert (kept.predictions, kept.evidence) == ((), "")


# --- reachable: the model can state a claim, and a person can read how it is doing ----------------


@pytest.fixture
def port(tmp_path: Path, registry: StepRegistry) -> WorkflowContract[None]:
    """The contract over a directory, with the registry that can verify the report's steps."""
    return WorkflowContract[None](
        source=folder_toolset(tmp_path / "workflows"), vocabulary=FOLDER, expose=(), registry=registry, saved_by="test"
    )


def _yaml(spec: Workflow) -> str:
    """`spec` as the YAML text `save_workflow`'s `spec_yaml` argument now takes."""
    return yaml.safe_dump(spec.model_dump(mode="json"), sort_keys=False)


async def _call(port: WorkflowContract[None], name: str, **args: object):
    toolset = port.get_toolset()
    assert toolset is not None
    ctx: RunContext[None] = RunContext(deps=None, model=TestModel(), usage=RunUsage())
    tools = await toolset.get_tools(ctx)
    return await toolset.call_tool(name, dict(args), ctx, tools[name])


async def test_the_port_carries_the_reason_and_the_claim_into_the_store(
    port: WorkflowContract[None], quarterly_report: Workflow, tmp_path: Path
) -> None:
    """A claim a model can write but no library keeps is a claim nothing will ever grade.

    The agent authors most adaptations, so the plugin it reaches the library through is where evidence and
    predictions have to be askable -- and they travel on the lineage the machinery derived, never on a
    parentage the model asserted.
    """
    await _call(port, SAVE_TOOL, spec_yaml=_yaml(quarterly_report), applicability="Use when a quarter closes. Do not use mid-quarter.")
    answer = await _call(
        port,
        ADAPT_TOOL,
        workflow_id="quarterly-report@3",
        ops=[{"op": "set_description", "to": "Fetch, reconcile, draft, approve, publish -- with the costs split out."}],
        applicability="Use when a quarter closes. Do not use mid-quarter.",
        evidence="Reconciliation hid which side moved, so costs are drafted separately.",
        predictions=[{"claim": "produces", "subject": "draft_report", "shape": "ReportDraft"}],
    )
    assert "quarterly-report@4" in answer, answer

    kept = await FolderLibrary(tmp_path / "workflows").fetch_lineage("quarterly-report@4")
    assert kept is not None
    assert kept.evidence.startswith("Reconciliation hid")
    assert kept.predictions == (Prediction(claim="produces", subject="draft_report", shape="ReportDraft"),)


async def test_an_adaptation_that_states_nothing_is_still_an_adaptation(
    port: WorkflowContract[None], quarterly_report: Workflow, tmp_path: Path
) -> None:
    """Stating why and claiming what are encouraged, not compulsory: neither may block an edit."""
    await _call(port, SAVE_TOOL, spec_yaml=_yaml(quarterly_report), applicability="Use when a quarter closes. Do not use mid-quarter.")
    await _call(
        port,
        ADAPT_TOOL,
        workflow_id="quarterly-report@3",
        ops=[{"op": "set_description", "to": "Now with commentary."}],
        applicability="Use when a quarter closes. Do not use mid-quarter.",
    )
    kept = await FolderLibrary(tmp_path / "workflows").fetch_lineage("quarterly-report@4")
    assert kept is not None
    assert (kept.evidence, kept.predictions) == ("", ())


# --- the reachable end is a pixel: a person opening the child reads how its claims are doing ------


class _NoAgent:
    """The artifact view never runs the agent; the session just has to hold one."""


def _install(registry: StepRegistry, library: FolderLibrary, tmp_path: Path) -> tuple[Runtime, BrowsingController]:
    """Install the process's own session, and hand back that runtime alongside a controller over it."""
    runtime = Runtime(registry=registry, library=library, agent=_NoAgent(), session_log=NoSessionLog(), state=a_state(tmp_path))  # type: ignore[arg-type]
    return runtime, BrowsingController(
        sessions=FakeSessionPool(runtime),
        archive=records_under(tmp_path).archive,
        spend=records_under(tmp_path).spend,
        evidence=records_under(tmp_path).evidence,
        self_authoring=SelfAuthoringSettings(custom_root=tmp_path / "custom"),
    )


async def _body(controller: BrowsingController, workflow_id: str) -> dict[str, Any]:
    """The artifact view's JSON, read the way the browser reads it."""
    return json.loads(bytes((await controller.stored_workflow(workflow_id)).body))


async def _saved_child(library: FolderLibrary, spec: Workflow, registry: StepRegistry, *predictions: Prediction, evidence: str) -> str:
    """The report, adapted to stop before publishing, with whatever its adapter claimed."""
    await save(spec, library, registry, title="Q", summary="s", applicability="a", saved_by="test")
    child, lineage = adapt(
        spec,
        ops=({"op": "remove_node", "id": "publish"}, {"op": "set_output", "to": {"node": "approve"}}),
        evidence=evidence,
        predictions=predictions,
    )
    return await save(child, library, registry, title="Q", summary="s", applicability="a", saved_by="test", lineage=lineage)


async def test_a_person_opening_a_child_reads_why_it_exists_and_how_its_claims_are_doing(
    tmp_path: Path, quarterly_report: Workflow, registry: StepRegistry
) -> None:
    """A claim only the model can see is a claim the person cannot act on -- the same defect as never grading it."""
    records = records_under(tmp_path)
    library = FolderLibrary(tmp_path / "workflows")
    child_id = await _saved_child(
        library,
        quarterly_report,
        registry,
        Prediction(claim="produces", subject="draft_report", shape="ReportDraft"),
        evidence="Publishing fired on drafts nobody had read, so the gate answers instead.",
    )
    await records.evidence.append(
        RunEvidence(
            workflow_id=child_id,
            workflow_version=quarterly_report.version + 1,
            outcome="completed",
            produced={"draft_report": "ReportDraft"},
            runner="test",
        )
    )
    _, controller = _install(registry, library, tmp_path)

    body = await _body(controller, child_id)
    assert body["lineage"]["evidence"].startswith("Publishing fired"), "the reason travels to the person who asks"
    (shown,) = body["predictions"]
    assert shown["holds"] is True
    assert "HOLDS" in shown["reads"]
    assert "draft_report" in shown["reads"]
    assert body["prediction_record"] == {"hits": 1, "misses": 0, "unmeasurable": 0, "hit_rate": 1.0}


async def test_a_child_nothing_has_run_reads_unmeasurable_rather_than_missed(
    tmp_path: Path, quarterly_report: Workflow, registry: StepRegistry
) -> None:
    """The tri-state at the one place a person meets it: a version nobody ran has not failed anything."""
    library = FolderLibrary(tmp_path / "workflows")
    child_id = await _saved_child(library, quarterly_report, registry, Prediction(claim="completes"), evidence="")
    _, controller = _install(registry, library, tmp_path)

    body = await _body(controller, child_id)
    (shown,) = body["predictions"]
    assert shown["holds"] is None
    assert "UNMEASURABLE" in shown["reads"]
    assert body["prediction_record"]["hit_rate"] is None, "no rate is not a rate of zero"


async def test_a_run_of_the_parent_s_version_grades_none_of_the_child_s_claims(
    tmp_path: Path, quarterly_report: Workflow, registry: StepRegistry
) -> None:
    """A library may key its ledger on a name rather than a version, and the child must not inherit the runs."""
    records = records_under(tmp_path)
    library = FolderLibrary(tmp_path / "workflows")
    child_id = await _saved_child(library, quarterly_report, registry, Prediction(claim="completes"), evidence="")
    await records.evidence.append(
        RunEvidence(workflow_id=child_id, workflow_version=quarterly_report.version, outcome="completed", runner="test")
    )
    _, controller = _install(registry, library, tmp_path)

    (shown,) = (await _body(controller, child_id))["predictions"]
    assert shown["holds"] is None, "a run of v3 says nothing about what v4 claims"


async def test_an_artifact_that_claimed_nothing_shows_no_scoreboard(
    tmp_path: Path, quarterly_report: Workflow, registry: StepRegistry
) -> None:
    """`null`, not an empty table: nothing to grade is a different statement from grading nothing."""
    library = FolderLibrary(tmp_path / "workflows")
    workflow_id = await save(quarterly_report, library, registry, title="Q", summary="s", applicability="a", saved_by="test")
    _, controller = _install(registry, library, tmp_path)

    body = await _body(controller, workflow_id)
    assert body["predictions"] is None
    assert body["prediction_record"] is None


async def test_the_app_s_own_door_carries_the_reason_and_the_claim_all_the_way_to_the_store(
    tmp_path: Path, quarterly_report: Workflow, registry: StepRegistry
) -> None:
    """Two plugins, one record: the button a person presses has to keep what the agent's tool keeps.

    The gesture is split -- adapting makes a draft, saving keeps it -- so the claim has to survive
    the draft in between, exactly as the parent link does. Dropped there, an adaptation made in the
    studio would arrive in the library having claimed nothing while the agent's did not.
    """
    library = FolderLibrary(tmp_path / "workflows")
    # The parent arrives described, because a child inherits its words and an undescribed one
    # would be refused at the save this case is about.
    await save(
        quarterly_report,
        library,
        registry,
        title="Quarterly report",
        summary="Closes one quarter's books.",
        applicability="Use at a quarter end. Not for a single month.",
        saved_by="test",
        presentation=WorkflowPresentation(
            purpose="Close one quarter's books.",
            produces="The published quarterly report.",
            owner_participation="Approve the draft before it is published.",
            not_for="A question about a single month.",
        ),
    )
    runtime, _ = _install(registry, library, tmp_path)
    controller = a_controller(SessionPool(runtime), state=tmp_path / "state")

    adapted = json.loads(
        bytes(
            (
                await controller.adapt_saved_workflow(
                    AdaptRequest(
                        workflow_id="quarterly-report@3",
                        ops=[{"op": "set_description", "to": "Now with the costs split out."}],
                        evidence="One number hid which side moved.",
                        predictions=[Prediction(claim="produces", subject="draft_report")],
                    )
                )
            ).body
        )
    )
    assert adapted["lineage"]["evidence"] == "One number hid which side moved."

    saved = json.loads(bytes((await controller.save_current()).body))
    assert saved["ok"] is True, saved
    kept = await library.fetch_lineage(saved["id"])
    assert kept is not None
    assert kept.evidence == "One number hid which side moved."
    assert kept.predictions == (Prediction(claim="produces", subject="draft_report"),)
