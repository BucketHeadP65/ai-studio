"""What the session projection's reads return: one row type per projected table, and one registered project."""

from __future__ import annotations

import uuid

from datetime import datetime
from decimal import Decimal

from pydantic import BaseModel, ConfigDict
from pydantic.dataclasses import dataclass as validated

from harness.domain.contracts import FlagSource, SessionType


@validated(frozen=True)
class SessionRow:
    """One `sessions` row, as the projection stores it."""

    id: uuid.UUID
    project_id: uuid.UUID
    title: str
    started_at: datetime
    last_activity_at: datetime
    archived: bool
    turn_count: int
    input_tokens: int
    output_tokens: int
    cache_read_tokens: int
    cache_write_tokens: int
    cost: Decimal
    unpriced: int
    session_kind: SessionType


@validated(frozen=True)
class TurnRow:
    """One `turns` row: when it ran, what it cost, and how it ended."""

    seq: int
    run_id: str | None
    entry: str
    started_at: datetime
    settled_at: datetime | None
    outcome: str
    input_tokens: int
    output_tokens: int
    cache_read_tokens: int
    cache_write_tokens: int


@validated(frozen=True)
class MessageRow:
    """One `messages` row: one message of one run, in the order it was said."""

    run_id: str
    idx: int
    kind: str
    payload: str
    text_content: str


@validated(frozen=True)
class FlagRow:
    """One `flags` row: what was flagged, why, and where the flag came from."""

    id: uuid.UUID
    session_id: uuid.UUID
    target_kind: str
    target: str
    note: str
    tool_call_id: str | None
    source: FlagSource
    created_at: datetime


@validated(frozen=True)
class ProjectRow:
    """One `projects` row: enough for a picker."""

    id: uuid.UUID
    name: str
    root_path: str


class RegisteredProject(BaseModel):
    """One project currently on the registry of projects: its id, and the root it points at."""

    model_config = ConfigDict(frozen=True)

    project_id: str
    root_path: str
