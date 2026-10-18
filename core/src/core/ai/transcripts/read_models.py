"""What the run ports answer: one summary line per run."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from datetime import datetime


@dataclass(frozen=True)
class RunSummary:
    """One line about a run of a conversation: what it is, who ran it, when, what it was asked, how it ended."""

    source: str
    """The run as a transcript source, `<scheme>:<locator>`, so a reader can open it."""
    agent: str | None
    """The agent that ran it, when the source records one."""
    started_at: datetime
    """When the run began."""
    first_prompt: str
    """The first thing the user asked in it, cut short. Empty when it asked nothing."""
    last_event_type: str
    """The type of the last event the run recorded, or `no events`."""
    ended_at: datetime | None = None
    """When the last event was recorded, or None when the run recorded none."""
