"""Every write the store runs: the run, its events, its steps and the tool effects.

Each function runs on the connection its caller holds, inside the transaction the caller opened. A jsonb value
travels as the JSON text `canonical_json` renders, and the statement casts it.
"""

from __future__ import annotations

from typing import TYPE_CHECKING
from uuid import UUID

from core.ai.history.conversation_record import canonical_json, digest_of, message_kind
from core.ai.history.infrastructure import reads
from core.ai.history.infrastructure.queries import (
    DELETE_CONVERSATION_MESSAGES,
    DELETE_CONVERSATION_RUNS,
    INSERT_EVENT,
    INSERT_MESSAGE_FORMS,
    INSERT_MESSAGE_STAMPS,
    INSERT_MESSAGES,
    INSERT_RUN,
    INSERT_STEP,
    KEEP_INSTRUCTIONS,
    KEEP_REQUEST_PARAMETERS,
    LOCK_RUN,
    NEXT_STEP_SEQ,
    POINT_STEP_AT_PARAMETERS,
    UPSERT_TOOL_EFFECT,
)
from core.ai.history.infrastructure.rows import SeqRow, row_as

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence

    from pydantic import JsonValue
    from pydantic_ai_harness.step_persistence import ContinuableSnapshot, RunRecord, StepEvent, ToolEffectRecord

    from core.ai.history.conversation_record import ConversationRecord, MessageJson
    from core.ai.history.domain import StoredStep
    from core.ai.history.internal_contracts import Connection, Row


async def register_run(connection: Connection, record: RunRecord) -> None:
    """Insert the run. Registering the same run id twice is a no-op.

    Args:
        connection: The connection the caller's transaction runs on.
        record: The run.

    Raises:
        ValueError: when the record carries no conversation id.
    """
    if record.conversation_id is None:
        msg = f"run {record.run_id!r} has no conversation id, and every run belongs to a conversation"
        raise ValueError(msg)
    await connection.execute(INSERT_RUN, *_run_row(record, UUID(record.conversation_id)))


async def append_event(connection: Connection, event: StepEvent) -> None:
    """Append the event, which the insert numbers one past the run's last one.

    The tool calls of one response run in parallel and each records its own events, so the run is locked for the
    transaction that writes this one: waiters queue behind the lock instead of claiming the same number.

    Args:
        connection: The connection the caller's transaction runs on.
        event: The event.
    """
    await connection.execute(LOCK_RUN, event.run_id)
    await connection.execute(INSERT_EVENT, *_event_row(event))


async def save_snapshot(connection: Connection, snapshot: ContinuableSnapshot) -> None:
    """Record the snapshot as the diff against what the conversation already holds.

    The step takes the run's next number and is written first, then what the conversation did not hold yet.

    Args:
        connection: The connection the caller's transaction runs on.
        snapshot: The messages the model was given at one step.

    Raises:
        ValueError: when the run is unknown, when the snapshot names a conversation other than the run's, or when a
            message does not come back from what would be stored for it.
    """
    conversation_id = await _conversation_of(connection, snapshot)
    record = await reads.conversation_record(connection, conversation_id)
    seq = _next_seq(await connection.fetchrow(NEXT_STEP_SEQ, snapshot.run_id))
    record.record(snapshot, seq=seq)
    await connection.execute(INSERT_STEP, *_step_row(record.steps[-1], _ordinals(record)))
    await _write_additions(connection, conversation_id, record)


async def record_tool_effect(connection: Connection, record: ToolEffectRecord) -> None:
    """Insert or update the effect of one tool call.

    Args:
        connection: The connection the caller's transaction runs on.
        record: The tool call's effect.
    """
    await connection.execute(UPSERT_TOOL_EFFECT, *_tool_effect_row(record))


async def record_request_parameters(connection: Connection, *, run_id: str, step_index: int, parameters: Mapping[str, JsonValue]) -> None:
    """Keep what one request was sent with beside the messages: once per distinct set, one row per step.

    Args:
        connection: The connection the caller's transaction runs on.
        run_id: The run the request belongs to.
        step_index: The request's step number in that run.
        parameters: What the request was sent with, as JSON.
    """
    sent_with = dict(parameters)
    digest = digest_of(sent_with)
    await connection.execute(KEEP_REQUEST_PARAMETERS, digest, canonical_json(sent_with))
    await connection.execute(POINT_STEP_AT_PARAMETERS, run_id, step_index, digest)


async def delete_conversation(connection: Connection, conversation_id: UUID) -> None:
    """Delete everything the conversation holds.

    The runs take their events, steps, request parameters and tool effects with them, and the messages take their
    forms and their stamps. Instructions and request parameters are content-addressed and shared, so they stay.

    Args:
        connection: The connection the caller's transaction runs on.
        conversation_id: The conversation.
    """
    await connection.execute(DELETE_CONVERSATION_RUNS, conversation_id)
    await connection.execute(DELETE_CONVERSATION_MESSAGES, conversation_id)


async def _conversation_of(connection: Connection, snapshot: ContinuableSnapshot) -> UUID:
    """The conversation the snapshot belongs to, refusing an unknown run or a snapshot that names another conversation."""
    run = await reads.run(connection, snapshot.run_id)
    if run is None:
        msg = f"run {snapshot.run_id!r} is not registered"
        raise ValueError(msg)
    belongs_to = str(run.conversation_id)
    if snapshot.conversation_id is not None and snapshot.conversation_id != belongs_to:
        msg = f"snapshot of run {snapshot.run_id!r} names conversation {snapshot.conversation_id!r}, the run belongs to {belongs_to!r}"
        raise ValueError(msg)
    return run.conversation_id


async def _write_additions(connection: Connection, conversation_id: UUID, record: ConversationRecord) -> None:
    """Write what the step brought that the conversation did not hold, `messages` before the forms and stamps that point at it."""
    step = record.steps[-1]
    ordinals = _ordinals(record)
    new_messages = [(key, content) for (key, form), content in record.new_forms.items() if form == 0]
    if record.new_instructions:
        await connection.execute(KEEP_INSTRUCTIONS, *_instruction_rows(record.new_instructions))
    if new_messages:
        await connection.execute(INSERT_MESSAGES, *_message_rows(conversation_id, ordinals, new_messages, step))
    if record.new_forms:
        await connection.execute(INSERT_MESSAGE_FORMS, *_form_rows(conversation_id, ordinals, record.new_forms))
    if record.new_stamps:
        await connection.execute(INSERT_MESSAGE_STAMPS, *_stamps_rows(conversation_id, ordinals, record.new_stamps))


def _run_row(record: RunRecord, conversation_id: UUID) -> tuple[object, ...]:
    """What the `runs` row carries: run, conversation, parent, agent, start, metadata, registration."""
    return (
        record.run_id,
        conversation_id,
        record.parent_run_id,
        record.agent_name,
        record.started_at,
        canonical_json(dict(record.metadata)),
        record.registration_id,
    )


def _event_row(event: StepEvent) -> tuple[object, ...]:
    """What the `run_events` row carries: run, kind, step, time, tool call, tool, error, metadata. The number is the statement's."""
    return (
        event.run_id,
        event.kind,
        event.step_index,
        event.timestamp,
        event.tool_call_id,
        event.tool_name,
        event.error,
        canonical_json(dict(event.metadata)),
    )


def _tool_effect_row(record: ToolEffectRecord) -> tuple[object, ...]:
    """What the `tool_effects` row carries: run, call, tool, status, start, end, key, summary."""
    return (
        record.run_id,
        record.tool_call_id,
        record.tool_name,
        record.status,
        record.started_at,
        record.ended_at,
        record.idempotency_key,
        record.effect_summary,
    )


def _step_row(step: StoredStep, ordinals: Mapping[str, int]) -> tuple[object, ...]:
    """What the `steps` row carries: run, number, step, time, state, and the entries flattened to ordinal, form and stamp version."""
    return (
        step.run_id,
        step.seq,
        step.step_index,
        step.at,
        step.state,
        [number for entry in step.entries for number in (ordinals[entry.key], entry.form, entry.stamp)],
    )


def _instruction_rows(texts: Mapping[str, str]) -> tuple[object, ...]:
    """What the `instructions` rows carry: every digest, then every text."""
    return (list(texts), list(texts.values()))


def _message_rows(
    conversation_id: UUID, ordinals: Mapping[str, int], messages: Sequence[tuple[str, MessageJson]], step: StoredStep
) -> tuple[object, ...]:
    """What the `messages` rows carry: the conversation, the step that first saw them all, then the ordinals, keys and kinds."""
    return (
        conversation_id,
        step.run_id,
        step.seq,
        step.at,
        [ordinals[key] for key, _ in messages],
        [key for key, _ in messages],
        [message_kind(content) for _, content in messages],
    )


def _form_rows(conversation_id: UUID, ordinals: Mapping[str, int], forms: Mapping[tuple[str, int], MessageJson]) -> tuple[object, ...]:
    """What the `message_forms` rows carry: the conversation, then the ordinals, forms, contents and content addresses."""
    return (
        conversation_id,
        [ordinals[key] for key, _ in forms],
        [form for _, form in forms],
        [canonical_json(content) for content in forms.values()],
        [digest_of(content) for content in forms.values()],
    )


def _stamps_rows(conversation_id: UUID, ordinals: Mapping[str, int], stamps: Mapping[tuple[str, int], MessageJson]) -> tuple[object, ...]:
    """What the `message_stamps` rows carry: the conversation, then the ordinals, versions, stamps and content addresses."""
    return (
        conversation_id,
        [ordinals[key] for key, _ in stamps],
        [version for _, version in stamps],
        [canonical_json(sent_with) for sent_with in stamps.values()],
        [digest_of(sent_with) for sent_with in stamps.values()],
    )


def _ordinals(record: ConversationRecord) -> dict[str, int]:
    """Where each message of the conversation sits, the order the record holds them in."""
    return {key: ordinal for ordinal, key in enumerate(record.forms)}


def _next_seq(row: Row | None) -> int:
    """The number the next step takes: what `max(seq) + 1` answered, or 0 for a run with no step yet."""
    return row_as(SeqRow, row).seq if row is not None else 0
