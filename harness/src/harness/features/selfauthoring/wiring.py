"""What the self-authoring slice registers: where owner-reviewed custom items live, and what the owner's surface reads."""

from __future__ import annotations

from pathlib import Path

from wireup import injectable

from harness.adapters.contracts import ModelTiers
from harness.features.selfauthoring.controller import SelfAuthoringDeployment
from harness.settings import RunSettings, SelfAuthoringSettings, StateSettings, custom_root


@injectable
def custom_items_root(settings: SelfAuthoringSettings) -> Path:
    """Where owner-reviewed custom items live: the settings-declared root, or this checkout's own.

    Args:
        settings: The self-authoring settings section.

    Returns:
        The custom root.
    """
    return custom_root(settings)


@injectable
def self_authoring_deployment(
    state: StateSettings, self_authoring: SelfAuthoringSettings, run: RunSettings, tiers: ModelTiers
) -> SelfAuthoringDeployment:
    """The deployment facts the owner's own surface reads, as the one thing a route here declares.

    Args:
        state: Where the plugin switches and the profile selection live.
        self_authoring: Where owner-reviewed custom items live.
        run: Which engine runs a workflow.
        tiers: How a tier name becomes a model here.

    Returns:
        The four sections together.
    """
    return SelfAuthoringDeployment(state=state, self_authoring=self_authoring, run=run, tiers=tiers)


REGISTRATIONS: list[object] = [custom_items_root, self_authoring_deployment]
"""Everything this slice puts in the container, each under the type its consumers declare."""
