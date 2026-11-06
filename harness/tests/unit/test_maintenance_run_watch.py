"""A step maintenance run that ended at its plan judge's last rejection raises one `plan_rejected` flag per changed step."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING

from core.ai.workflows.contracts import (
    Briefing,
    FromNode,
    FromParameter,
    JudgeGate,
    LibraryCollection,
    Outcome,
    ParameterSpec,
    RunEvidence,
    RunNode,
    Shape,
    Workflow,
    spec_identity,
    step_content_address,
)

from harness.features.maintenance.contracts import MAINTENANCE_WORKFLOW, PLAN_GATE, MaintenanceRunWatch
from harness.session import SessionPool
from tests.support.fakes.library_collection import FakeCollection
from tests.support.fakes.sessions import APool
from tests.support.runtime import a_runtime

if TYPE_CHECKING:
    from collections.abc import Mapping

T0 = datetime(2026, 11, 5, 9, 0, tzinfo=UTC)
TEXT = Shape(json_schema={"type": "string"})
STEPS = {
    ("a", 2): Briefing(name="a", instructions="Summarise the whole source in one short paragraph.", inputs={"text": TEXT}, output=TEXT),
    ("b", 5): Briefing(name="b", instructions="List every claim the source makes, one per line.", inputs={"text": TEXT}, output=TEXT),
}


def _maintenance(name: str = MAINTENANCE_WORKFLOW) -> Workflow:
    """The smallest workflow shaped like step maintenance: a planner, and its plan judge with three attempts."""
    return Workflow(
        name=name,
        version=1,
        description="Check new reusable step versions against their consumers.",
        parameters={"changed_step_ids": ParameterSpec(description="the changed steps", json_schema={"type": "string"})},
        nodes=(
            RunNode(id="plan_changes", work="plan_changes", inputs={"changed": FromParameter(parameter="changed_step_ids")}),
            JudgeGate(id=PLAN_GATE, work="judge", reviews=FromNode(node="plan_changes"), rejected_to="plan_changes"),
        ),
        output=FromNode(node=PLAN_GATE),
    )


def _ended(
    workflow: Workflow, *, outcome: str = "failed", rejected: Mapping[str, int] | None = None, changed: str = "st.a.v2,st.b.v5"
) -> RunEvidence:
    return RunEvidence.model_validate({
        "workflow_id": f"wf.{workflow.name}.v{workflow.version}",
        "spec_hash": spec_identity(workflow),
        "workflow_version": workflow.version,
        "outcome": outcome,
        "result": Outcome(status="failed" if outcome == "failed" else "succeeded", summary="what the run did"),
        "gates_auto": (),
        "gates_orchestrated": (),
        "gates_rejected": dict(rejected if rejected is not None else {PLAN_GATE: 3}),
        "params": {"changed_step_ids": changed},
        "runner": "app:schedule:7",
        "at": T0,
    })


async def _world(*workflows: Workflow) -> tuple[FakeCollection, LibraryCollection, MaintenanceRunWatch]:
    store = FakeCollection(prune=True)
    library = LibraryCollection(store, store.ingest, collection=store.collection)
    for (_, version), step in STEPS.items():
        await library.put_step(step, version=version, title="Step", summary="s", applicability="a", provenance={"saved_by": "owner:cli"})
    for workflow in workflows:
        await library.put(workflow, title="Workflow", summary="s", applicability="a", provenance={"saved_by": "owner:cli"})
    return store, library, MaintenanceRunWatch(APool(SessionPool(a_runtime(library=library))))


async def test_a_run_its_plan_judge_rejected_to_the_end_flags_each_changed_step() -> None:
    workflow = _maintenance()
    _, library, watch = await _world(workflow)
    run = _ended(workflow)

    await watch.run_ended(run)

    for name, step_id in (("a", "st.a.v2"), ("b", "st.b.v5")):
        flag = await library.fetch_flag(f"mf.rejected.{name}")
        assert flag is not None
        assert (flag.kind, flag.step, flag.step_id, flag.workflow) == ("plan_rejected", name, step_id, "")
        assert flag.step_hash == step_content_address(STEPS[name, int(step_id.rsplit("v", 1)[1])])
        assert (flag.raised_by, flag.raised_at, flag.status) == ("agent:step-maintenance", run.at, "open")
        assert run.at.isoformat() in flag.evidence
        assert "3 of 3" in flag.evidence


async def test_fewer_rejections_a_completed_run_and_another_workflow_write_nothing() -> None:
    maintenance = _maintenance()
    other = _maintenance("digest-sources")
    store, _, watch = await _world(maintenance, other)
    written = len(store.payloads)

    await watch.run_ended(_ended(maintenance, rejected={PLAN_GATE: 2}))
    await watch.run_ended(_ended(maintenance, outcome="completed"))
    await watch.run_ended(_ended(other))

    assert len(store.payloads) == written


async def test_a_changed_step_whose_stored_document_no_longer_validates_leaves_the_later_flags_written() -> None:
    workflow = _maintenance()
    store, library, watch = await _world(workflow)
    store.pages["st.a.v2"]["frontmatter"]["step"] = {"name": "a"}

    await watch.run_ended(_ended(workflow))

    assert await library.fetch_flag("mf.rejected.a") is None
    assert await library.fetch_flag("mf.rejected.b") is not None


async def test_a_changed_step_the_library_cannot_read_leaves_the_other_flags_written() -> None:
    workflow = _maintenance()
    _, library, watch = await _world(workflow)
    run = _ended(workflow).model_copy(update={"params": {"changed_step_ids": "st.gone.v1, st.b.v5"}})

    await watch.run_ended(run)

    assert await library.fetch_flag("mf.rejected.gone") is None
    assert await library.fetch_flag("mf.rejected.b") is not None


async def test_a_rejected_plan_is_flagged_under_the_newest_saved_version_of_each_named_step() -> None:
    workflow = _maintenance()
    _, library, watch = await _world(workflow)
    newer = Briefing(name="a", instructions="Summarise the whole source in one short paragraph that names its author.", inputs={"text": TEXT}, output=TEXT)
    await library.put_step(newer, version=3, title="Step", summary="s", applicability="a", provenance={"saved_by": "owner:cli"})

    await watch.run_ended(_ended(workflow))

    flag = await library.fetch_flag("mf.rejected.a")
    assert flag is not None
    assert (flag.step_id, flag.step_hash) == ("st.a.v3", step_content_address(newer))
    flags = await library.flags(kind="plan_rejected")
    assert sorted(card.id for card in flags) == ["mf.rejected.a", "mf.rejected.b"]
