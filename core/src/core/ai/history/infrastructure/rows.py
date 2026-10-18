"""What each statement's rows carry, one frozen type per shape, with the types the schema declares.

A connection hands back rows keyed by column name. `row_as` and `rows_as` turn them into these types once, right
after the fetch, and everything downstream reads columns by name off an object the type checker knows.
"""

from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime
from functools import cache
from typing import Literal
from uuid import UUID

from pydantic import JsonValue, TypeAdapter
from pydantic_ai_harness.step_persistence import EventKind, SnapshotState, ToolEffectStatus

from core.ai.history.conversation_record import MessageJson
from core.ai.history.internal_contracts import Row
from core.ai.history.read_models import Jsonb


@dataclass(frozen=True)
class SeqRow:
    """The number the next row of a run's table takes."""

    seq: int


@dataclass(frozen=True)
class EventRow:
    """One `run_events` row."""

    run_id: str
    seq: int
    kind: EventKind
    step_index: int
    at: datetime
    tool_call_id: str | None
    tool_name: str | None
    error: str | None
    metadata: Jsonb[dict[str, str]]


@dataclass(frozen=True)
class PositionRow:
    """The one position a statement answered with."""

    position: int


@dataclass(frozen=True)
class StepRow:
    """One `steps` row, without the run the caller already knows."""

    seq: int
    step_index: int
    at: datetime
    state: SnapshotState
    entries: list[int]
    """Every entry of the step as three numbers laid end to end: the message's ordinal, its form and its stamp version."""


@dataclass(frozen=True)
class StepEntriesRow:
    """One step's run, number and entries: what a reconstruction needs of it, and what its errors are reported against."""

    run_id: str
    seq: int
    entries: list[int]
    """Every entry of the step as three numbers laid end to end: the message's ordinal, its form and its stamp version."""


@dataclass(frozen=True)
class ToolEffectRow:
    """One `tool_effects` row."""

    run_id: str
    tool_call_id: str
    tool_name: str
    status: ToolEffectStatus
    started_at: datetime
    ended_at: datetime | None
    idempotency_key: str | None
    effect_summary: str | None


@dataclass(frozen=True)
class MessageDigestsRow:
    """What one message of a conversation is known by: its key, then the content address of every form and every stamp version it has."""

    key: str
    form_digests: list[str]
    stamp_digests: list[str]


@dataclass(frozen=True)
class MessagePiecesRow:
    """One message as it is stored: its form and the stamps it was sent with, instructions still a digest."""

    content: Jsonb[MessageJson]
    stamps: Jsonb[MessageJson]


@dataclass(frozen=True)
class StepMessagePieceRow:
    """One message a step's entries name, with the entry it answers so a caller can tell which entries went unanswered."""

    ordinal: int
    form: int
    version: int
    content: Jsonb[MessageJson]
    stamps: Jsonb[MessageJson]


@dataclass(frozen=True)
class StepParametersRow:
    """One step's request parameters: the step it belongs to and the document the recorder wrote for it."""

    step_index: int
    parameters: Jsonb[dict[str, JsonValue]]


@dataclass(frozen=True)
class InstructionRow:
    """One instructions text under its content address."""

    digest: str
    text: str


type ContinuationStatus = Literal["started", "completed"]
"""Whether one continuation attempt is still running or has settled. The two the check constraint allows."""


@dataclass(frozen=True)
class ContinuationAttemptRow:
    """One `continuation_attempts` row.

    Everything past `run_id` is filled only once the attempt is `completed`, which the table's own
    check constraint enforces. `output` reads back as the JSON that was stored, so a stored JSON
    null and a started attempt's empty column both arrive as `None`: read it under `status`.
    """

    conversation_id: UUID
    attempt_id: str
    expected_revision: int
    status: ContinuationStatus
    run_id: str
    snapshot_seq: int | None
    checkpoint_digest: str | None
    output: Jsonb[JsonValue]
    output_digest: str | None
    started_at: datetime
    completed_at: datetime | None


@dataclass(frozen=True)
class ContinuationHeadRow:
    """One `continuation_heads` row: how far a conversation has been carried, and by which attempt."""

    conversation_id: UUID
    revision: int
    attempt_id: str


@dataclass(frozen=True)
class RevisionRow:
    """The revision a head statement answered with, which is how a caller tells one affected row from none."""

    revision: int


@dataclass(frozen=True)
class AttemptIdRow:
    """The attempt a statement answered with, which is how a caller tells one affected row from none."""

    attempt_id: str


@cache
def _adapter[T](row_type: type[T]) -> TypeAdapter[T]:
    """The validator for one row type, built once."""
    return TypeAdapter(row_type)


def row_as[T](row_type: type[T], row: Row) -> T:
    """One row as the type that names its columns.

    Args:
        row_type: The frozen type whose fields are the row's columns.
        row: The row a connection handed back.

    Returns:
        The row, validated.

    Raises:
        ValidationError: when a column holds something the type does not declare.
    """
    return _adapter(row_type).validate_python(dict(row.items()))


def rows_as[T](row_type: type[T], rows: Iterable[Row]) -> list[T]:
    """Every row as the type that names its columns.

    Args:
        row_type: The frozen type whose fields are the rows' columns.
        rows: The rows a connection handed back.

    Returns:
        The rows, validated, in the same order.

    Raises:
        ValidationError: when a column holds something the type does not declare.
    """
    adapter = _adapter(row_type)
    return [adapter.validate_python(dict(row.items())) for row in rows]
