"""Test doubles for the self-authoring slice: a recording `ActivationLog`, and the deployment a surface reads.

A fake rather than a mock: it satisfies `ActivationLog` structurally and exposes what it
recorded as a plain public attribute, so a test asserts on state rather than on a call spy.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from harness.adapters.model_list import ModelList
from harness.adapters.model_tiers import ModelTiers
from harness.features.selfauthoring.controller import SelfAuthoringDeployment
from harness.settings import RunSettings, SelfAuthoringSettings
from tests.support.state import a_state
from tests.support.tiers import never_built

if TYPE_CHECKING:
    from pathlib import Path

    from harness.features.selfauthoring.domain import ActivationEvent


def a_deployment(home: Path, *, models: ModelList | None = None) -> SelfAuthoringDeployment:
    """What the owner's surface reads, all of it under `home`: state, custom root, engine, tiers.

    `models` is the list a tier setting is checked against; `None` offers the pinned catalog alone.
    No tier here is ever asked for a model, so the build seam answers a function model.
    """
    state = a_state(home)
    listed = models if models is not None else ModelList(listing=None)
    return SelfAuthoringDeployment(
        state=state,
        self_authoring=SelfAuthoringSettings(custom_root=home / "custom"),
        run=RunSettings(),
        tiers=ModelTiers(state=state, models=listed, build=never_built),
    )


@dataclass
class FakeActivationLog:
    """Records every activation act in memory, in the order `record` was called."""

    events: list[ActivationEvent] = field(default_factory=list)

    def record(self, event: ActivationEvent) -> ActivationEvent:
        self.events.append(event)
        return event
