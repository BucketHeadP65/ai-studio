"""Three named model tiers, each a model and a thinking level the owner sets, read fresh on every request.

`orchestrator` runs the chat agent and the deep delegations. `worker` runs workflow steps by default and the standard
delegations. `helper` runs the quick delegations. The owner's entries live in one file under the state home, written
atomically and read per request, so a change reaches the next turn and the next step with no restart. An entry naming
a model the list no longer offers gives way to the tier's default rather than blocking a turn.
"""

from __future__ import annotations

import json

from dataclasses import dataclass
from typing import TYPE_CHECKING, Protocol

from core.ai.workflows.contracts import Tier
from core.files import atomic_write
from core.thinking import Thinking
from pydantic import BaseModel, TypeAdapter, ValidationError
from pydantic_ai.models import Model
from pydantic_ai.models.wrapper import WrapperModel

from harness.adapters.models import DEFAULT_THINKING, MODEL_ID, context_window_for

if TYPE_CHECKING:
    from pathlib import Path

    from harness.adapters.model_list import ModelList
    from harness.settings import StateSettings

TIERS: tuple[Tier, ...] = ("orchestrator", "worker", "helper")
"""The tiers in listing order, the order AI Factory Studio shows them in."""

TIER_ROLES: dict[Tier, str] = {
    "orchestrator": "the chat agent, deep delegations, and steps that name it",
    "worker": "workflow steps by default, and standard delegations",
    "helper": "quick delegations, and steps that name it",
}
"""What runs on each tier, in the words AI Factory Studio's tier card shows."""


class TierSetting(BaseModel, frozen=True):
    """One tier's model id and thinking level."""

    model: str
    thinking: Thinking


DEFAULT_TIERS: dict[Tier, TierSetting] = {tier: TierSetting(model=MODEL_ID, thinking=DEFAULT_THINKING) for tier in TIERS}
"""What a tier runs on when the owner has set nothing."""

_STORED = TypeAdapter(dict[Tier, TierSetting])


class ModelBuilder(Protocol):
    """How a model id at a thinking level becomes a model. `model_for` satisfies it."""

    def __call__(self, model_id: str, /, *, thinking: Thinking) -> Model:
        """Build the model `model_id` names at `thinking`.

        Args:
            model_id: A catalog id, or any id the gateway lists.
            thinking: The thinking level the model is built at.

        Returns:
            The model.

        Raises:
            ValueError: The route `model_id` needs has no key in the credentials.
        """
        ...


@dataclass(frozen=True)
class TierInEffect:
    """What a tier runs on now, and whether that is its default rather than the owner's choice."""

    tier: Tier
    setting: TierSetting
    default: bool


@dataclass(frozen=True)
class ModelTiers:
    """How a tier name becomes a model in this deployment."""

    state: StateSettings
    """Where the owner's tier file lives."""
    models: ModelList
    """Every model this deployment can reach: what a stored entry is checked against."""
    build: ModelBuilder
    """How an id at a thinking level becomes a model."""


def tiers_path(state: StateSettings) -> Path:
    """The owner's tier file under `state`'s home.

    Args:
        state: The state settings.

    Returns:
        The file's path. The file may not exist yet.
    """
    return state.model_tiers


def stored_tiers(tiers: ModelTiers) -> dict[Tier, TierSetting]:
    """The owner's stored entries, exactly as written, including any whose model is no longer listed.

    Args:
        tiers: The deployment's tiers.

    Returns:
        The entries by tier, empty when the file is missing, unreadable or malformed.
    """
    try:
        return _STORED.validate_json(tiers_path(tiers.state).read_text(encoding="utf-8"))
    except (OSError, ValueError, ValidationError):
        return {}


def _write(tiers: ModelTiers, stored: dict[Tier, TierSetting]) -> None:
    """Persist the whole store, creating its home, so a concurrent reader never sees half a file."""
    path = tiers_path(tiers.state)
    path.parent.mkdir(parents=True, exist_ok=True)
    atomic_write(path, json.dumps({name: entry.model_dump() for name, entry in stored.items()}, indent=2))


def set_tier(tiers: ModelTiers, tier: Tier, setting: TierSetting) -> None:
    """Persist `setting` for `tier`.

    Args:
        tiers: The deployment's tiers.
        tier: The tier to set.
        setting: The model and thinking level the tier runs on from the next request.

    Raises:
        ValueError: `setting.model` is not on the model list.
    """
    if not tiers.models.is_listed(setting.model):
        msg = f"{setting.model!r} is not on this deployment's model list. Search GET /api/harness/models for one that is."
        raise ValueError(msg)
    stored = stored_tiers(tiers)
    stored[tier] = setting
    _write(tiers, stored)


def clear_tier(tiers: ModelTiers, tier: Tier) -> None:
    """Drop the owner's entry for `tier`, so its default stands again.

    Args:
        tiers: The deployment's tiers.
        tier: The tier to clear.
    """
    stored = stored_tiers(tiers)
    if stored.pop(tier, None) is not None:
        _write(tiers, stored)


def tier_in_effect(tiers: ModelTiers, tier: Tier) -> TierInEffect:
    """What `tier` runs on now: the owner's entry when its model is still listed, else the default.

    Args:
        tiers: The deployment's tiers.
        tier: The tier to read.

    Returns:
        The setting in effect, and whether it is the default.
    """
    stored = stored_tiers(tiers).get(tier)
    if stored is not None and tiers.models.is_listed(stored.model):
        return TierInEffect(tier=tier, setting=stored, default=False)
    return TierInEffect(tier=tier, setting=DEFAULT_TIERS[tier], default=True)


def tiers_in_effect(tiers: ModelTiers) -> dict[Tier, TierInEffect]:
    """What every tier runs on now.

    Args:
        tiers: The deployment's tiers.

    Returns:
        All three, in listing order.
    """
    return {tier: tier_in_effect(tiers, tier) for tier in TIERS}


def tier_context_window(tiers: ModelTiers, tier: Tier) -> int | None:
    """How many tokens the model `tier` runs on accepts.

    Args:
        tiers: The deployment's tiers.
        tier: The tier to read.

    Returns:
        The catalog's figure, else the model list's, else `None` when neither knows.
    """
    model_id = tier_in_effect(tiers, tier).setting.model
    if (window := context_window_for(model_id)) is not None:
        return window
    entry = tiers.models.entry(model_id)
    return entry.context_length if entry is not None else None


class TierModel(WrapperModel):
    """A model that is whatever its tier runs on at the moment of each request.

    Built once and mounted like any model. Every read of `wrapped` reads the tier file, and the underlying model is
    rebuilt only when the tier's setting changed since the last read.
    """

    def __init__(self, tier: Tier, tiers: ModelTiers) -> None:
        """Hold the tier. No model is built until one is first needed.

        Args:
            tier: The tier this model follows.
            tiers: How the tier's setting is read and its model built.
        """
        Model.__init__(self)
        self.tier: Tier = tier
        self._tiers = tiers
        self._built_for: TierSetting | None = None
        self._built: Model | None = None

    def __getattr__(self, item: str):
        # `wrapped` is never set, so every read of it lands here and answers the tier's model now. A private
        # name is never forwarded, which keeps a half-built instance from recursing.
        if item.startswith("_"):
            raise AttributeError(item)
        current = self._current()
        return current if item == "wrapped" else getattr(current, item)

    def __repr__(self) -> str:
        """The tier and the setting its model was last built for. Naming a tier never builds a model."""
        return f"{type(self).__name__}(tier={self.tier!r}, built_for={self._built_for!r})"

    def _current(self) -> Model:
        """The tier's model now, rebuilt only when the tier's setting changed since the last read."""
        setting = tier_in_effect(self._tiers, self.tier).setting
        built = self._built
        if built is None or setting != self._built_for:
            built = self._tiers.build(setting.model, thinking=setting.thinking)
            self._built = built
            self._built_for = setting
        return built


def environment_stamps(custom: Path) -> dict[str, str]:
    """The stamps this deployment adds to every fingerprint it writes or checks.

    Every fingerprint writer and checker must take its stamps from here. A second spelling would turn a fresh workflow
    stale, or hide a stale one. The orchestrator's model is not stamped. `promoted_capabilities` is a digest over every
    promoted capability's name and current source, so `drift()` reports a promoted capability whose source changed
    under the same name like any other change.

    Args:
        custom: The root of the reviewed custom items.

    Returns:
        The stamps by name.
    """
    # An upward import the layering test sanctions, kept lazy so importing this adapter never imports a slice.
    from harness.features.selfauthoring.contracts import (  # noqa: PLC0415 (sanctioned upward edge)
        FileCustomCapabilities,
        promoted_capabilities_digest,
    )

    capabilities = FileCustomCapabilities.read_only(custom / "capabilities")
    return {"promoted_capabilities": promoted_capabilities_digest(capabilities)}
