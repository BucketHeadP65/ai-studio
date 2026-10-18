"""The record's history read, without a database behind it."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from pydantic_ai.messages import ModelMessage

    from harness.domain.ports import ConversationHistory, ConversationMessages


class FakeConversationHistory:
    """Answers each conversation whatever a test put under its id, and records what was asked."""

    def __init__(self, histories: dict[str, list[ModelMessage]] | None = None) -> None:
        self.histories = histories if histories is not None else {}
        self.asked: list[str] = []

    async def history_for_next_run(self, *, conversation_id: str) -> list[ModelMessage]:
        """What the model last saw in that conversation, or nothing."""
        self.asked.append(conversation_id)
        return list(self.histories.get(conversation_id, []))


_proof: type[ConversationHistory] = FakeConversationHistory
"""Pyright fails this module the moment `FakeConversationHistory` drifts from the `ConversationHistory` port."""


@dataclass
class FakeConversationMessages:
    """Answers each conversation whatever a test put under its id, and records what was asked."""

    by_conversation: dict[str, list[ModelMessage]] = field(default_factory=dict)
    asked: list[str] = field(default_factory=list)

    async def conversation_messages(self, *, conversation_id: str) -> list[ModelMessage]:
        """Every message of that conversation, or nothing."""
        self.asked.append(conversation_id)
        return list(self.by_conversation.get(conversation_id, []))


_messages_proof: type[ConversationMessages] = FakeConversationMessages
"""Pyright fails this module the moment `FakeConversationMessages` drifts from the `ConversationMessages` port."""
