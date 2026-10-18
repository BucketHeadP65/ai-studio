"""The custom items the agent writes for itself: their item types, their manifest entries and the acts on them."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field

CustomItemType = Literal["capability", "step", "agent", "profile"]
"""Every custom-item type the `harness` CLI and its HTTP routes act on. Promotion applies to capabilities only and is no further type."""

CUSTOM_ITEM_DIRECTORIES: dict[str, str] = {"capability": "capabilities", "step": "steps", "agent": "agents", "profile": "profiles"}
"""Each custom-item type's folder under the custom root, in the order a refusal lists them.

Keyed by plain `str`, so a CLI argument that names no item type can be looked up before it is refused by name."""


class _Activation(BaseModel):
    """What every activation event carries: which item, which way it went, and through what."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    name: str
    action: Literal["activate", "deactivate", "delete"]
    """Which way the flip went. A delete takes the item off disk as well as out of the setup."""
    via: Literal["cli", "agent"]
    """Where the act came through: `cli` for the owner, `agent` for the agent's own tools.

    An activation is never `agent`, because an agent never activates its own item. A deactivation is
    recorded whichever surface it came through."""
    at: datetime = Field(default_factory=lambda: datetime.now(UTC))


class CapabilityActivated(_Activation):
    """One act on a custom capability."""

    kind: Literal["capability_activated"] = "capability_activated"


class StepActivated(_Activation):
    """One act on a custom step."""

    kind: Literal["step_activated"] = "step_activated"


class AgentActivated(_Activation):
    """One act on a custom agent."""

    kind: Literal["agent_activated"] = "agent_activated"


class ProfileActivated(_Activation):
    """One act on a custom profile."""

    kind: Literal["profile_activated"] = "profile_activated"


class CapabilityPromoted(_Activation):
    """One promotion or demotion of a custom capability.

    `action` reuses the activation words: a promotion is `activate` and a demotion is `deactivate`,
    which is what tells this apart from an ordinary capability activation.
    """

    kind: Literal["capability_promoted"] = "capability_promoted"


ActivationEvent = Annotated[
    CapabilityActivated | StepActivated | AgentActivated | ProfileActivated | CapabilityPromoted,
    Field(discriminator="kind"),
]
"""One line of the activation log, told apart by its `kind` field."""


class CustomStep(BaseModel):
    """Manifest entry for one custom step module."""

    name: str
    module_file: str
    function_names: list[str] = []
    status: Literal["active", "disabled"] = "disabled"
    last_error: str | None = None


class CustomAgent(BaseModel):
    """Manifest entry for one custom agent document."""

    name: str
    document_file: str
    model: str | None = None
    """The document's own `model`, kept for display. `None` when it names none, and the host's own model wins either way."""
    status: Literal["active", "disabled"] = "disabled"
    last_error: str | None = None


class ProfileDoc(BaseModel):
    """One profile document's declared set, as written.

    A list left out means "leave that item type alone at selection". An explicit empty list means
    "reconcile that item type to nothing active". `workflows` is a pinned display list, named here
    and shown in the library, never enforced against it.
    """

    model_config = ConfigDict(extra="forbid")

    description: str = ""
    instructions: str | None = None
    plugins: list[str] = []
    capabilities: list[str] | None = None
    agents: list[str] | None = None
    scripts: list[str] | None = None
    skills: list[str] = []
    workflows: list[str] = []


class CustomProfile(BaseModel):
    """Manifest entry for one custom profile."""

    name: str
    document_file: str
    plugins: list[str] = []
    status: Literal["active", "disabled"] = "disabled"
    last_error: str | None = None
