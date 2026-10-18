"""The request and response bodies of the self-authoring routes."""

from __future__ import annotations

from typing import Literal

from core.ai.workflows.contracts import Tier
from core.thinking import Thinking
from pydantic import BaseModel, Field

from harness.adapters.contracts import ModelEntry
from harness.features.selfauthoring.domain import CustomItemType

_NextStep = Literal["activate", "promote"] | None


class HarnessArtifact(BaseModel, frozen=True):
    """One custom item's listing row: identity, review status, and the one forward act it is ready for."""

    name: str = Field(description="The name it was written under.")
    status: Literal["active", "disabled"] = Field(description="Live, or still pending the owner's review.")
    descriptor: str = Field(description="Its kind-specific summary: a capability's class, an agent's model, a step's functions.")
    last_error: str | None = Field(default=None, description="Why the last write failed validation, if it did.")
    promoted: bool | None = Field(
        default=None, description="Whether it is joined to the capability registry. `null` outside kind=capability."
    )
    next_step: _NextStep = Field(default=None, description="The one forward act this row is ready for, if any.")


class HarnessListing(BaseModel, frozen=True):
    """Every custom item of one type, which is what `GET /api/harness/{kind}` answers."""

    kind: CustomItemType
    artifacts: list[HarnessArtifact]


class HarnessAction(BaseModel, frozen=True):
    """What an activate or disable act did: which custom item, and the state it is in now."""

    kind: CustomItemType
    name: str
    status: Literal["active", "disabled"]


class HarnessDeletion(BaseModel, frozen=True):
    """What a delete did: the custom item is gone from its library, and the act is recorded."""

    kind: CustomItemType
    name: str
    deleted: Literal[True] = True


class PluginState(BaseModel, frozen=True):
    """One built-in plugin: what it is, whether it is on, and whether it can move at all."""

    name: str
    description: str
    enabled: bool
    fixed: bool = Field(description="An always-on plugin cannot be turned off, and a toggle on one is refused.")


class PluginsListing(BaseModel, frozen=True):
    """The whole built-in composition, which is what `GET /api/harness/plugins` answers."""

    plugins: list[PluginState]


class PromotionAction(BaseModel, frozen=True):
    """What a promote or demote act did: the capability, and whether it is promoted now."""

    name: str
    promoted: bool


class HarnessComposition(BaseModel, frozen=True):
    """What this process is actually running: the host agent, its registered steps, and the promoted set.

    The custom-item listings show the pending library. This is the live environment those custom items
    join once activated.
    """

    model: str = Field(description="The model the orchestrator tier runs on.")
    run_engine: Literal["in_process", "temporal"] = Field(description="Where workflow runs execute.")
    steps_registered: tuple[str, ...] = Field(description="Work-registry steps this process can execute, by name.")
    promoted: tuple[str, ...] = Field(description="Capabilities joined to the registry, declarable by name in an agent document.")
    active: dict[CustomItemType, int] = Field(description="Custom items currently active, counted per kind.")


class TierBody(BaseModel, frozen=True):
    """What to put a tier on."""

    model: str = Field(description="A model id from GET /api/harness/models.")
    thinking: Thinking = Field(description="The thinking level, one of `low`, `medium`, `high`, `xhigh`.")


class TierView(BaseModel, frozen=True):
    """One tier as AI Factory Studio shows it."""

    tier: Tier
    role: str = Field(description="What runs on this tier.")
    model: ModelEntry
    thinking: Thinking
    default: bool = Field(description="Whether this is the tier's default rather than the owner's choice.")


class TiersListing(BaseModel, frozen=True):
    """The three tiers in effect, in listing order."""

    tiers: tuple[TierView, ...]


class ModelsListing(BaseModel, frozen=True):
    """A search over every model the deployment can reach, pinned entries first."""

    models: tuple[ModelEntry, ...]


class Selection(BaseModel, frozen=True, use_attribute_docstrings=True):
    """The profile in effect, or `null` when the hand-set plugins govern. GET and PUT share this one shape."""

    name: str | None
    """An active profile's name. `null` on a PUT clears the selection, and on a GET means none is selected."""
