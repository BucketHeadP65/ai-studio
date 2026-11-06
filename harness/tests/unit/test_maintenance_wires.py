"""The step maintenance steps' pydantic types wire into briefings and maps by `like`, so a workflow file can name them.

A briefing that takes `{like: prepare_plan}` and answers `{like: plan_decisions}` feeds `land_changes` its decisions
beside the drafted plan, a map whose briefing answers one element of `select_disagreements` feeds
`hand_over_repository_changes`'s drafts, and a map whose briefing answers `{like: repository_verdict}` feeds its verdicts. `report_changes` feeds the hand-over too, which answers for the run, so
no node is a dead end. The registry holds the steps over a library that holds nothing.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING

from core.ai.workflows.contracts import FileEvidenceLog, LibraryCollection, StepRegistry, Workflow, validate

from harness.domain.contracts import CHECK_CHANGE_TOOL
from harness.features.maintenance.contracts import MaintenanceSteps
from tests.support.fakes.case_runner import FakeCaseRunner
from tests.support.fakes.library_collection import FakeCollection
from tests.support.fakes.owner_notifier import FakeOwnerNotifier

if TYPE_CHECKING:
    from pathlib import Path

    from core.ai.workflows.contracts import Briefing

BRIEFING = "Read what the previous step answered and answer it again in the same shape, deciding each item it holds."


async def _perform(task: Briefing, output_type: type, /, **inputs: object) -> object:
    """Nothing here performs a step: the registry only builds and validates them."""
    del task, output_type, inputs
    return None


def _registry(tmp_path: Path) -> StepRegistry:
    """A registry that builds briefings, grants `check_change` and holds the step maintenance steps."""
    store = FakeCollection()
    steps = MaintenanceSteps(
        library=LibraryCollection(store, store.ingest, collection=store.collection),
        history=FileEvidenceLog(tmp_path / "evidence.jsonl"),
        notifier=FakeOwnerNotifier(),
        cases=FakeCaseRunner(),
        clock=lambda: datetime(2026, 11, 6, tzinfo=UTC),
        environment={},
        current_run=lambda: None,
    )
    registry = StepRegistry(perform=_perform, grants=(CHECK_CHANGE_TOOL,))
    steps.register(registry)
    return registry


WORKFLOW = {
    "name": "step-maintenance-wires",
    "version": 1,
    "description": "Check, plan, land, report and hand over a reusable step's new version, as the maintenance workflow wires it.",
    "parameters": {"changed_step_ids": {"description": "the changed step versions' ids", "json_schema": {"type": "string"}}},
    "inline": [
        {
            "name": "plan_changes",
            "instructions": BRIEFING,
            "inputs": {"draft": {"like": "prepare_plan"}},
            "output": {"like": "plan_decisions"},
            "tools": [CHECK_CHANGE_TOOL],
        },
        {
            "name": "draft_repository_change",
            "instructions": BRIEFING,
            "inputs": {"change": {"like": "select_disagreements", "element": True}},
            "output": {"like": "select_disagreements", "element": True},
            "tools": [CHECK_CHANGE_TOOL],
        },
        {
            "name": "judge_repository_change",
            "instructions": BRIEFING,
            "inputs": {"draft": {"like": "select_disagreements", "element": True}},
            "output": {"like": "repository_verdict"},
        },
    ],
    "nodes": [
        {"kind": "run", "id": "list_consumers", "work": "list_consumers", "inputs": {"changed_step_ids": {"parameter": "changed_step_ids"}}},
        {"kind": "run", "id": "check_contracts", "work": "check_contracts", "inputs": {"consumers": {"node": "list_consumers"}}},
        {"kind": "run", "id": "run_step_cases", "work": "run_step_cases", "inputs": {"changed_step_ids": {"parameter": "changed_step_ids"}}},
        {
            "kind": "run",
            "id": "prepare_plan",
            "work": "prepare_plan",
            "inputs": {"consumers": {"node": "list_consumers"}, "checks": {"node": "check_contracts"}, "cases": {"node": "run_step_cases"}},
        },
        {"kind": "run", "id": "plan_changes", "work": "plan_changes", "inputs": {"draft": {"node": "prepare_plan"}}},
        {"kind": "run", "id": "land_changes", "work": "land_changes", "inputs": {"plan": {"node": "prepare_plan"}, "decisions": {"node": "plan_changes"}}},
        {"kind": "run", "id": "report_changes", "work": "report_changes", "inputs": {"landed": {"node": "land_changes"}}},
        {
            "kind": "run",
            "id": "select_disagreements",
            "work": "select_disagreements",
            "inputs": {"landed": {"node": "land_changes"}, "consumers": {"node": "list_consumers"}},
        },
        {
            "kind": "map",
            "id": "draft_repository_changes",
            "turns": ["draft_repository_change"],
            "over": {"node": "select_disagreements"},
            "each": "change",
            "on_error": "skip",
        },
        {
            "kind": "map",
            "id": "judge_repository_changes",
            "turns": ["judge_repository_change"],
            "over": {"node": "draft_repository_changes"},
            "each": "draft",
            "on_error": "skip",
        },
        {
            "kind": "run",
            "id": "hand_over_repository_changes",
            "work": "hand_over_repository_changes",
            "inputs": {
                "report": {"node": "report_changes"},
                "disagreements": {"node": "select_disagreements"},
                "drafts": {"node": "draft_repository_changes"},
                "verdicts": {"node": "judge_repository_changes"},
            },
        },
    ],
    "output": {"node": "hand_over_repository_changes"},
}


def test_the_maintenance_steps_wire_into_briefings_and_maps_typed_by_like(tmp_path: Path) -> None:
    report = validate(Workflow.model_validate(WORKFLOW), _registry(tmp_path))

    assert report.ok, report.issues
