"""Opening one saved workflow: does it fit here, has the environment moved, and where did it come from.

The three questions are the same question at different distances, and the person browsing the
library was only being answered the first of them. Staleness and lineage existed, were tested, and
were reachable *by the agent* through the contract port -- `fetch_workflow` renders both --
while the human clicking the same artifact in the library panel got neither. An audit that finds
the machinery built and then finds half of it unreachable has not finished.

Staleness especially: it is the failure mode a library of intent actually dies of, and a verdict
only a model can see is a verdict the person cannot act on.
"""

from __future__ import annotations

import json

from typing import TYPE_CHECKING, Any

import pytest

from core.ai.workflows.contracts import Grade, Outcome, RunEvidence, StepRegistry, Workflow, WorkflowFingerprint, adapt, save, spec_identity
from core.ai.workflows.infrastructure.folder import FolderLibrary

from harness.adapters.model_tiers import environment_stamps
from harness.features.browsing.controller import BrowsingController
from harness.features.selfauthoring.infrastructure.capabilities import FileCustomCapabilities
from harness.features.selfauthoring.infrastructure.promotion import promote_capability
from harness.session import NoSessionLog, Runtime
from harness.settings import SelfAuthoringSettings
from tests.conftest import CallLog, register_quarterly_steps
from tests.support.fakes.sessions import FakeSessionPool
from tests.support.selfauthoring import FakeActivationLog
from tests.support.state import a_state, records_under

if TYPE_CHECKING:
    from pathlib import Path


_PROMOTABLE_SOURCE = """
from dataclasses import dataclass

from pydantic_ai.capabilities import AbstractCapability


@dataclass
class Marker(AbstractCapability):
    '''Meets the promotion contract: a declared serialization name and a dataclass on the class itself.'''
"""


class _NoAgent:
    """The artifact view never runs the agent; the session just has to hold one."""


@pytest.fixture
def library(tmp_path: Path) -> FolderLibrary:
    return FolderLibrary(tmp_path / "workflows")


def _install(registry: StepRegistry, library: FolderLibrary, home: Path) -> BrowsingController:
    """A controller over one runtime, holding `registry` and `library` as the session it serves."""
    runtime = Runtime(registry=registry, library=library, agent=_NoAgent(), session_log=NoSessionLog(), state=a_state(home))  # type: ignore[arg-type]
    records = records_under(home)
    return BrowsingController(
        sessions=FakeSessionPool(runtime),
        archive=records.archive,
        spend=records.spend,
        evidence=records.evidence,
        self_authoring=SelfAuthoringSettings(custom_root=home / "custom"),
    )


async def _body(controller: BrowsingController, workflow_id: str) -> dict[str, Any]:
    """The endpoint's JSON, read the way the browser reads it."""
    return json.loads(bytes((await controller.stored_workflow(workflow_id)).body))


async def test_a_fresh_artifact_says_so_rather_than_saying_nothing(library: FolderLibrary, quarterly_report: Workflow, registry: StepRegistry, tmp_path: Path) -> None:
    """`[]` is a verdict -- checked and fresh -- and it has to be distinguishable from silence."""
    workflow_id = await save(quarterly_report, library, registry, title="Q", summary="s", applicability="a", saved_by="test")
    controller = _install(registry, library, tmp_path)

    body = await _body(controller, workflow_id)
    assert body["staleness"] == []
    assert body["fits"] is True


async def test_an_artifact_whose_environment_moved_names_what_moved(library: FolderLibrary, quarterly_report: Workflow, registry: StepRegistry, tmp_path: Path) -> None:
    """The whole point of a stamp: the drift is named, in prose the person can act on."""
    workflow_id = await save(
        quarterly_report,
        library,
        registry,
        title="Q",
        summary="s",
        applicability="a",
        saved_by="test",
        environment={"promoted_capabilities": "digest-one"},
    )
    controller = _install(registry, library, tmp_path)
    # A capability promoted under the controller's own custom root, so the host's real stamp moves.
    store = FileCustomCapabilities(tmp_path / "custom" / "capabilities", activations=FakeActivationLog())
    store.write("marker", _PROMOTABLE_SOURCE)
    assert store.activate("marker") is True
    promote_capability(store, "marker")
    moved = environment_stamps(tmp_path / "custom")["promoted_capabilities"]

    body = await _body(controller, workflow_id)
    assert body["staleness"], "the promoted-capabilities digest moved, so this is not fresh"
    assert any("digest-one" in finding and moved in finding for finding in body["staleness"])


async def test_an_artifact_that_predates_stamps_reads_as_unknown_not_fresh(library: FolderLibrary, quarterly_report: Workflow, registry: StepRegistry, tmp_path: Path) -> None:
    """The tri-state, at the one place a person meets it. `null` is not `[]`."""
    workflow_id = await library.put(quarterly_report, title="Q", summary="s", applicability="a", provenance={"saved_by": "test"})
    controller = _install(registry, library, tmp_path)

    body = await _body(controller, workflow_id)
    assert body["staleness"] is None, "no stamp is unknown, and unknown must not render as fresh"


async def test_a_step_that_changed_its_contract_is_named(library: FolderLibrary, quarterly_report: Workflow, registry: StepRegistry, call_log: CallLog, tmp_path: Path) -> None:
    """Drift that is not a host stamp: a registered step whose signature moved."""
    workflow_id = await save(quarterly_report, library, registry, title="Q", summary="s", applicability="a", saved_by="test")
    thinner = StepRegistry()
    register_quarterly_steps(thinner, call_log, exclude=frozenset({"publish"}))
    controller = _install(thinner, library, tmp_path)

    body = await _body(controller, workflow_id)
    assert any("publish" in finding for finding in body["staleness"] or ())


async def test_an_adapted_artifact_shows_where_it_came_from(library: FolderLibrary, quarterly_report: Workflow, registry: StepRegistry, tmp_path: Path) -> None:
    """Supersession is a link, and a link nobody can follow is not a link."""
    await save(quarterly_report, library, registry, title="Q", summary="s", applicability="a", saved_by="test")
    child, lineage = adapt(quarterly_report, ops=({"op": "set_description", "to": "Now with commentary."},))
    child_id = await save(child, library, registry, title="Q", summary="s", applicability="a", saved_by="test", lineage=lineage)
    controller = _install(registry, library, tmp_path)

    body = await _body(controller, child_id)
    assert body["lineage"]["parent_name"] == quarterly_report.name
    assert body["lineage"]["parent_version"] == quarterly_report.version
    assert body["lineage"]["ops"] == [{"op": "set_description", "to": "Now with commentary."}]


async def test_the_parent_is_named_by_an_id_the_reader_can_open(library: FolderLibrary, quarterly_report: Workflow, registry: StepRegistry, tmp_path: Path) -> None:
    """A name and a version are not an address, and following the link is the whole gesture.

    The library mints ids, so the library is asked to resolve one; a route that assembled
    `f"{name}@{version}"` itself would be encoding one adapter's slug convention into a view that
    is supposed to work over any of them.
    """
    parent_id = await save(quarterly_report, library, registry, title="Q", summary="s", applicability="a", saved_by="test")
    child, lineage = adapt(quarterly_report, ops=({"op": "set_description", "to": "Now with commentary."},))
    child_id = await save(child, library, registry, title="Q", summary="s", applicability="a", saved_by="test", lineage=lineage)
    controller = _install(registry, library, tmp_path)

    body = await _body(controller, child_id)
    assert body["lineage"]["parent_id"] == parent_id
    assert (await _body(controller, parent_id))["ok"] is True, "the id the child points at really opens"


async def test_a_parent_the_store_no_longer_holds_is_not_offered_as_a_link(library: FolderLibrary, quarterly_report: Workflow, registry: StepRegistry, tmp_path: Path) -> None:
    """A dead link is worse than none: it invites a click that returns a 404 and teaches nothing.

    The lineage itself still renders -- the child does know what it came from -- but the address
    is `null`, because the library was asked and had no page to give.
    """
    child, lineage = adapt(quarterly_report, ops=({"op": "set_description", "to": "Now with commentary."},))
    child_id = await save(child, library, registry, title="Q", summary="s", applicability="a", saved_by="test", lineage=lineage)
    controller = _install(registry, library, tmp_path)

    body = await _body(controller, child_id)
    assert body["lineage"]["parent_name"] == quarterly_report.name, "the child still records where it came from"
    assert body["lineage"]["parent_id"] is None


async def test_an_artifact_nobody_adapted_has_no_lineage(library: FolderLibrary, quarterly_report: Workflow, registry: StepRegistry, tmp_path: Path) -> None:
    workflow_id = await save(quarterly_report, library, registry, title="Q", summary="s", applicability="a", saved_by="test")
    controller = _install(registry, library, tmp_path)
    assert (await _body(controller, workflow_id))["lineage"] is None


async def test_a_stamp_that_no_longer_parses_is_unknown_rather_than_a_crash(library: FolderLibrary, quarterly_report: Workflow, registry: StepRegistry, tmp_path: Path) -> None:
    """A library can hand back anything; the view still has to answer."""
    workflow_id = await library.put(
        quarterly_report,
        title="Q",
        summary="s",
        applicability="a",
        provenance={"saved_by": "test"},
        fingerprint=WorkflowFingerprint(grants="abc"),
    )
    path = next((library._root).glob("*.md"))  # type: ignore[attr-defined]
    path.write_text(path.read_text(encoding="utf-8").replace("grants: abc", "grants: {oh: dear}"), encoding="utf-8")
    controller = _install(registry, library, tmp_path)

    assert (await _body(controller, workflow_id))["staleness"] is None


async def _record_evidence(spec: Workflow, workflow_id: str, produced: dict[str, str], tmp_path: Path) -> None:
    """Write one completed run into the ledger the view reads."""
    await records_under(tmp_path).evidence.append(
        RunEvidence(workflow_id=workflow_id, spec_hash=spec_identity(spec), outcome="completed", produced=produced, runner="test")
    )


async def test_an_artifact_that_still_does_what_it_did_says_so(library: FolderLibrary, quarterly_report: Workflow, registry: StepRegistry, tmp_path: Path) -> None:
    """The birth check has been recording evidence on every run and nothing ever read it.

    `service.launch` writes `produced` -- the shape of every value a run put out -- into each
    evidence record, with a comment saying a later `check_birth` can ask whether the artifact still
    does what it once did. Nothing called `check_birth`. The evidence was being collected for a
    reader that did not exist, which is the most expensive kind of unbuilt: it looks finished.
    """
    workflow_id = await save(quarterly_report, library, registry, title="Q", summary="s", applicability="a", saved_by="test")
    await _record_evidence(quarterly_report, workflow_id, {"fetch_sales": "SalesData", "reconcile": "Ledger", "publish": "ReportUrl"}, tmp_path)
    controller = _install(registry, library, tmp_path)

    assert (await _body(controller, workflow_id))["birth"] == []


async def test_an_artifact_that_lost_a_node_it_was_born_producing_says_which(library: FolderLibrary, quarterly_report: Workflow, registry: StepRegistry, tmp_path: Path) -> None:
    """Trust is about a thing that ran. Change what it is, and the trust is about something else."""
    workflow_id = await save(quarterly_report, library, registry, title="Q", summary="s", applicability="a", saved_by="test")
    await _record_evidence(quarterly_report, workflow_id, {"publish": "ReportUrl", "long_gone": "Whatever"}, tmp_path)
    controller = _install(registry, library, tmp_path)

    findings = (await _body(controller, workflow_id))["birth"]
    assert findings
    assert any("long_gone" in finding for finding in findings)


async def test_an_artifact_nobody_has_run_has_no_birth_to_check(library: FolderLibrary, quarterly_report: Workflow, registry: StepRegistry, tmp_path: Path) -> None:
    """No attested run is not a failed birth check; it is the absence of one."""
    workflow_id = await save(quarterly_report, library, registry, title="Q", summary="s", applicability="a", saved_by="test")
    controller = _install(registry, library, tmp_path)

    assert (await _body(controller, workflow_id))["birth"] is None


async def test_a_run_that_failed_is_not_birth_evidence(library: FolderLibrary, quarterly_report: Workflow, registry: StepRegistry, tmp_path: Path) -> None:
    """A run that fell over never demonstrated the artifact does anything."""
    records = records_under(tmp_path)
    workflow_id = await save(quarterly_report, library, registry, title="Q", summary="s", applicability="a", saved_by="test")
    await records.evidence.append(
        RunEvidence(
            workflow_id=workflow_id, spec_hash=spec_identity(quarterly_report), outcome="failed", produced={"fetch_sales": "SalesData"}, runner="test"
        )
    )
    controller = _install(registry, library, tmp_path)

    assert (await _body(controller, workflow_id))["birth"] is None


async def test_a_child_that_never_ran_says_which_nodes_its_parent_vouched_for(library: FolderLibrary, quarterly_report: Workflow, registry: StepRegistry, tmp_path: Path) -> None:
    """`carried_trust` is the last piece of this machinery that had no caller outside the tests.

    It answers the question a person actually has when a v2 with no runs of its own is on screen:
    "how much of this is new?" The parent ran; the diff says which nodes it left alone; those
    nodes are literally the nodes that were attested. Without it a child reads `unverified` and
    nothing distinguishes "one node changed" from "rewritten from scratch" -- which is the whole
    difference a typed diff exists to record.

    Not a tier. The child has earned nothing, and this does not pretend otherwise; it reports what
    its parent's evidence still describes.
    """
    records = records_under(tmp_path)
    parent_id = await save(quarterly_report, library, registry, title="Q", summary="s", applicability="a", saved_by="test")
    await records.evidence.append(
        RunEvidence(
            workflow_id=parent_id,
            spec_hash=spec_identity(quarterly_report),
            workflow_version=quarterly_report.version,
            outcome="completed",
            produced={"fetch_sales": "SalesData", "fetch_costs": "CostData", "reconcile": "Ledger", "publish": "ReportUrl"},
        )
    )
    child, lineage = adapt(quarterly_report, ops=({"op": "remove_node", "id": "publish"}, {"op": "set_output", "to": {"node": "approve"}}))
    child_id = await save(child, library, registry, title="Q", summary="s", applicability="a", saved_by="test", lineage=lineage)
    controller = _install(registry, library, tmp_path)

    body = await _body(controller, child_id)
    assert body["carried"] == ["fetch_costs", "fetch_sales", "reconcile"]
    assert "publish" not in (body["carried"] or ()), "the node the diff removed carries nothing"


async def test_an_artifact_with_no_parent_carries_nothing_rather_than_an_empty_promise(library: FolderLibrary, quarterly_report: Workflow, registry: StepRegistry, tmp_path: Path) -> None:
    """`null`, not `[]`. Nothing to inherit is a different statement from inheriting nothing."""
    workflow_id = await save(quarterly_report, library, registry, title="Q", summary="s", applicability="a", saved_by="test")
    controller = _install(registry, library, tmp_path)
    assert (await _body(controller, workflow_id))["carried"] is None


async def test_runs_earned_under_another_address_still_count_for_the_artifact(library: FolderLibrary, quarterly_report: Workflow, registry: StepRegistry, tmp_path: Path) -> None:
    """The defect, at the one screen a person meets it on: the artifact moved and its trust did not follow.

    The run below was attested when the artifact lived somewhere else -- another library, or this one
    before it re-keyed. Keyed on the address it would be invisible and the view would say
    `unverified` with a straight face. Keyed on the spec, it is the same artifact and the same run.
    """
    records = records_under(tmp_path)
    workflow_id = await save(quarterly_report, library, registry, title="Q", summary="s", applicability="a", saved_by="test")
    await records.evidence.append(
        RunEvidence(
            workflow_id="wf.somewhere.else.v3",
            spec_hash=spec_identity(quarterly_report),
            outcome="completed",
            result=Outcome(status="succeeded", summary="the report was published"),
        )
    )
    controller = _install(registry, library, tmp_path)

    trust = (await _body(controller, workflow_id))["trust"]
    assert trust["level"] == "machine-confirmed"
    assert trust["runs_with_evidence"] == 1


async def test_an_artifact_nothing_has_ever_recorded_says_exactly_that(library: FolderLibrary, quarterly_report: Workflow, registry: StepRegistry, tmp_path: Path) -> None:
    """No record at all reads unverified with nothing counted."""
    workflow_id = await save(quarterly_report, library, registry, title="Q", summary="s", applicability="a", saved_by="test")
    controller = _install(registry, library, tmp_path)

    trust = (await _body(controller, workflow_id))["trust"]
    assert trust["level"] == "unverified"
    assert trust["runs_with_evidence"] == 0
    assert trust["failed_runs"] == 0


async def test_a_child_cannot_inherit_its_parents_runs_through_the_hash(library: FolderLibrary, quarterly_report: Workflow, registry: StepRegistry, tmp_path: Path) -> None:
    """Content addressing must not reopen the laundering route the version filter closed.

    The parent's run is attested at the child's own address -- the exact shape a name-scoped library
    produces. It names the parent's spec, so it is known to be about something else and is not counted.
    """
    records = records_under(tmp_path)
    child, lineage = adapt(quarterly_report, ops=({"op": "set_description", "to": "Now with commentary."},))
    child_id = await save(child, library, registry, title="Q", summary="s", applicability="a", saved_by="test", lineage=lineage)
    await records.evidence.append(
        RunEvidence(
            workflow_id=child_id,
            spec_hash=spec_identity(quarterly_report),
            outcome="completed",
            result=Outcome(status="succeeded", summary="the report was published"),
        )
    )
    controller = _install(registry, library, tmp_path)

    trust = (await _body(controller, child_id))["trust"]
    assert trust["level"] == "unverified"
    assert trust["runs_with_evidence"] == 0, "the parent's run said what it exercised, and it was not this"


async def test_grades_ride_beside_the_tier_without_moving_it(library: FolderLibrary, quarterly_report: Workflow, registry: StepRegistry, tmp_path: Path) -> None:
    """Reliable and right are different claims -- a grade must reach the view without touching `tier`.

    Two structural grades, no completed run: the tier stays `unverified` (nothing ever finished),
    while the grade fields report what was measured. A reader who only saw `tier` would not know
    anything had been scored at all.
    """
    records = records_under(tmp_path)
    workflow_id = await save(quarterly_report, library, registry, title="Q", summary="s", applicability="a", saved_by="test")
    ledger = records.evidence
    for score in (0.8, 0.6):
        await ledger.append(
            Grade(
                workflow_id=workflow_id,
                spec_hash=spec_identity(quarterly_report),
                judge_kind="structural",
                judge="recall/groundtruth-research-v1",
                measure="evidence-recall",
                score=score,
            )
        )
    controller = _install(registry, library, tmp_path)

    trust = (await _body(controller, workflow_id))["trust"]
    assert trust["level"] == "unverified", "a grade is not a completed run"
    assert trust["structural_grades"] == 2
    assert trust["structural_mean"] == 0.7
    assert trust["structural_last"] == 0.6
