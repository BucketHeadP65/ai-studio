"""What the agent proposes, and what the browser sends.

`Draft` lives here rather than beside the routes because the agent's output contract is the
slice's domain: change it and every other file in the slice changes with it.
"""

from __future__ import annotations

from core.ai.workflows.contracts import Lineage, Outcome, WorkflowIntent, WorkflowPresentation
from pydantic import BaseModel, ConfigDict, Field, model_validator


class Draft(BaseModel):
    """The agent's current proposal, as spec YAML plus the prose a library needs."""

    spec_yaml: str = Field(default="", description="The workflow as YAML. Empty until you draft one.")
    title: str = ""
    summary: str = ""
    applicability: str = ""
    lineage: Lineage | None = None
    """The typed diff this proposal came from, if it is somebody's child.

    On the draft rather than the conversation: being an adaptation is a fact about the proposal,
    not a token cleared after a save, and the draft is the half that survives a restart."""
    presentation: WorkflowPresentation | None = None
    """The author's own words about this proposal, once anything has written any.

    `None` is a proposal nobody has described yet, which is not the same as one described with
    empty strings: nothing here supplies words on an author's behalf."""
    intent: WorkflowIntent | None = None
    """What the person asked for, and what they expect a run to do, once anything has recorded it.

    On the draft rather than the conversation, for the reason a lineage is: what was asked for is
    a fact about the proposal and survives a restart with it. An incomplete intent is a normal
    draft, and `None` is a proposal nobody has stated one for."""


class SourceRun(BaseModel):
    """One ended run offered as the reason a new version of its workflow exists.

    Read-only: an adaptation cites a run, and citing one never writes to its record. `outcome` is
    what that run was observed to do.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    run_id: str
    workflow_id: str
    run_title: str = ""
    outcome: Outcome


class ToolResultIn(BaseModel):
    """The browser's answer to one call the previous turn left for it: a verb's outcome, or an approval."""

    model_config = ConfigDict(extra="forbid")

    tool_call_id: str
    tool_name: str
    output: str | None = None
    """What a client-executed verb did, in the words the model reads back."""
    approved: bool | None = None
    """The person's answer to an approval request."""
    reason: str | None = None
    """Why an approval was denied, when it was."""

    @model_validator(mode="after")
    def _one_answer(self) -> ToolResultIn:
        """A result answers as an outcome or as an approval, never neither and never both."""
        if (self.output is None) == (self.approved is None):
            msg = "a tool result carries exactly one of output or approved"
            raise ValueError(msg)
        return self
