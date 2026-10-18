"""Every read the store runs: the runs, their events and tool effects, and the messages behind a step.

Each function runs on the connection its caller holds, so one caller's reads all run on one connection.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Final
from uuid import UUID

from pydantic_ai_harness.step_persistence import ContinuableSnapshot, RunRecord, StepEvent, ToolEffectRecord

from core.ai.history.conversation_record import ConversationRecord, MessageJson, instructions_digests, message_from, messages_of
from core.ai.history.infrastructure.queries import (
    SELECT_COMPLETE_STEPS,
    SELECT_CONTINUATION_ATTEMPT,
    SELECT_CONTINUATION_HEAD,
    SELECT_CONVERSATION_DIGESTS,
    SELECT_EVENTS,
    SELECT_EVENTS_AFTER,
    SELECT_FIRST_MESSAGES,
    SELECT_INSTRUCTIONS,
    SELECT_LAST_EVENT_POSITION,
    SELECT_LATEST_COMPLETE_STEP,
    SELECT_LATEST_STEP,
    SELECT_LATEST_STEP_OF_CONVERSATION,
    SELECT_RUN,
    SELECT_RUNS,
    SELECT_RUNS_ENDED_SINCE,
    SELECT_STARTED_CONTINUATION_ATTEMPTS,
    SELECT_STARTED_TOOL_EFFECTS,
    SELECT_STEP_AT_SEQ,
    SELECT_STEP_MESSAGES,
    SELECT_STEP_PARAMETERS,
    SELECT_STEPS,
    SELECT_TOOL_EFFECT,
)
from core.ai.history.infrastructure.rows import (
    ContinuationAttemptRow,
    ContinuationHeadRow,
    EventRow,
    InstructionRow,
    MessageDigestsRow,
    MessagePiecesRow,
    PositionRow,
    StepEntriesRow,
    StepMessagePieceRow,
    StepParametersRow,
    StepRow,
    ToolEffectRow,
    row_as,
    rows_as,
)
from core.ai.history.read_models import EventAtPositionRow, RecordedStep, RunRow

if TYPE_CHECKING:
    from collections.abc import Sequence
    from datetime import datetime

    from pydantic import JsonValue
    from pydantic_ai.messages import ModelMessage
    from pydantic_ai_harness.step_persistence import EventKind

    from core.ai.history.internal_contracts import Connection


async def run(connection: Connection, run_id: str) -> RunRow | None:
    """Read one run's row.

    Args:
        connection: The connection the caller holds.
        run_id: The run.

    Returns:
        The row, or None when no run has this id.
    """
    row = await connection.fetchrow(SELECT_RUN, run_id)
    return row_as(RunRow, row) if row is not None else None


async def run_record(connection: Connection, run_id: str) -> RunRecord | None:
    """Read one run as the `StepStore` port hands it back.

    Args:
        connection: The connection the caller holds.
        run_id: The run.

    Returns:
        The run, or None when no run has this id.
    """
    found = await run(connection, run_id)
    return _run_record(found) if found is not None else None


async def run_records(connection: Connection, *, parent_run_id: str | None, conversation_id: str | None) -> list[RunRecord]:
    """Read the runs matching both filters, oldest first.

    Args:
        connection: The connection the caller holds.
        parent_run_id: Only the runs this run started, or None for any.
        conversation_id: Only the runs of this conversation, as a UUID string, or None for any.

    Returns:
        The runs, oldest first.
    """
    rows = await connection.fetch(SELECT_RUNS, parent_run_id, UUID(conversation_id) if conversation_id is not None else None)
    return [_run_record(found) for found in rows_as(RunRow, rows)]


async def events(connection: Connection, run_id: str) -> list[StepEvent]:
    """Read a run's events in order.

    Args:
        connection: The connection the caller holds.
        run_id: The run.

    Returns:
        The events, in the order they were appended.
    """
    rows = await connection.fetch(SELECT_EVENTS, run_id)
    return [_step_event(event) for event in rows_as(EventRow, rows)]


async def events_after(connection: Connection, position: int, *, kinds: Sequence[EventKind], limit: int) -> list[EventAtPositionRow]:
    """Read the record's events past a position, oldest position first, with each run's agent and conversation.

    Positions are assigned when a row is inserted, not when its transaction commits, so a reader that advances past a
    position can miss a row whose transaction committed later than a higher id. A consumer that must not miss one
    re-reads a window behind its position or relies on the writer's transactions being short.

    Args:
        connection: The connection the caller holds.
        position: The `id` of the last event a consumer handled, 0 before the first.
        kinds: The event types to read, as `EventKind` values.
        limit: The most events to return.

    Returns:
        The events, oldest position first.
    """
    rows = await connection.fetch(SELECT_EVENTS_AFTER, position, list(kinds), limit)
    return rows_as(EventAtPositionRow, rows)


TERMINAL_KINDS: Final[tuple[EventKind, ...]] = ("run_completed", "run_failed")
"""The two events a run ends with."""


async def last_event_position(connection: Connection) -> int:
    """Read the highest position any event holds, which is where a new consumer starts.

    Args:
        connection: The connection the caller holds.

    Returns:
        The position, 0 when the record holds no event.
    """
    row = await connection.fetchrow(SELECT_LAST_EVENT_POSITION)
    return 0 if row is None else row_as(PositionRow, row).position


async def runs_ended_since(connection: Connection, since: datetime, *, limit: int, after_position: int = 0) -> list[EventAtPositionRow]:
    """Read the events that ended a run at or after `since`, oldest position first, with the run's agent and conversation.

    One event per run that ended: a run ends once, with `run_completed` or `run_failed`. Restarting the window on the
    next pass catches late commits behind the page cursor.

    Args:
        connection: The connection the caller holds.
        since: The earliest end to read.
        limit: The most events to return.
        after_position: The previous page's last event id, 0 for the first page.

    Returns:
        The events, oldest position first.
    """
    rows = await connection.fetch(SELECT_RUNS_ENDED_SINCE, list(TERMINAL_KINDS), since, limit, after_position)
    return rows_as(EventAtPositionRow, rows)


async def latest_snapshot(connection: Connection, run_id: str, *, include_interrupted: bool) -> ContinuableSnapshot | None:
    """Read the run's newest snapshot, complete ones only unless asked otherwise.

    Args:
        connection: The connection the caller holds.
        run_id: The run.
        include_interrupted: Whether a step saved interrupted counts.

    Returns:
        The snapshot, or None when the run is unknown or has no step that counts.

    Raises:
        ValueError: when a message the step's entries name has no stored form or stamp version.
    """
    latest = SELECT_LATEST_STEP if include_interrupted else SELECT_LATEST_COMPLETE_STEP
    found = await run(connection, run_id)
    if found is None:
        return None
    row = await connection.fetchrow(latest, run_id)
    if row is None:
        return None
    return await snapshot_of(connection, found, row_as(StepRow, row))


async def snapshots(connection: Connection, run_id: str, *, include_interrupted: bool) -> list[ContinuableSnapshot]:
    """Read every one of the run's steps, oldest first, complete ones only unless asked otherwise.

    The whole run rather than what it ended holding: a message a later step replaced is still in the step that
    first held it, which is what a reader searching the run's own history reads.

    Args:
        connection: The connection the caller holds.
        run_id: The run.
        include_interrupted: Whether steps saved interrupted count.

    Returns:
        The snapshots, oldest first, empty for an unknown run.

    Raises:
        ValueError: when a message the step's entries name has no stored form or stamp version.
    """
    found = await run(connection, run_id)
    if found is None:
        return []
    rows = await connection.fetch(SELECT_STEPS if include_interrupted else SELECT_COMPLETE_STEPS, run_id)
    return [await snapshot_of(connection, found, step) for step in rows_as(StepRow, rows)]


async def recorded_steps(connection: Connection, found: RunRow) -> list[RecordedStep]:
    """Read every step of the run, oldest first, interrupted ones included, each with the ordinals its entries name.

    What a reader of the whole run reads: the history as the model was given it at every step, and the ordinal that ties
    one message across the steps whatever form it has in each.

    Args:
        connection: The connection the caller holds.
        found: The run's row.

    Returns:
        The steps, oldest first.

    Raises:
        ValueError: when a message the step's entries name has no stored form or stamp version.
    """
    rows = await connection.fetch(SELECT_STEPS, found.run_id)
    return [await _recorded_step(connection, found, step) for step in rows_as(StepRow, rows)]


async def step_parameters(connection: Connection, run_id: str) -> dict[int, dict[str, JsonValue]]:
    """Read the request parameters recorded for a run.

    Args:
        connection: The connection the caller holds.
        run_id: The run.

    Returns:
        Each step's parameters by step index. A step nothing was recorded for is absent.
    """
    rows = await connection.fetch(SELECT_STEP_PARAMETERS, run_id)
    return {row.step_index: row.parameters for row in rows_as(StepParametersRow, rows)}


async def conversation_messages(connection: Connection, conversation_id: UUID) -> list[ModelMessage]:
    """Read every message of the conversation once, in its first form: what happened, for a reader.

    Args:
        connection: The connection the caller holds.
        conversation_id: The conversation.

    Returns:
        The messages.
    """
    rows = await connection.fetch(SELECT_FIRST_MESSAGES, conversation_id)
    return messages_of(await _merged(connection, rows_as(MessagePiecesRow, rows)))


async def history_for_next_run(connection: Connection, conversation_id: UUID) -> list[ModelMessage]:
    """Read the newest complete step of the conversation's newest top-level run.

    A run another run started carries a parent and is skipped: what it saw is the parent's own business. Runs that start
    at the same instant are ordered by run id, which pydantic-ai draws in time order.

    Args:
        connection: The connection the caller holds.
        conversation_id: The conversation.

    Returns:
        The step's messages, or an empty list when the conversation has no such step.

    Raises:
        ValueError: when a message the step's entries name has no stored form or stamp version.
    """
    row = await connection.fetchrow(SELECT_LATEST_STEP_OF_CONVERSATION, conversation_id)
    if row is None:
        return []
    step = row_as(StepEntriesRow, row)
    return messages_of(await _step_messages(connection, conversation_id, run_id=step.run_id, seq=step.seq, entries=step.entries))


async def tool_effect(connection: Connection, *, run_id: str, tool_call_id: str) -> ToolEffectRecord | None:
    """Read the effect of one tool call.

    Args:
        connection: The connection the caller holds.
        run_id: The run the call belongs to.
        tool_call_id: The call.

    Returns:
        The effect, or None when none was recorded.
    """
    row = await connection.fetchrow(SELECT_TOOL_EFFECT, run_id, tool_call_id)
    return _tool_effect(row_as(ToolEffectRow, row)) if row is not None else None


async def started_tool_effects(connection: Connection, run_id: str) -> list[ToolEffectRecord]:
    """Read the tool calls that were in flight when the run last wrote.

    Args:
        connection: The connection the caller holds.
        run_id: The run.

    Returns:
        Their effects.
    """
    rows = await connection.fetch(SELECT_STARTED_TOOL_EFFECTS, run_id)
    return [_tool_effect(effect) for effect in rows_as(ToolEffectRow, rows)]


async def step_at(connection: Connection, *, run_id: str, seq: int) -> StepRow | None:
    """Read the run's step at exactly this sequence, whatever state it was saved in.

    Args:
        connection: The connection the caller holds.
        run_id: The run.
        seq: The step's sequence in the run.

    Returns:
        The step, or None when the run wrote no such step.
    """
    row = await connection.fetchrow(SELECT_STEP_AT_SEQ, run_id, seq)
    return row_as(StepRow, row) if row is not None else None


async def latest_complete_step(connection: Connection, run_id: str) -> StepRow | None:
    """Read the run's newest settled step.

    Args:
        connection: The connection the caller holds.
        run_id: The run.

    Returns:
        The step, or None when the run has none.
    """
    row = await connection.fetchrow(SELECT_LATEST_COMPLETE_STEP, run_id)
    return row_as(StepRow, row) if row is not None else None


async def continuation_attempt(connection: Connection, *, conversation_id: UUID, attempt_id: str) -> ContinuationAttemptRow | None:
    """Read one conversation's attempt under this id.

    Args:
        connection: The connection the caller holds.
        conversation_id: The conversation.
        attempt_id: The attempt.

    Returns:
        The attempt, or None when nothing has claimed the id.
    """
    row = await connection.fetchrow(SELECT_CONTINUATION_ATTEMPT, conversation_id, attempt_id)
    return row_as(ContinuationAttemptRow, row) if row is not None else None


async def started_continuation_attempts(connection: Connection, conversation_id: UUID) -> list[ContinuationAttemptRow]:
    """Read every attempt on this conversation that began and never settled.

    Args:
        connection: The connection the caller holds.
        conversation_id: The conversation.

    Returns:
        The attempts, oldest first.
    """
    rows = await connection.fetch(SELECT_STARTED_CONTINUATION_ATTEMPTS, conversation_id)
    return rows_as(ContinuationAttemptRow, rows)


async def continuation_head(connection: Connection, conversation_id: UUID) -> ContinuationHeadRow | None:
    """Read how far this conversation has been carried.

    Args:
        connection: The connection the caller holds.
        conversation_id: The conversation.

    Returns:
        The head, or None before the conversation's first commit.
    """
    row = await connection.fetchrow(SELECT_CONTINUATION_HEAD, conversation_id)
    return row_as(ContinuationHeadRow, row) if row is not None else None


async def conversation_record(connection: Connection, conversation_id: UUID) -> ConversationRecord:
    """Read what a write needs of the conversation: the digest of every form and of every stamp version, no content.

    The messages come back in ordinal order, which is the order the record holds them in. The steps stay unread: their
    numbering comes from `max(seq)` in the database, and the record holds the one step being written.

    Args:
        connection: The connection the caller holds.
        conversation_id: The conversation.

    Returns:
        A record that holds the digests and no step.
    """
    record = ConversationRecord()
    rows = await connection.fetch(SELECT_CONVERSATION_DIGESTS, conversation_id)
    for message in rows_as(MessageDigestsRow, rows):
        record.forms[message.key] = message.form_digests
        record.stamps[message.key] = message.stamp_digests
    return record


async def snapshot_of(connection: Connection, found: RunRow, step: StepRow) -> ContinuableSnapshot:
    """Read one step as the `StepStore` port hands it back: the run's lineage, and the messages the step's entries name.

    Args:
        connection: The connection the caller holds.
        found: The run's row.
        step: The step's row.

    Returns:
        The snapshot.

    Raises:
        ValueError: when a message the step's entries name has no stored form or stamp version.
    """
    rendered = await _step_messages(connection, found.conversation_id, run_id=found.run_id, seq=step.seq, entries=step.entries)
    return ContinuableSnapshot(
        run_id=found.run_id,
        step_index=step.step_index,
        messages=messages_of(rendered),
        conversation_id=str(found.conversation_id),
        parent_run_id=found.parent_run_id,
        agent_name=found.agent_name,
        timestamp=step.at,
        state=step.state,
    )


async def _recorded_step(connection: Connection, found: RunRow, step: StepRow) -> RecordedStep:
    """One step read back whole: the messages its entries name, and the ordinals out of those entries."""
    rendered = await _step_messages(connection, found.conversation_id, run_id=found.run_id, seq=step.seq, entries=step.entries)
    return RecordedStep(
        seq=step.seq,
        step_index=step.step_index,
        at=step.at,
        state=step.state,
        ordinals=tuple(step.entries[0::3]),
        messages=messages_of(rendered),
    )


async def _step_messages(
    connection: Connection, conversation_id: UUID, *, run_id: str, seq: int, entries: Sequence[int]
) -> list[MessageJson]:
    """Rebuild one snapshot's messages from its entries in two statements, refusing an entry with no stored piece."""
    triples = [(entries[i], entries[i + 1], entries[i + 2]) for i in range(0, len(entries), 3)]
    if not triples:
        return []
    rows = await connection.fetch(
        SELECT_STEP_MESSAGES,
        conversation_id,
        [ordinal for ordinal, _, _ in triples],
        [form for _, form, _ in triples],
        [version for _, _, version in triples],
    )
    pieces = rows_as(StepMessagePieceRow, rows)
    found = {(piece.ordinal, piece.form, piece.version) for piece in pieces}
    missing = next((triple for triple in triples if triple not in found), None)
    if missing is not None:
        ordinal, form, version = missing
        msg = f"run {run_id!r} seq {seq}: missing message ordinal {ordinal} form {form} version {version}"
        raise ValueError(msg)
    return await _merged(connection, [MessagePiecesRow(content=piece.content, stamps=piece.stamps) for piece in pieces])


async def _merged(connection: Connection, pieces: Sequence[MessagePiecesRow]) -> list[MessageJson]:
    """One message per stored pair, in the order the pairs came back, instructions text restored."""
    texts = await _instruction_texts(connection, instructions_digests(piece.stamps for piece in pieces))
    return [message_from(piece.content, piece.stamps, texts) for piece in pieces]


async def _instruction_texts(connection: Connection, digests: set[str]) -> dict[str, str]:
    """The instructions texts behind a set of digests."""
    if not digests:
        return {}
    rows = await connection.fetch(SELECT_INSTRUCTIONS, sorted(digests))
    return {instruction.digest: instruction.text for instruction in rows_as(InstructionRow, rows)}


def _run_record(found: RunRow) -> RunRecord:
    """One `runs` row as the record the port hands back."""
    return RunRecord(
        run_id=found.run_id,
        conversation_id=str(found.conversation_id),
        parent_run_id=found.parent_run_id,
        agent_name=found.agent_name,
        metadata=found.metadata,
        started_at=found.started_at,
        registration_id=found.registration_id,
    )


def _step_event(event: EventRow) -> StepEvent:
    """One `run_events` row as the event the port hands back."""
    return StepEvent(
        run_id=event.run_id,
        kind=event.kind,
        step_index=event.step_index,
        timestamp=event.at,
        tool_call_id=event.tool_call_id,
        tool_name=event.tool_name,
        error=event.error,
        metadata=event.metadata,
    )


def _tool_effect(effect: ToolEffectRow) -> ToolEffectRecord:
    """One `tool_effects` row as the record the port hands back."""
    return ToolEffectRecord(
        tool_call_id=effect.tool_call_id,
        tool_name=effect.tool_name,
        run_id=effect.run_id,
        status=effect.status,
        started_at=effect.started_at,
        ended_at=effect.ended_at,
        idempotency_key=effect.idempotency_key,
        effect_summary=effect.effect_summary,
    )
