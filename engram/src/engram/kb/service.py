"""The knowledge base's use cases."""

from __future__ import annotations

from typing import TYPE_CHECKING

from core.cqrs.contracts import Command, narrow_command

from engram.kb.domain.retrieval_policy import ApplyRetrievalPolicy, RetrievalPolicyReport
from engram.kb.infrastructure.retrieval_policy_files import apply_retrieval_policy, restore_previous_policy

if TYPE_CHECKING:
    from core.cqrs.contracts import Event


class ApplyRetrievalPolicyHandler:
    """Handles `ApplyRetrievalPolicy`: write the knowledge base's live policy, snapshotting the outgoing one.

    A command carrying a policy makes that policy live. A command carrying none restores the
    latest snapshot. Raises when a restore is asked of a knowledge base with no history.
    """

    def __init__(self) -> None:
        self.events: list[Event] = []

    async def __call__(self, command: Command) -> RetrievalPolicyReport:
        """Bus entry point (the `CommandHandler` `__call__` contract), narrowed defensively.

        Args:
            command: The command to handle, which must be an `ApplyRetrievalPolicy`.

        Returns:
            What `handle` returns.
        """
        command = narrow_command(command, ApplyRetrievalPolicy, handler="ApplyRetrievalPolicyHandler")  # pragma: no cover (the bus routes by type)
        return await self.handle(command)

    async def handle(self, command: ApplyRetrievalPolicy) -> RetrievalPolicyReport:
        """Apply the command's policy, or restore the previous one when it carries none.

        Args:
            command: The command naming the knowledge base and the policy, or no policy.

        Returns:
            The report of what was written.

        Raises:
            ValueError: When a restore is asked of a knowledge base with no policy history.
        """
        if command.policy is None:
            return restore_previous_policy(command.knowledge_base_path)
        return apply_retrieval_policy(command.policy, knowledge_base_path=command.knowledge_base_path)
