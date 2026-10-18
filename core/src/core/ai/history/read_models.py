"""What the record's reads hand to code outside it: a run, an event at its position, and a step read back whole."""

import json

from dataclasses import dataclass
from datetime import datetime
from typing import Annotated
from uuid import UUID

from pydantic import BeforeValidator
from pydantic_ai.messages import ModelMessage
from pydantic_ai_harness.step_persistence import EventKind, SnapshotState


def _decoded(value: object) -> object:
    """A jsonb column as Python: the driver hands it back as text, a caller may hand it back decoded."""
    return json.loads(value) if isinstance(value, str) else value


type Jsonb[T] = Annotated[T, BeforeValidator(_decoded)]
"""A jsonb column, decoded before the row type validates it."""


@dataclass(frozen=True)
class RunRow:
    """One `runs` row."""

    run_id: str
    conversation_id: UUID
    parent_run_id: str | None
    agent_name: str | None
    started_at: datetime
    metadata: Jsonb[dict[str, str]]
    registration_id: str | None
    """The durable registration that claimed the run, `None` for a run registered before registrations were kept."""


@dataclass(frozen=True)
class EventAtPositionRow:
    """One `run_events` row at its global position, with the run it belongs to."""

    id: int
    """The event's global position in `run_events`, which consumers page by."""
    run_id: str
    kind: EventKind
    step_index: int
    at: datetime
    agent_name: str | None
    conversation_id: UUID


@dataclass(frozen=True)
class RecordedStep:
    """One step read back whole: its messages as the model was given them, each under the ordinal the conversation knows it by."""

    seq: int
    """The store's own number for the step, unique and increasing within the run."""
    step_index: int
    """The step number the snapshot carries."""
    at: datetime
    """When the snapshot was taken."""
    state: SnapshotState
    ordinals: tuple[int, ...]
    messages: list[ModelMessage]
