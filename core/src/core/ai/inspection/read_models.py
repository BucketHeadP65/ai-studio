"""What the inspection's tables read back as: one frozen row type per table, and the closed sets their columns hold."""

from dataclasses import dataclass
from datetime import datetime
from typing import Literal
from uuid import UUID

from core.ai.history.contracts import Jsonb
from core.ai.transcripts.contracts import Span
from core.ai.workflows.contracts import Tier

type QuestionType = Literal["attribute", "scan"]
"""What the inspector was asked: to attribute one lead, or to scan one whole run."""

type LeadResolution = Literal["open", "undecided", "invalid", "dismissed"]
"""Where a lead stands: still open, or settled without a finding and how."""

type AttemptResolution = Literal["undecided", "invalid", "dismissed"]
"""How an attempt that gave no finding settles a lead."""


@dataclass(frozen=True)
class StoredBlame:
    """One blame as stored: root_cause, surface_kind, surface_name, why."""

    root_cause: str
    surface_kind: str
    surface_name: str
    why: str


@dataclass(frozen=True)
class StoredLead:
    """One `leads` row. `attempted_at` and `refusal` are set when the inspector was asked and gave no finding."""

    lead_id: int
    source: str
    watch: str
    symptom: str
    spans: Jsonb[list[Span]]
    spans_digest: str
    found_by: str
    seen_at: datetime
    attempted_at: datetime | None = None
    refusal: str | None = None
    resolution: LeadResolution = "open"


@dataclass(frozen=True)
class StoredFinding:
    """One `findings` row."""

    finding_id: int
    lead_id: int | None
    source: str
    symptom: str
    spans: Jsonb[list[Span]]
    blames: Jsonb[list[StoredBlame]]
    found_by: str
    model: str | None
    decided_at: datetime
    question_id: int | None = None
    explanation: str = ""


@dataclass(frozen=True)
class StoredQuestion:
    """One `questions` row: one thing the inspector was asked, and the one line the handler ended it with."""

    question_id: int
    conversation_id: UUID
    kind: QuestionType
    source: str
    watch: str
    lead_id: int | None
    tier: Tier
    model: str
    asked_at: datetime
    answer: str | None = None
