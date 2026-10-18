"""The values the inspection slice answers with: one more answer from the inspector, and one command issued by hand."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from uuid import UUID


@dataclass(frozen=True)
class Answered:
    """One more answer from the inspector, and the model that spoke."""

    answer: str
    conversation_id: UUID
    """The conversation the exchange is recorded under."""
    model: str


@dataclass(frozen=True)
class Issued:
    """One command issued for one watch, or the one already in flight."""

    watch: str
    """The watch's name."""
    job_id: int
    created: bool
    """Whether this call queued the command. False when one was already in flight."""
