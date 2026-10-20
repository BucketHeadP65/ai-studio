"""The request and response bodies of the session and project routes."""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal

from pydantic import BaseModel, Field, JsonValue

from harness.domain.contracts import FlagSource, SessionType


class UsageTotals(BaseModel):
    """Token counts summed across some scope: a whole session, or one of its turns."""

    input_tokens: int = Field(default=0, description="Prompt tokens across the scope.")
    output_tokens: int = Field(default=0, description="Completion tokens across the scope.")
    cache_read_tokens: int = Field(default=0, description="Tokens served from the provider's prompt cache.")
    cache_write_tokens: int = Field(default=0, description="Tokens written into the provider's prompt cache.")


class ContextVitals(BaseModel):
    """How full the session's context stands right now, never a stored row.

    Computed fresh from the session's own history, anchored on the model's reported usage. While a turn is in flight
    it is the live gauge instead, the fresher fact until the run settles and the history updates.
    """

    used_tokens: int
    window_tokens: int
    fraction: float
    resolved: bool = Field(description="False when the window is the fallback guess, not the model's own.")


class SessionSummary(BaseModel):
    """One session as a listing row: enough to show it, never its transcript."""

    id: str = Field(description="Session id.")
    project_id: str = Field(description="Owning project.")
    title: str = Field(description="Rename if set, else derived excerpt.")
    started_at: datetime = Field(description="First event of the session.")
    last_activity_at: datetime = Field(description="Newest event of the session.")
    archived: bool = Field(description="Hidden from the default listing.")
    answering: bool = Field(description="Whether a turn is in flight in this session right now.")
    turn_count: int = Field(description="Turns recorded.")
    usage: UsageTotals = Field(description="Summed usage.")
    kind: SessionType = Field(
        description="The authoring conversation, one driving a workflow unattended, or AI Factory Studio's follow-me companion."
    )
    cost_usd: Decimal = Field(description="Priced total across the session's turns.")
    unpriced: int = Field(default=0, description="Responses no source could price. Above zero, cost_usd is a floor.")
    context: ContextVitals | None = Field(
        description="The session's own history anchored on the model's reported usage, or the live in-flight gauge while a "
        "turn is running. None when this process does not hold the session."
    )


class TurnView(BaseModel):
    """One turn's own record: when it ran, what it cost, and how it ended."""

    seq: int = Field(description="Session-log seq of the turn_started event.")
    run_id: str | None = Field(description="Step-log run the turn wrote, when it settled.")
    entry: str = Field(description="spa or acp.")
    started_at: datetime = Field(description="When the prompt entered.")
    settled_at: datetime | None = Field(description="When the reply landed. None for failed or open turns.")
    outcome: str = Field(description="settled, failed, abandoned, or open.")
    usage: UsageTotals = Field(description="This turn's usage.")


class TranscriptMessage(BaseModel):
    """One message from one run, in the order it was said."""

    run_id: str = Field(description="Run that produced the message.")
    idx: int = Field(description="Position within the run's new messages.")
    kind: str = Field(description="request or response.")
    payload: JsonValue = Field(description="The raw ModelMessage, JSON-shaped.")
    text: str = Field(description="Extracted text for display and search.")


class FlagView(BaseModel):
    """One filed grievance: what it names, why, and where it came from."""

    id: str = Field(description="Flag id.")
    session_id: str = Field(description="Subject session.")
    target_kind: str = Field(description="tool, skill, capability, workflow, agent, instructions, or self.")
    target: str = Field(description="The named thing.")
    note: str = Field(description="What ground.")
    tool_call_id: str | None = Field(description="Offending call, when the flag names one.")
    source: FlagSource = Field(description="live: filed by the agent during a turn.")
    created_at: datetime = Field(description="When it was filed.")


class ProjectView(BaseModel):
    """One project row: enough for a picker, plus its current health and load."""

    id: str = Field(description="Project id.")
    name: str = Field(description="Display name.")
    root_path: str = Field(description="Absolute folder path.")
    missing: bool = Field(description="Whether the root path no longer exists on disk.")
    running_sessions: int = Field(description="Sessions of this project with a turn in flight right now.")


class SessionPatch(BaseModel):
    """Rename, archive, or both. An absent field leaves that half of the session alone."""

    title: str | None = None
    """The session's new title. Absent leaves the title as it is."""
    archived: bool | None = None
    """Whether the session is hidden from the default listing. Absent leaves it as it is."""


class ProjectRegisterRequest(BaseModel):
    """The one thing opening a project needs: where it lives."""

    root_path: str
    """The project's folder on this machine. A leading `~` is expanded, and the path must name an existing directory."""


class ProjectRename(BaseModel):
    """The one thing renaming a project needs: its new display name."""

    name: str
    """The project's new display name, written into its own config file."""
