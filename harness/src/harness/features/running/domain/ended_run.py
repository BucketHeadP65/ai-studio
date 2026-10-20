"""An ended run: the whole record the archive keeps, and the facts its checked outcome is read from.

Nothing here reads or writes a file. The archive's adapter stores the record, and the service builds it.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Literal

from core.ai.artifacts import ArtifactRef
from core.ai.workflows.contracts import (
    FromNode,
    FromParameter,
    GateReview,
    JudgeGate,
    OrchestratorGate,
    Outcome,
    PersonGate,
    ReviewedArtifactRef,
    WorkflowArguments,
    canonical_reviewed_value,
    observed_outcome,
    reviewed_value_digest,
)
from pydantic import BaseModel, ConfigDict, Field, JsonValue
from pydantic_core import to_jsonable_python

from harness.features.running.domain.progress import StepEvent

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence

    from core.ai.workflows.contracts import GateDecisionFact, MapBatch, ResultArtifactSpec, RetryFeedback, Wire, Workflow, WorkItemFact


class ReviewedValue(BaseModel):
    """One value a gate reviewed, under the address it was captured at, as canonical JSON text.

    `canonical` is the exact text the digest addresses. Never re-serialise it.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    ref: ReviewedArtifactRef
    canonical: str


class RunArchiveRecord(BaseModel):
    """One ended run, whole: what ran, what it produced, and how it ended."""

    run_id: str
    workflow_id: str
    run_title: str
    """What whoever started this run called it. Empty for a run nobody named. Never borrowed from the
    session the run ran in."""
    outcome: Literal["completed", "failed", "stopped"]
    """How the execution ended. What the run actually did is `result`, and the two are separate."""
    result: Outcome
    """The run's checked outcome, classified from its own facts when it ended."""
    failure: str = ""
    started_at: str
    ended_at: str
    runner: str
    session_id: str
    params: WorkflowArguments
    """What the run was given, as the JSON each parameter declares."""
    spec: dict[str, object]
    """The snapshot of the workflow that actually ran, as `Workflow.model_dump(mode="json")`."""
    values: dict[str, object]
    """Jsonable per-node outputs, the run log's own state dict at the moment it closed."""
    conversations: dict[str, list[dict[str, object]]] = Field(default_factory=dict)
    """Each step conversation's messages at the moment the run ended, keyed `"node[item]"` and
    jsonable via `ModelMessagesTypeAdapter`."""
    events: list[StepEvent]
    gates_approved: tuple[str, ...] = ()
    gates_rejected: dict[str, int] = Field(default_factory=dict)
    gates_auto: tuple[str, ...] = ()
    gates_orchestrated: tuple[str, ...] = ()
    """Gate ids this run's own orchestrator decided rather than a person: declared orchestrator gates
    and person gates `gate_mode="orchestrate"` converted."""
    reviewed: tuple[ReviewedValue, ...] = ()
    """Every value this run's gates reviewed, copied here as the run ended so a reference to one
    outlives the run log. The copies are swept together with the record."""
    pinned: bool = False


def jsonable_values(values: dict[str, object]) -> dict[str, object]:
    """Each value as plain JSON, skipping any value that cannot convert, as the run log does.

    Args:
        values: The per-node outputs.

    Returns:
        The values that convert, under their node ids.
    """
    jsonable: dict[str, object] = {}
    for node_id, value in values.items():
        try:
            jsonable[node_id] = to_jsonable_python(value)
        except ValueError:
            continue
    return jsonable


@dataclass(frozen=True)
class EndedRun:
    """Everything an ending reads to say what its run actually did, gathered as one thing.

    Assembled from live run state at an ending that held it, and from the run log at an ending that
    did not. Either way it is facts the run could not have written about itself.
    """

    spec: Workflow
    run_id: str
    status: Literal["completed", "failed", "stopped"]
    values: dict[str, object]
    params: Mapping[str, JsonValue]
    work_facts: Sequence[WorkItemFact]
    batches: Mapping[str, MapBatch]
    gate_decisions: Sequence[GateDecisionFact]
    gate_generations: Mapping[str, int]
    exhausted: RetryFeedback | None = None
    """The rejection that spent a gate's last reach, where one ended the run. Read off the error
    the run raised, never off the run log: a failing run's last decision never reaches the file."""


def checked_outcome(ended: EndedRun) -> Outcome:
    """What `ended` actually did, with a reference minted for every artifact its values fix.

    Each reference's digest is taken over the same jsonable values the archive record keeps, so it
    resolves against the record later.

    Args:
        ended: The facts of the run that ended.

    Returns:
        The checked outcome.
    """
    values = jsonable_values(ended.values)
    result = ended.spec.result
    return observed_outcome(
        result,
        status=ended.status,
        facts=ended.work_facts,
        primary=ended.batches.get(result.primary_work) if result is not None else None,
        decisions=ended.gate_decisions,
        reviews=_gate_reviews(ended, values),
        artifacts=_artifact_refs(ended, values),
        exhausted=ended.exhausted,
    )


def _artifact_refs(ended: EndedRun, values: dict[str, object]) -> tuple[ArtifactRef, ...]:
    """One reference per declared artifact whose value this run fixed, leaving out any whose value is absent."""
    result = ended.spec.result
    if result is None:
        return ()
    minted: list[ArtifactRef] = []
    for artifact in result.artifacts:
        selected = _selected(artifact, values, ended.params)
        digest = None if selected is None else _digest_of(selected)
        if digest is None:
            continue
        node_id, turn = producing_address(artifact.source)
        minted.append(
            ArtifactRef(
                id=artifact.id,
                label=artifact.label,
                run_id=ended.run_id,
                node_id=node_id,
                turn=turn,
                value_digest=digest,
                media_type=artifact.media_type,
            )
        )
    return tuple(minted)


def _gate_reviews(ended: EndedRun, values: dict[str, object]) -> dict[str, GateReview]:
    """What each required gate's `reviews` wire resolves to in the run's final values, at the gate's own address."""
    result = ended.spec.result
    if result is None:
        return {}
    required = {check.gate_id for check in result.required_gates}
    reviews: dict[str, GateReview] = {}
    for node in ended.spec.nodes:
        if node.id not in required or not isinstance(node, PersonGate | JudgeGate | OrchestratorGate):
            continue
        node_id, turn = producing_address(node.reviews)
        value = _resolve(node.reviews, values, ended.params)
        digest = None if value is None else _digest_of(value)
        reviewed = None if digest is None else ReviewedArtifactRef(run_id=ended.run_id, node_id=node_id, turn=turn, value_digest=digest)
        reviews[node.id] = GateReview(generation=ended.gate_generations.get(node.id, 0), reviewed=reviewed)
    return reviews


def _selected(artifact: ResultArtifactSpec, values: dict[str, object], params: Mapping[str, JsonValue]) -> object | None:
    """The value one declared artifact names: its whole source, or the one top-level field of it."""
    value = _resolve(artifact.source, values, params)
    if artifact.field is None or value is None:
        return value
    return value.get(artifact.field) if isinstance(value, dict) else None


def _resolve(wire: Wire, values: dict[str, object], params: Mapping[str, JsonValue]) -> object | None:
    """What a wire names among a run's recorded values, or `None` when the run recorded none."""
    if isinstance(wire, FromParameter):
        return params.get(wire.parameter)
    key = wire.node if wire.turn is None else f"{wire.node}.{wire.turn}"
    return values.get(key)


def producing_address(wire: Wire) -> tuple[str, str | None]:
    """Where a wire's value was produced: the node and turn, or the parameter it came in on.

    Mint and resolve every reference into a reviewed value with this one function. A reference
    minted at one address and looked for at another resolves to nothing.

    Args:
        wire: The wire a value arrived on.

    Returns:
        The node id and turn, or `parameter:<name>` and `None` for a parameter.
    """
    if isinstance(wire, FromNode):
        return wire.node, wire.turn
    return f"parameter:{wire.parameter}", None


def _digest_of(value: object) -> str | None:
    """The digest of `value`'s canonical bytes, or `None` for a value with no canonical spelling."""
    try:
        return reviewed_value_digest(canonical_reviewed_value(value))
    except (TypeError, ValueError):
        return None
