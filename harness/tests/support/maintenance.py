"""The step maintenance steps a composition registers, a way to register them over fakes, and one whole run's world.

The world holds a reusable step `extract_actions` at v1 and v2 and two consumers of v1, and the scripted models answer
the step maintenance workflow's planner, plan judge, drafter and repository judge by step name, so a run over it can be
performed on either engine.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING

import yaml

from core.ai.workflows.contracts import (
    CURRENT_STEP,
    Briefing,
    FromNode,
    FromParameter,
    LibraryCollection,
    ParameterSpec,
    RunNode,
    Shape,
    Workflow,
    load_spec_document,
    load_step_document,
    step_content_address,
    step_page_id,
)
from pydantic import BaseModel

from harness.features.maintenance.contracts import MaintenancePlan, MaintenanceSteps, RepositoryChange
from tests.support.fakes.case_runner import FakeCaseRunner
from tests.support.fakes.library_collection import FakeCollection
from tests.support.fakes.owner_notifier import FakeOwnerNotifier
from tests.support.fakes.run_history import FakeRunHistory

if TYPE_CHECKING:
    from collections.abc import Mapping

    from core.ai.workflows.contracts import StepRegistry

MAINTENANCE_STEPS = (
    "list_consumers",
    "check_contracts",
    "run_step_cases",
    "prepare_plan",
    "land_changes",
    "report_changes",
    "select_disagreements",
    "plan_decisions",
    "repository_verdict",
    "hand_over_repository_changes",
)
"""The registered steps of step maintenance."""

WORKFLOW_FILES = Path(__file__).resolve().parents[2] / "workflows"
"""This app's own workflow and step files."""


def register_maintenance_steps(registry: StepRegistry) -> MaintenanceSteps:
    """Register the step maintenance steps on `registry` over a library that holds nothing, so a workflow naming them validates.

    Args:
        registry: The registry to register the steps on.

    Returns:
        The steps, registered.
    """
    store = FakeCollection()
    steps = MaintenanceSteps(
        library=LibraryCollection(store, store.ingest, collection=store.collection),
        history=FakeRunHistory(),
        notifier=FakeOwnerNotifier(),
        cases=FakeCaseRunner(),
        clock=lambda: datetime(2026, 11, 6, tzinfo=UTC),
        environment={},
        current_run=lambda: None,
    )
    steps.register(registry)
    return steps


def step_maintenance_workflow() -> Workflow:
    """The `step-maintenance` workflow with the steps its `uses` names copied in, exactly as an import copies them."""
    text = (WORKFLOW_FILES / "workflows" / "step-maintenance.md").read_text(encoding="utf-8")
    spec = load_spec_document(text).workflow
    documents = [load_step_document(path.read_text(encoding="utf-8")) for path in sorted((WORKFLOW_FILES / "steps").glob("*.md"))]
    by_id = {step_page_id(document.step.name, document.version): document.step for document in documents}
    front = yaml.safe_load(text.split("---", 2)[1])
    uses = front.get("uses", []) if isinstance(front, dict) else []
    missing = [step_id for step_id in uses if step_id not in by_id]
    assert not missing, f"step-maintenance uses {missing}, which no step file in harness/workflows declares"
    return spec.model_copy(update={"inline": (*spec.inline, *(by_id[step_id] for step_id in uses))})


_TEXT = Shape(json_schema={"type": "string"})
EXTRACT = {
    1: Briefing(
        name="extract_actions",
        instructions="Read the meeting transcript and list every action item agreed, one line each.",
        inputs={"transcript": _TEXT},
        output=_TEXT,
    ),
    2: Briefing(
        name="extract_actions",
        instructions="Read the meeting transcript and list every action item agreed, one line each, naming who owns it. Leave out an item nobody owns.",
        inputs={"transcript": _TEXT},
        output=_TEXT,
    ),
}
"""Two versions of one reusable step. The second leaves out an item nobody owns."""
CHANGED = "st.extract_actions.v2"
CHANGED_HASH = step_content_address(EXTRACT[2])
FEEDBACK = (
    "wf.meeting-actions.v1 st.extract_actions.v2: pass\n"
    "wf.standup-actions.v1 st.extract_actions.v2: fail, the new version leaves out unowned items and the standup list "
    "must keep every item raised. Keep the copy."
)
"""What the scripted plan judge says when it rejects the first plan."""
PROPOSAL = "Give extract_actions an `unowned` parameter: the standup list keeps unowned items, the meeting list leaves them out."
"""What the scripted drafter proposes."""


def _consumer(name: str) -> Workflow:
    return Workflow(
        name=name,
        version=1,
        description=f"List the action items of one meeting for {name}.",
        parameters={"transcript": ParameterSpec(description="the meeting's transcript", json_schema={"type": "string"})},
        inline=(EXTRACT[1],),
        nodes=(RunNode(id="extract", work="extract_actions", inputs={"transcript": FromParameter(parameter="transcript")}),),
        output=FromNode(node="extract"),
    )


class MaintenanceWorld:
    """A library holding `extract_actions` v1 and v2 and two consumers of v1, and the step maintenance steps over it."""

    def __init__(self) -> None:
        self.store = FakeCollection(prune=True)
        self.library = LibraryCollection(self.store, self.store.ingest, collection=self.store.collection)
        self.notifier = FakeOwnerNotifier()
        self.steps = MaintenanceSteps(
            library=self.library,
            history=FakeRunHistory(),
            notifier=self.notifier,
            cases=FakeCaseRunner(),
            clock=lambda: datetime(2026, 11, 6, 9, 0, tzinfo=UTC),
            environment={"harness": "under test"},
            current_run=lambda: "run-12",
        )

    async def fill(self) -> tuple[str, str]:
        """Save `extract_actions` v1, the consumers `meeting-actions` and `standup-actions` of it, then v2.

        Returns:
            The two consumers' ids.
        """
        await self._put_step(1)
        meeting = await self._put(_consumer("meeting-actions"))
        standup = await self._put(_consumer("standup-actions"))
        await self._put_step(2)
        return meeting, standup

    async def fill_current(self) -> str:
        """Save `extract_actions` v1 and v2, then the consumer `meeting-actions` already holding v2.

        Returns:
            The consumer's id.
        """
        await self._put_step(1)
        await self._put_step(2)
        current = _consumer("meeting-actions").model_copy(update={"inline": (EXTRACT[2],)})
        return await self._put(current)

    async def _put_step(self, version: int) -> None:
        await self.library.put_step(
            EXTRACT[version],
            version=version,
            title="Extract actions",
            summary="Action items from a transcript.",
            applicability="Any meeting.",
            provenance={"saved_by": "owner:cli"},
        )

    async def _put(self, workflow: Workflow) -> str:
        return await self.library.put(
            workflow, title=f"{workflow.name} title", summary=f"{workflow.name} summary", applicability="One meeting.", provenance={"saved_by": "owner:cli"}
        )


@dataclass
class ScriptedMaintenanceModels:
    """The planner, the plan judge, the drafter and the repository judge of step maintenance, answered by step name.

    The planner decides to adopt both consumers first, and to keep the standup copy once a repair is owed. The plan judge answers
    `verdicts` in order. The drafter fills a proposal, and the repository judge keeps every draft. `performed` keeps each
    model step answered in order, `repairs` each planner call's owed repairs as (gate, attempt, feedback), and `judged`
    the inputs each plan judge call was handed.
    """

    performed: list[str] = field(default_factory=list)
    repairs: list[list[tuple[str, int, str]]] = field(default_factory=list)
    judged: list[Mapping[str, object]] = field(default_factory=list)
    verdicts: list[dict[str, str]] = field(
        default_factory=lambda: [{"outcome": "reject", "feedback": FEEDBACK}, {"outcome": "approve", "feedback": "every change passes"}]
    )

    def answer(self, step: str, output_type: type, inputs: Mapping[str, object]) -> object | None:
        """The scripted answer of `step` in `output_type`, or None for a step no model performs.

        Args:
            step: The step's name.
            output_type: The type the step answers in.
            inputs: What the step was handed.

        Returns:
            The answer, typed as the step's own output.
        """
        payload = self._payload(step, inputs)
        if payload is None:
            return None
        self.performed.append(step)
        assert issubclass(output_type, BaseModel), output_type
        return output_type.model_validate(payload)

    def _payload(self, step: str, inputs: Mapping[str, object]) -> Mapping[str, object] | None:
        match step:
            case "plan_changes":
                identity = CURRENT_STEP.get()
                owed = identity.retry_feedback if identity is not None else ()
                self.repairs.append([(feedback.gate_id, feedback.attempt, feedback.feedback) for feedback in owed])
                draft = inputs["draft"]
                assert isinstance(draft, MaintenancePlan)
                decisions = [
                    {"workflow_id": change.workflow_id, "step_id": change.step_id}
                    | (
                        {"disposition": "keep", "reason": "The standup list keeps every item, owned or not."}
                        if owed and change.workflow_id.startswith("wf.standup-actions")
                        else {"disposition": "adopt", "reason": "The new version names who owns each item, which the list is for."}
                    )
                    for change in draft.changes
                ]
                return {"decisions": decisions}
            case "judge_plan":
                self.judged.append(inputs)
                return self.verdicts.pop(0)
            case "draft_repository_change":
                change = inputs["change"]
                assert isinstance(change, RepositoryChange)
                return change.model_copy(update={"proposal": PROPOSAL}).model_dump(mode="json")
            case "judge_repository_change":
                draft = inputs["draft"]
                assert isinstance(draft, RepositoryChange)
                return {"step": draft.step, "decision": "keep", "reason": "Both lists keep what they are for."}
            case _:
                return None
