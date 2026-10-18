"""The use cases over custom items: the custom items of one item type, an item's summary, and the profile selection."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Literal, Protocol, cast, overload

from pydantic_ai_harness.capability_creation import AuthoredCapability

from harness import plugins
from harness.features.selfauthoring.domain import CUSTOM_ITEM_DIRECTORIES, CustomAgent, CustomProfile
from harness.features.selfauthoring.exceptions import SelectionError
from harness.features.selfauthoring.infrastructure.activation_log import activations_under
from harness.features.selfauthoring.infrastructure.agents import FileCustomAgents
from harness.features.selfauthoring.infrastructure.capabilities import FileCustomCapabilities
from harness.features.selfauthoring.infrastructure.profiles import FileCustomProfiles
from harness.features.selfauthoring.infrastructure.steps import FileCustomSteps

if TYPE_CHECKING:
    from collections.abc import Callable, Sequence
    from pathlib import Path

    from harness.features.selfauthoring.domain import CustomItemType, CustomStep, ProfileDoc
    from harness.features.selfauthoring.internal_contracts import ActivationLog, CustomItems
    from harness.settings import StateSettings


@overload
def custom_items_for(item_type: Literal["capability"], root: Path, *, activations: ActivationLog) -> FileCustomCapabilities: ...
@overload
def custom_items_for(item_type: Literal["step"], root: Path, *, activations: ActivationLog) -> FileCustomSteps: ...
@overload
def custom_items_for(item_type: Literal["agent"], root: Path, *, activations: ActivationLog) -> FileCustomAgents: ...
@overload
def custom_items_for(item_type: Literal["profile"], root: Path, *, activations: ActivationLog) -> FileCustomProfiles: ...
def custom_items_for(item_type: CustomItemType, root: Path, *, activations: ActivationLog) -> CustomItems:
    """The custom items of `item_type`, in their folder under `root`, recording through `activations`.

    `item_type` is the closed set, so this never refuses. `custom_items_named` is the way in for an
    unchecked string.

    Args:
        item_type: Which custom items.
        root: The custom root.
        activations: Where every act on them is recorded.

    Returns:
        The custom items of that type.
    """
    directory = root / CUSTOM_ITEM_DIRECTORIES[item_type]
    if item_type == "step":
        return FileCustomSteps(directory, activations=activations)
    if item_type == "agent":
        return FileCustomAgents(directory, activations=activations)
    if item_type == "profile":
        return FileCustomProfiles(directory, activations=activations)
    return FileCustomCapabilities(directory, activations=activations)


def custom_items_named(item_type: str, root: Path, *, activations: ActivationLog) -> CustomItems | None:
    """`custom_items_for` over an unchecked string, or `None` when it names no custom-item type.

    The CLI takes the item type as a bare string and refuses an unknown one by name, listing the four.

    Args:
        item_type: The item type as the CLI received it.
        root: The custom root.
        activations: Where every act on them is recorded.

    Returns:
        The custom items of that type, or `None`.
    """
    if item_type not in CUSTOM_ITEM_DIRECTORIES:
        return None
    return custom_items_for(cast("CustomItemType", item_type), root, activations=activations)


def descriptor_of(record: AuthoredCapability | CustomStep | CustomAgent | CustomProfile) -> str:
    """The short summary the `harness list` CLI and the listing route show for one custom item.

    Args:
        record: The custom item's manifest entry.

    Returns:
        A capability's class name, an agent's declared model (`(host default model)` when it names
        none), a profile's plugin list, or a step's registered function names (`?` when a broken step
        registered none).
    """
    if isinstance(record, AuthoredCapability):
        return record.class_name
    if isinstance(record, CustomAgent):
        return record.model or "(host default model)"
    if isinstance(record, CustomProfile):
        return f"plugins: {', '.join(record.plugins)}" if record.plugins else "plugins: none"
    return ", ".join(record.function_names) or "?"


def select_profile(name: str | None, *, root: Path, state: StateSettings) -> None:
    """Put one active profile in effect, or clear the selection with `None`.

    The whole profile is validated first. An unknown, pending or unreadable profile, or a named custom
    item that does not exist or is broken, refuses the selection outright, never half-applied. On
    success the custom items are reconciled to exactly the profile's lists, each flip recorded, and its
    plugins, instructions and skills are snapshotted for the runtime, in effect on the very next turn.

    Args:
        name: The profile to put in effect, or `None` to clear the selection.
        root: The custom root.
        state: Where the selection and the activation log live.

    Raises:
        SelectionError: The profile cannot be honored. The message gives the first reason found.
    """
    if name is None:
        plugins.clear_selection(state)
        return
    activations = activations_under(state)
    profiles = FileCustomProfiles(root / CUSTOM_ITEM_DIRECTORIES["profile"], activations=activations)
    record = next((entry for entry in profiles.list_all() if entry.name == name), None)
    if record is None:
        msg = f"no custom profile named {name!r}"
        raise SelectionError(msg)
    if record.status != "active":
        msg = f"profile {name!r} is not active. Activate it first"
        raise SelectionError(msg)
    doc = profiles.resolve_document(name)
    plans = _reconciliation_plans(doc, root, activations)
    for plan in plans:
        for wanted in plan.wanted:
            existing = next((entry for entry in plan.records if entry.name == wanted), None)
            if existing is None:
                msg = f"profile {name!r} names {plan.item_type} {wanted!r}, which does not exist"
                raise SelectionError(msg)
            if existing.last_error is not None:
                msg = f"profile {name!r} names {plan.item_type} {wanted!r}, which is broken: {existing.last_error}"
                raise SelectionError(msg)
    for plan in plans:
        plan.reconcile()
    plugins.apply_selection(
        state, plugins.Selection(name=name, plugins=tuple(doc.plugins), instructions=doc.instructions, skills=tuple(doc.skills))
    )


class _Reviewable(Protocol):
    """The part of a manifest entry reconciliation reads: identity, review status, validity."""

    name: str
    status: Literal["active", "disabled"]
    last_error: str | None


@dataclass
class _Plan:
    """One item type's reconciliation: the entries as they are, and the names that should be active."""

    item_type: str
    records: Sequence[_Reviewable]
    wanted: list[str]
    activate: Callable[[str], bool]
    deactivate: Callable[[str], bool]

    def reconcile(self) -> None:
        wanted = set(self.wanted)
        for record in self.records:
            if record.name in wanted and record.status != "active":
                self.activate(record.name)
            elif record.name not in wanted and record.status == "active":
                self.deactivate(record.name)


def _reconciliation_plans(doc: ProfileDoc, root: Path, activations: ActivationLog) -> list[_Plan]:
    """The reconciliation plan for each item type a selection touches. A list left out plans nothing."""
    plans: list[_Plan] = []
    if doc.capabilities is not None:
        capabilities = FileCustomCapabilities(root / CUSTOM_ITEM_DIRECTORIES["capability"], activations=activations)
        plans.append(
            _Plan("capability", list(capabilities.list_all()), list(doc.capabilities), capabilities.activate, capabilities.deactivate)
        )
    if doc.scripts is not None:
        steps = FileCustomSteps(root / CUSTOM_ITEM_DIRECTORIES["step"], activations=activations)
        plans.append(_Plan("script", list(steps.list_all()), list(doc.scripts), steps.activate, steps.deactivate))
    if doc.agents is not None:
        agents = FileCustomAgents(root / CUSTOM_ITEM_DIRECTORIES["agent"], activations=activations)
        plans.append(_Plan("agent", list(agents.list_all()), list(doc.agents), agents.activate, agents.deactivate))
    return plans


def selected_profile_name(state: StateSettings) -> str | None:
    """The name of the profile in effect.

    Args:
        state: Where the selection lives.

    Returns:
        The profile's name, or `None` when the hand-set plugins govern.
    """
    selection = plugins.current_selection(state)
    return selection.name if selection is not None else None


def selected_profile_voice(state: StateSettings) -> str | None:
    """The selected profile's standing instructions.

    Args:
        state: Where the selection lives.

    Returns:
        The instructions, or `None` when none are in effect.
    """
    selection = plugins.current_selection(state)
    if selection is None or not selection.instructions:
        return None
    return selection.instructions


def selected_profile_skills(state: StateSettings) -> tuple[str, ...] | None:
    """The selected profile's skill names.

    An active profile's own list governs even when it is empty. Only the absence of a selection falls
    back to the chat agent's default catalog.

    Args:
        state: Where the selection lives.

    Returns:
        The skill names, or `None` when no profile is selected.
    """
    selection = plugins.current_selection(state)
    return selection.skills if selection is not None else None
