"""What the library's queries return."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Literal

from pydantic import AwareDatetime, BaseModel, ConfigDict

from core.ai.workflows.domain.consumers import ConsumerEntry
from core.ai.workflows.domain.flags import FlagKind

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


class FlagCard(BaseModel):
    """One maintenance flag as a list shows it: its id, kind and status, what it is about, and its two sections."""

    model_config = ConfigDict(frozen=True)

    id: str
    """The flag's page id, `mf.<short kind>.<workflow name>.<step>` or `mf.<short kind>.<step>`."""
    kind: FlagKind
    status: Literal["open", "resolved"]
    step: str
    """The reusable step's name."""
    workflow: str
    """The consumer's workflow version id, empty for a flag about a step."""
    title: str
    summary: str
    raised_at: AwareDatetime
    """When the flag was raised, written in JSON as ISO 8601 with its offset."""
    evidence: str
    proposed_change: str
