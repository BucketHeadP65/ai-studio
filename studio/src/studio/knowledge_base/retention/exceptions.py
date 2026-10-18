"""The error a preview or a retention raises when it will not happen."""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from studio.knowledge_base.retention.domain import RetentionRefusal


class RetentionRefusedError(Exception):
    """One preview or retention that will not happen, with the reason and the sentence a person reads."""

    def __init__(self, refusal: RetentionRefusal, message: str) -> None:
        """Hold the reason beside the message.

        Args:
            refusal: Why the request was refused.
            message: The sentence a person reads.
        """
        super().__init__(message)
        self.refusal = refusal
