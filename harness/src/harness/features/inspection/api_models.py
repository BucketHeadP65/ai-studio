"""The request and response bodies of the inspection routes: findings, their evidence, questions, and one run."""

from __future__ import annotations

from datetime import datetime

from core.ai.inspection.contracts import LeadResolution
from pydantic import BaseModel, ConfigDict, Field, JsonValue


class BlameView(BaseModel):
    """One blame as stored: what is behind the symptom, where a fix lands, and why."""

    root_cause: str = Field(description="The root cause's name.")
    root_cause_text: str = Field(description="What is wrong, in the taxonomy's one sentence.")
    surface_kind: str = Field(description="What kind of thing a fix would edit.")
    surface_name: str = Field(description="Which one, empty when there was nothing to choose.")
    why: str = Field(description="One sentence pointing at the evidence.")


class EvidenceView(BaseModel):
    """One span of a finding, and the part it points at rendered for a person."""

    span: str = Field(description="The position, written step.message.part.")
    label: str = Field(description="What the part is, in words: the step and who wrote it.")
    kind: str = Field(
        description="The part's kind (user-prompt, text, thinking, tool-call, tool-return, retry-feedback, system-prompt), "
        "retried-return for a tool return that asks for its call again, or unreadable when the part could not be read."
    )
    text: str = Field(description="What that part says, rendered for a person, or why it could not be read.")


class LeadView(BaseModel):
    """One lead: the symptom a detector saw, where, and whether the inspector has been asked about it."""

    lead_id: int
    source: str
    watch: str
    symptom: str
    spans: list[str] = Field(description="Each position, written step.message.part.")
    found_by: str = Field(description="The detector's name: a rule key, or the inspector.")
    seen_at: datetime
    attempted_at: datetime | None = Field(description="When the inspector's lane finished trying every tier.")
    refusal: str | None = Field(description="The explanation for a dismissal or unresolved attribution.")
    resolution: LeadResolution = Field(description="Open, insufficient evidence, invalid output, or explicitly dismissed.")


class FindingView(BaseModel):
    """One finding as a listing row."""

    finding_id: int
    lead_id: int | None
    question_id: int | None = Field(description="The question that decided it, absent when a rule did.")
    source: str
    symptom: str
    symptom_text: str = Field(description="The symptom as it appears in a transcript, the taxonomy's sentence.")
    found_by: str = Field(description="A rule, or the inspector.")
    model: str | None = Field(description="The model that decided it, absent when a rule did.")
    decided_at: datetime
    blames: list[BlameView]
    lead_found_by: str | None = Field(description="Which detector produced the lead under it, when there is one.")
    explanation: str = Field(
        description="What went wrong and what to fix, in plain sentences: the inspector's own, or the blames' sentences "
        "with their coordinates removed."
    )


class FindingDetail(BaseModel):
    """One finding, the lead under it, and every span rendered."""

    finding: FindingView
    lead: LeadView | None
    evidence: list[EvidenceView]


class QuestionView(BaseModel):
    """One thing the inspector was asked, and the one line it ended with."""

    question_id: int
    conversation_id: str
    kind: str = Field(description="attribute, or scan.")
    source: str
    watch: str
    lead_id: int | None
    tier: str
    model: str
    asked_at: datetime
    answer: str | None


class ConversationMessageView(BaseModel):
    """One message of a recorded conversation, in the shape a transcript row already crosses the wire in."""

    run_id: str = Field(description="The run that said it, empty when the record kept none.")
    idx: int = Field(description="Position in the conversation, counting from zero.")
    kind: str = Field(description="request or response.")
    payload: JsonValue = Field(description="The raw ModelMessage, JSON-shaped.")
    text: str = Field(description="Every part of it as text, for display and search.")


class TurnView(BaseModel):
    """One step's messages as the chat draws them, and which ones hold a cited part."""

    step: int
    messages: list[ConversationMessageView]
    cited: list[int] = Field(description="Indices into `messages` of the ones holding a cited part.")
    first: int = Field(description="The index in the step's history of the turn's first message.")


class TurnPage(BaseModel):
    """The turn under investigation: one entry per step the finding cites, ascending."""

    turns: list[TurnView]


class JobView(BaseModel):
    """One command the inspection issued about a run, as its row in the jobs table (`coordination.jobs`) reads."""

    id: int
    kind: str
    key: str
    state: str
    attempts: int
    result: str | None
    last_error: str | None
    created_at: datetime
    finished_at: datetime | None


class RunOverview(BaseModel):
    """Everything the inspection did to one run."""

    source: str
    leads: list[LeadView]
    findings: list[FindingView]
    questions: list[QuestionView]
    jobs: list[JobView]


class StepOutlineView(BaseModel):
    """One step of a run in one line."""

    step: int
    messages: int = Field(description="How many messages the run added at this step.")
    summary: str = Field(description="What happened there: what was asked, called and how it ended, said. `nothing new` when it added nothing.")


class RunOutline(BaseModel):
    """The run in one screen, with the facts the record keeps about it."""

    source: str
    agent: str | None = Field(description="The agent that ran it, absent when the record holds no such run.")
    started_at: datetime | None
    ended_at: datetime | None = Field(description="When its last event was recorded.")
    ended: str | None = Field(description="The kind of its last event.")
    model: str | None = Field(description="The model its last recorded request went to.")
    tool_calls: int
    steps: list[StepOutlineView]


class AskIn(BaseModel):
    """The one thing a follow-up carries: what to ask."""

    model_config = ConfigDict(use_attribute_docstrings=True)

    text: str = Field(min_length=1)
    """The question, in words. It holds at least one character."""
