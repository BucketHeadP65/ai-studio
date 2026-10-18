"""The request bodies the running routes read."""

from __future__ import annotations

from typing import Annotated

from core.ai.artifacts import ArtifactRef
from core.ai.workflows.contracts import WorkflowArguments
from pydantic import BaseModel, ConfigDict, Field, StringConstraints, field_validator

from harness.domain.contracts import GateMode

RUN_TITLE_MAX = 200
"""The most characters a run's own name may hold."""


class RunRequest(BaseModel):
    """Which saved workflow to run, and the values for its declared parameters."""

    workflow_id: str
    arguments: WorkflowArguments = Field(default_factory=dict)
    """One JSON value per declared parameter, each as the type that parameter declares. A client
    that can send nothing but text still works: text under a parameter that cannot hold it is read
    as the JSON that parameter declares, once, before anything is checked."""
    run_title: Annotated[str, StringConstraints(max_length=RUN_TITLE_MAX)] = ""
    """What to call this run in front of a person, at most `RUN_TITLE_MAX` characters. Empty leaves
    the run unnamed, and no name is borrowed from elsewhere."""
    gate_mode: GateMode = "wait"
    """`"wait"` holds a person gate for a person, `"auto"` passes every gate with nobody asked,
    `"orchestrate"` hands a person gate to this run's own orchestrator instead of waiting for a
    person. A declared orchestrator gate decides the same way under every one of the three."""
    auto_approve_gates: bool = False
    """Kept for old callers. `True` means the same as `gate_mode="auto"`, and a request where the
    two disagree is refused."""


class SteerStepIn(BaseModel):
    """A message for one running step's conversation, as the browser posts it to the steer route."""

    message: str
    """The words for the step. An empty or whitespace-only message is refused."""

    @field_validator("message")
    @classmethod
    def _not_blank(cls, value: str) -> str:
        """Refuse an empty or whitespace-only steer."""
        if not value.strip():
            msg = "a steer needs a message"
            raise ValueError(msg)
        return value


class ApproveRequest(BaseModel):
    """Who approves the workflow, and why, in their own words.

    `by` must name who is approving. `note` is logged beside the approval and never reaches the
    evidence.
    """

    by: str = ""
    """Who approves. A blank or whitespace-only name is refused."""
    note: str = ""


class PinRequest(BaseModel):
    """Whether one archived run should survive the retention sweep."""

    pinned: bool


class ResolveArtifactRequest(BaseModel):
    """The reference to resolve, and nothing else. A URL is never a way to ask this."""

    model_config = ConfigDict(extra="forbid")

    ref: ArtifactRef


class Decision(BaseModel):
    """A person's answer at a gate: approve, or reject and rerun from the gate's target.

    `feedback` is read only on a rejection. An approval releases the value whatever `feedback` says.
    """

    approve: bool
    feedback: str = ""


class AutomodeRequest(BaseModel):
    """Whether the live run's gates should pass themselves from here on, with nobody asked."""

    on: bool


class ResumeRequest(BaseModel):
    """The graph to resume the paused run with.

    The body is optional. An empty `spec_yaml` falls back to the draft this session settled on, and
    with no draft either, to the workflow the waiting run started from. Send YAML only for a graph
    edited somewhere else.
    """

    spec_yaml: str = ""
