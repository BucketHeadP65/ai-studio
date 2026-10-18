"""What a transcript does not carry and a rule may need: facts about the tools, declared by the application."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field


class ToolFacts(BaseModel):
    """Facts about the tools an agent holds. Empty means unknown, and a rule that needs a fact it does not have stays silent."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    world_changing: frozenset[str] = Field(
        default=frozenset(), description="Tools whose call changes the world. A repeat of one is damage, not waste."
    )
    result_cap_chars: int | None = Field(default=None, description="The size above which a tool return counts as too large to keep whole.")
