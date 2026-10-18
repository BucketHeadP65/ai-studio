"""The value types of the progressive-disclosure protocol, shared by every provider.

A `DisclosureEnvelope` is what a provider returns for one disclosed unit, whatever knowledge base sits behind it.
Its `children` are `ChildSummary` stubs a caller opens further by handing their `ref` back to the provider's tool.
No envelope or child field carries a lifecycle status, so never assume a disclosed or listed unit is `active`.
`kind` is the provider's own unit type and not a closed set. Test a unit's level by its ref prefix, never by a `kind` literal.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

DisclosureStatus = Literal["active", "superseded", "outdated", "pending"]
"""A disclosed unit's lifecycle: current, replaced by a newer unit, stale but not yet replaced, or not yet disclosed or indexed."""

DisclosureMode = Literal["strict", "auto", "search_only"]
"""How a disclosure call resolves: exactly the requested depth, the provider's own widening or narrowing, or child stubs without body."""


class ChildSummary(BaseModel):
    """One child stub under a disclosed unit: enough to decide whether to disclose it further."""

    model_config = ConfigDict(frozen=True)

    ref: str = Field(description="The id to read next. Fetch it exactly as written to open this child.")
    kind: str = Field(description="What kind of unit this child is in its knowledge base.")
    title: str = Field(description="The child's display title.")
    summary: str = Field(description="One line to decide whether this child is worth opening. Every level deeper costs tokens.")


class DisclosureEnvelope(BaseModel):
    """One disclosed unit: its identity, disclosed body, child stubs, and the mode it was rendered under."""

    model_config = ConfigDict(frozen=True)

    ref: str = Field(description="This unit's id, as resolved.")
    kind: str = Field(description="What kind of unit this is in its knowledge base.")
    title: str = Field(description="The unit's display title.")
    summary: str | None = Field(default=None, description="The unit's one-line summary, when it has one.")
    content: str | None = Field(default=None, description="The unit's own text at this level. None when this level only lists children.")
    children: tuple[ChildSummary, ...] = Field(
        default=(), description="One-line stubs of everything directly inside. Each ref is readable next. Empty for a unit with no children."
    )
    mode: DisclosureMode = Field(default="strict", description="How this knowledge base discloses: 'search_only' serves no browsable root map.")


def approx_tokens(envelope: DisclosureEnvelope) -> int:
    """Estimate an envelope's token cost as one token per four characters of its JSON.

    Args:
        envelope: The envelope to estimate.

    Returns:
        The estimated token count.
    """
    return len(envelope.model_dump_json()) // 4
