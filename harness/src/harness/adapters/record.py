"""The record as the history the recall capability searches, narrowed to one conversation."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from core.ai.history.contracts import message_key

if TYPE_CHECKING:
    from pydantic_ai.messages import ModelMessage
    from pydantic_ai_harness.step_persistence import RunRecord

    from harness.domain.contracts import SessionTranscript


@dataclass(frozen=True)
class RecordHistorySource:
    """One conversation's runs and their messages, for the recall capability to index.

    `conversation_id` keeps one session's recall out of another's history, since one record holds every session's
    runs. A `None` conversation lists no runs.
    """

    store: SessionTranscript
    conversation_id: str | None

    async def list_runs(self) -> list[RunRecord]:
        """This conversation's runs.

        Returns:
            The runs, oldest first, empty when no conversation is named.
        """
        if self.conversation_id is None:
            return []
        return await self.store.list_runs(conversation_id=self.conversation_id)

    async def run_history(self, *, run_id: str) -> list[ModelMessage]:
        """Every message the run's steps hold, each once, in the order it first appeared.

        Every step is read, not only the newest, so both the original messages and the summary compaction replaced them
        with are returned. Two messages are told apart by the record's own key for a message, never by the stamps a
        re-send put on it. A summary is a message of its own and keeps its place.

        Args:
            run_id: The run to read.

        Returns:
            The messages, in the order each first appeared.
        """
        seen: set[str] = set()
        history: list[ModelMessage] = []
        for step in await self.store.list_snapshots(run_id=run_id):
            for message in step.messages:
                held = message_key(message)
                if held in seen:
                    continue
                seen.add(held)
                history.append(message)
        return history
