"""What happened to one workflow run, as facts: the run log's grammar and its codec.

A run's own lifecycle belongs to the workflow that ran, not to the conversation that launched it.
Every event here is past tense, appended once, and never edited. Each names the run it belongs to
and the workflow it ran, so one line read on its own says what it is about.

Everything is pure: the codec maps lines to events and back, and the host owns the file.
"""

from __future__ import annotations

import json

from datetime import datetime
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, PositiveInt, TypeAdapter, ValidationError

DecisionSource = Literal["judge", "person", "orchestrator", "auto", "timeout"]
"""Who actually decided one gate reach. `auto` is a run-level bypass, never a reviewer."""

RejectionSource = Literal["judge", "person", "orchestrator", "timeout"]
"""The sources a rejection may name: every reviewer, plus the deadline nobody answered."""

ValueDigest = Annotated[str, Field(pattern="^[0-9a-f]{64}$")]
"""SHA-256 of a reviewed value's canonical JSON, lowercase hex."""


class ReviewedArtifactRef(BaseModel):
    """The address of one immutable value a gate reviewed, within one run."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    run_id: str
    node_id: str
    """Which node produced the value, or `parameter:<name>` for a value wired from a parameter."""
    turn: str | None = None
    """Which turn of a map produced it, when the wire named one."""
    value_digest: ValueDigest


class RetryFeedback(BaseModel):
    """The exact rejected value and the repair one target execution owes because of it."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    gate_id: str
    source: RejectionSource
    target_node: str
    """The node this rejection reruns, which is the gate's own `rejected_to`."""
    target_turn: str | None = None
    """Which turn of that node owes the repair, when the target is a map. `None` for every other node."""
    attempt: PositiveInt
    """Which reach of the gate refused, counting its first review as one."""
    feedback: str
    reviewed: ReviewedArtifactRef
    requested_repair: str
    """What to change. Falls back to `feedback` when the decider said nothing more specific."""


class GateDecisionFact(BaseModel):
    """One decision a gate actually received, over one generation of its reviewed value."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    gate_id: str
    attempt: PositiveInt
    verdict: Literal["approve", "reject", "cannot_judge"]
    source: DecisionSource
    """Who decided. A judge that could not judge and the person it escalated to record one fact each."""
    review_generation: PositiveInt
    """Which reviewed value this decision belongs to: an approval of an earlier generation
    releases nothing for a later one, even when both generations digest identically."""
    reviewed_value_digest: ValueDigest


class _RunEvent(BaseModel):
    """Every run event is frozen and closed: an unknown key is a different event, not baggage."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    run_id: str
    workflow_id: str


class RunLaunched(_RunEvent):
    """The workflow started executing."""

    kind: Literal["run_launched"] = "run_launched"


class RunPaused(_RunEvent):
    """Someone stopped the run without ending it. Its run log stays, so it can resume."""

    kind: Literal["run_paused"] = "run_paused"


class RunResumed(_RunEvent):
    """A paused run started again, replaying the prefix it had already settled."""

    kind: Literal["run_resumed"] = "run_resumed"


class RunStopped(_RunEvent):
    """Someone ended the run for good. Nothing about it is resumable afterwards."""

    kind: Literal["run_stopped"] = "run_stopped"


class RunFinished(_RunEvent):
    """The run reached a terminal state. `status` is the outcome word the evidence carries."""

    kind: Literal["run_finished"] = "run_finished"
    status: str


class GateReached(_RunEvent):
    """A gate got the value in front of a decider."""

    kind: Literal["gate_reached"] = "gate_reached"
    gate_id: str


class GateDecided(_RunEvent):
    """A gate got its answer, from a person or from this run's own orchestrator."""

    kind: Literal["gate_decided"] = "gate_decided"
    gate_id: str
    approved: bool
    by: Literal["human", "orchestrator"] = "human"
    """Who decided. A line without this key reads as `human`."""


RunEvent = Annotated[
    RunLaunched | RunPaused | RunResumed | RunStopped | RunFinished | GateReached | GateDecided,
    Field(discriminator="kind"),
]


class RecordedRunEvent(BaseModel):
    """One run log line: the event plus where and when it sits on the run's timeline."""

    model_config = ConfigDict(frozen=True)

    seq: int
    at: datetime
    event: RunEvent


_RECORDED: TypeAdapter[RecordedRunEvent] = TypeAdapter(RecordedRunEvent)


def encode_run_event(record: RecordedRunEvent) -> str:
    """One flat JSONL line: `seq`, `at`, then the event's own keys, greppable without this codec.

    Args:
        record: The event and its place on the run's timeline.

    Returns:
        The line, without a trailing newline.
    """
    payload = {"seq": record.seq, "at": record.at.isoformat(), **record.event.model_dump(mode="json")}
    return json.dumps(payload)


def decode_run_event(line: str) -> RecordedRunEvent | None:
    """The line's event, or `None` for a torn tail or a kind this reader does not know.

    It never raises, so a damaged final line costs one event and never the whole read.

    Args:
        line: One line of a run log.

    Returns:
        The recorded event, or `None` when the line is not one.
    """
    try:
        payload = json.loads(line)
    except ValueError:
        return None
    if not isinstance(payload, dict) or "seq" not in payload or "at" not in payload:
        return None
    seq, at = payload.pop("seq"), payload.pop("at")
    try:
        return _RECORDED.validate_python({"seq": seq, "at": at, "event": payload})
    except ValidationError:
        return None
