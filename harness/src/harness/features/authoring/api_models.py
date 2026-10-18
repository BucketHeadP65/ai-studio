"""The request bodies the authoring routes read."""

from typing import Any

from core.ai.workflows.contracts import Prediction
from pydantic import BaseModel, Field
from pydantic_ai.ui.vercel_ai.request_types import SubmitMessage

from harness.domain.contracts import PageContext, SessionType
from harness.features.authoring.domain import ToolResultIn


class SessionIn(BaseModel):
    """Which project a new session opens against, and what it is for.

    `project_id` absent opens on the harness checkout's own home. `kind` names the authoring
    conversation (the default), one opened to drive a workflow unattended, or AI Factory Studio's
    follow-me companion.
    """

    project_id: str | None = None
    kind: SessionType = "chat"


class AdaptRequest(BaseModel):
    """One saved workflow, the typed diff to apply to it, and what the person making it claims."""

    workflow_id: str
    ops: list[dict[str, Any]]
    """The typed edits to apply, as data. The whole set is checked before any edit is applied."""
    evidence: str = ""
    """Why this version exists. Prose, and nothing grades it."""
    predictions: list[Prediction] = Field(default_factory=list)
    """What a later run of the child should show. Graded against the run evidence, never asserted here."""


class RunAdaptRequest(AdaptRequest):
    """The same diff, plus the ended run the person is adapting because of.

    `source_run_id` empty adapts with nothing cited, exactly as the unscoped route does. Naming a
    run this deployment did not archive is refused, never adapted without the citation.
    """

    source_run_id: str = ""
    feedback: str = ""
    """The owner's own reason, in their words. Recorded beside the run, graded by nothing."""


class SteerIn(BaseModel):
    """A message for the turn already in flight, as the browser posts it to the steer route."""

    message: str


class ChatRequest(SubmitMessage):
    """One turn as AI Factory Studio sends it: the AI SDK's own body, plus what this app's surfaces add.

    `messages` carries the new message only. What the model saw before it comes from the record,
    never from the browser.
    """

    context: PageContext | None = None
    """The page the person is looking at, folded into the prompt."""
    tool_results: list[ToolResultIn] = Field(default_factory=list)
    """The browser's answers to the calls the previous turn stopped for."""
    design_only: bool = False
    """Whether this turn asks for a design and nothing else.

    A surface that only designs says so, and the turn is then offered no verb that starts a run or
    changes a standing schedule. It narrows what this turn may reach and can widen nothing: a
    request that says nothing gets the ordinary surface."""
