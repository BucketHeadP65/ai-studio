"""The pydantic-ai harness `StepStore` over Postgres: every message once, every step as entries."""

from __future__ import annotations

from typing import TYPE_CHECKING
from uuid import UUID

from core.ai.history.infrastructure import checkpoints, reads, writes

if TYPE_CHECKING:
    from collections.abc import Mapping

    from pydantic import JsonValue
    from pydantic_ai.messages import ModelMessage
    from pydantic_ai_harness.step_persistence import ContinuableSnapshot, RunRecord, StepEvent, ToolEffectRecord

    from core.ai.history.internal_contracts import Database


class PostgresStepStore:
    """Keeps runs, events, tool effects and snapshots in Postgres behind the `StepStore` port.

    Each snapshot is stored as the diff against what the conversation already holds. A run must carry a conversation id.
    The application owns the conversations table and creates the row before the run. Every method takes a connection for
    its block and gives it back.
    """

    def __init__(self, database: Database) -> None:
        """Build the store over a database.

        Args:
            database: Where each method takes its connection.
        """
        self.database = database

    async def register_run(self, record: RunRecord) -> None:
        """Insert the run. Registering the same run id twice is a no-op.

        Args:
            record: The run.

        Raises:
            ValueError: when the record carries no conversation id.
        """
        async with self.database.acquire() as connection, connection.transaction():
            await writes.register_run(connection, record)

    async def get_run(self, *, run_id: str) -> RunRecord | None:
        """Read one run.

        Args:
            run_id: The run.

        Returns:
            The run, or None when no run has this id.
        """
        async with self.database.acquire() as connection:
            return await reads.run_record(connection, run_id)

    async def list_runs(self, *, parent_run_id: str | None = None, conversation_id: str | None = None) -> list[RunRecord]:
        """Read the runs matching both filters, oldest first.

        Args:
            parent_run_id: Only the runs this run started, or None for any.
            conversation_id: Only the runs of this conversation, as a UUID string, or None for any.

        Returns:
            The runs, oldest first.
        """
        async with self.database.acquire() as connection:
            return await reads.run_records(connection, parent_run_id=parent_run_id, conversation_id=conversation_id)

    async def append_event(self, event: StepEvent) -> None:
        """Append the event as the run's next one.

        Args:
            event: The event.
        """
        async with self.database.acquire() as connection, connection.transaction():
            await writes.append_event(connection, event)

    async def list_events(self, *, run_id: str) -> list[StepEvent]:
        """Read a run's events in order.

        Args:
            run_id: The run.

        Returns:
            The events, in the order they were appended.
        """
        async with self.database.acquire() as connection:
            return await reads.events(connection, run_id)

    async def save_snapshot(self, snapshot: ContinuableSnapshot) -> None:
        """Record the snapshot as the diff against what the conversation already holds, in one transaction.

        Two runs of one conversation writing at once end in a unique violation on the message the second one also
        saw, never in a silent duplicate, so a caller must let the failure stand rather than retry it.

        Args:
            snapshot: The messages the model was given at one step.

        Raises:
            ValueError: when the run is unknown, when the snapshot names a conversation other than the run's, or when a
                message does not come back from what would be stored for it.
        """
        async with self.database.acquire() as connection, connection.transaction():
            await writes.save_snapshot(connection, snapshot)

    async def latest_snapshot(self, *, run_id: str, include_interrupted: bool = False) -> ContinuableSnapshot | None:
        """Read the run's newest snapshot, reading the run and the step on one connection.

        Args:
            run_id: The run.
            include_interrupted: Whether a step saved interrupted counts.

        Returns:
            The snapshot, or None when the run is unknown or has no step that counts.
        """
        async with self.database.acquire() as connection:
            return await reads.latest_snapshot(connection, run_id, include_interrupted=include_interrupted)

    async def list_snapshots(self, *, run_id: str, include_interrupted: bool = False) -> list[ContinuableSnapshot]:
        """Read every step of the run, oldest first, complete ones only unless asked otherwise.

        What a reader of the run's own history reads: a message a later step replaced is still held by the step
        that first carried it, and the newest step alone would not offer it.

        Args:
            run_id: The run.
            include_interrupted: Whether steps saved interrupted count.

        Returns:
            The snapshots, oldest first, empty for an unknown run.
        """
        async with self.database.acquire() as connection:
            return await reads.snapshots(connection, run_id, include_interrupted=include_interrupted)

    async def record_tool_effect(self, record: ToolEffectRecord) -> None:
        """Insert or update the effect of one tool call.

        Args:
            record: The tool call's effect.
        """
        async with self.database.acquire() as connection, connection.transaction():
            await writes.record_tool_effect(connection, record)

    async def get_tool_effect(self, *, run_id: str, tool_call_id: str) -> ToolEffectRecord | None:
        """Read the effect of one tool call.

        Args:
            run_id: The run the call belongs to.
            tool_call_id: The call.

        Returns:
            The effect, or None when none was recorded.
        """
        async with self.database.acquire() as connection:
            return await reads.tool_effect(connection, run_id=run_id, tool_call_id=tool_call_id)

    async def list_unresolved_tool_effects(self, *, run_id: str) -> list[ToolEffectRecord]:
        """Read the tool calls that were in flight when the run last wrote.

        Args:
            run_id: The run.

        Returns:
            Their effects.
        """
        async with self.database.acquire() as connection:
            return await reads.started_tool_effects(connection, run_id)

    async def record_request_parameters(self, *, run_id: str, step_index: int, parameters: Mapping[str, JsonValue]) -> None:
        """Keep what one request was sent with beside the messages: once per distinct set, one row per step.

        Args:
            run_id: The run the request belongs to.
            step_index: The request's step number in that run.
            parameters: What the request was sent with, as JSON.
        """
        async with self.database.acquire() as connection, connection.transaction():
            await writes.record_request_parameters(connection, run_id=run_id, step_index=step_index, parameters=parameters)

    async def conversation_messages(self, *, conversation_id: str) -> list[ModelMessage]:
        """Read every message of the conversation once, in its first form: what happened, for a reader.

        Args:
            conversation_id: The conversation, as a UUID string.

        Returns:
            The messages.
        """
        async with self.database.acquire() as connection:
            return await reads.conversation_messages(connection, UUID(conversation_id))

    async def delete_conversation(self, conversation_id: str) -> None:
        """Delete every continuation, run, message, step and effect of one conversation, in one transaction.

        The head and its attempts go first.

        Args:
            conversation_id: The conversation, as a UUID string.
        """
        async with self.database.acquire() as connection, connection.transaction():
            await checkpoints.delete_conversation_checkpoints(connection, UUID(conversation_id))
            await writes.delete_conversation(connection, UUID(conversation_id))

    async def history_for_next_run(self, *, conversation_id: str) -> list[ModelMessage]:
        """Read what the model last saw: the newest complete step of the conversation's newest top-level run.

        A run another run started carries a parent and is skipped: what it saw is the parent's own business.
        Runs that start at the same instant are ordered by run id, which pydantic-ai draws in time order.

        Args:
            conversation_id: The conversation, as a UUID string.

        Returns:
            The step's messages, or an empty list when the conversation has no such step.
        """
        async with self.database.acquire() as connection:
            return await reads.history_for_next_run(connection, UUID(conversation_id))
