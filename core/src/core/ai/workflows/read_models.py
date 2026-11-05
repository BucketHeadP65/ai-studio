"""What the library's queries return."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from pydantic import BaseModel, ConfigDict

from core.ai.workflows.domain.consumers import ConsumerEntry

if TYPE_CHECKING:
    from collections.abc import Mapping
    from datetime import datetime

    from core.ai.workflows.domain.intent import WorkflowIntent
    from core.ai.workflows.domain.presentation import WorkflowPresentation


@dataclass(frozen=True)
class SavedWorkflow:
    """One candidate as a library surfaces it: enough to choose by, never the whole item.

    A workflow or a reusable step, whichever the search asked for. `applicability` should say when
    *not* to use the item. `provisional` is true while the item has not earned trust yet.
    """

    id: str
    title: str
    summary: str
    applicability: str = ""
    score: float | None = None
    provisional: bool = True
    archived_at: datetime | None = None
    """When this item was archived (timezone-aware UTC), or None while it is live."""
    metadata: Mapping[str, object] = field(default_factory=dict)
    """Whatever the library knows that this protocol does not model. Read it if you know
    the library. Ignore it if you do not."""
    presentation: WorkflowPresentation | None = None
    """The author's words for the surfaces a person meets this workflow on, when the library kept
    any. `None` means the card carries none. This code never invents them."""
    intent: WorkflowIntent | None = None
    """What the person who wanted this automation asked for, when the library kept it. `None` means
    nobody stated an intent. No intent is ever inferred from the workflow."""


class ConsumerCard(BaseModel):
    """One live workflow version that holds a copy of a reusable step, with what its consumer record says about that copy."""

    model_config = ConfigDict(frozen=True)

    workflow_id: str
    """The workflow version's library id, `wf.<name>.v<version>`."""
    name: str
    version: int
    entry: ConsumerEntry
    """The record's entry for the step that was asked about."""
