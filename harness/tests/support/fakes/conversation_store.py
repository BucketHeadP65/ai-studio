"""A transcript that can also erase a conversation, recording which ones it was asked to erase."""

from __future__ import annotations

from tests.support.fakes.step_store import FakeStepStore


class FakeConversationStore(FakeStepStore):
    """The record a delete route reaches: every step-store read, plus the cascade, in memory.

    `deleted` is the conversation ids it was asked to erase, in the order it was asked, so a test
    asserts on which conversation went rather than on the call having happened at all.
    """

    def __init__(self) -> None:
        super().__init__()
        self.deleted: list[str] = []

    async def delete_conversation(self, conversation_id: str) -> None:
        """Record the erasure and drop that conversation's runs, the way the cascade does."""
        self.deleted.append(conversation_id)
        self.runs = [run for run in self.runs if run.conversation_id != conversation_id]
