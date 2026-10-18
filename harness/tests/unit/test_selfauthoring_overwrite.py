"""Overwriting an active custom item deactivates it, whatever its type.

The write records exactly one `deactivate` event from `agent`, and a promoted capability loses its
promotion with it, because its new code is unreviewed.
"""

from __future__ import annotations

import json

from typing import TYPE_CHECKING

import pytest

from harness.features.selfauthoring.domain import (
    AgentActivated,
    CapabilityActivated,
    CapabilityPromoted,
    CustomStep,
    ProfileActivated,
    StepActivated,
)
from harness.features.selfauthoring.infrastructure.activation_log import record_overwrite
from harness.features.selfauthoring.infrastructure.agents import FileCustomAgents
from harness.features.selfauthoring.infrastructure.capabilities import FileCustomCapabilities
from harness.features.selfauthoring.infrastructure.profiles import FileCustomProfiles
from harness.features.selfauthoring.infrastructure.promotion import promote_capability
from harness.features.selfauthoring.infrastructure.steps import FileCustomSteps
from tests.support.selfauthoring import FakeActivationLog

if TYPE_CHECKING:
    from pathlib import Path

_PROMOTABLE_CAPABILITY = """
from dataclasses import dataclass

from pydantic_ai.capabilities import AbstractCapability


@dataclass
class Marker(AbstractCapability):
    '''A declared dataclass with a serialization name, so it can be promoted.'''
"""

_STEP = """
async def double(x: int) -> int:
    return x * 2
"""

_AGENT = "instructions: 'Say whether the quarter looks healthy.'\n"

_PROFILE = "plugins: []\n"

CustomItemsType = type[FileCustomCapabilities | FileCustomSteps | FileCustomAgents | FileCustomProfiles]
EventType = type[CapabilityActivated | StepActivated | AgentActivated | ProfileActivated]


@pytest.mark.parametrize(
    ("items_type", "source", "event_type"),
    [
        pytest.param(FileCustomCapabilities, _PROMOTABLE_CAPABILITY, CapabilityActivated, id="capability"),
        pytest.param(FileCustomSteps, _STEP, StepActivated, id="step"),
        pytest.param(FileCustomAgents, _AGENT, AgentActivated, id="agent"),
        pytest.param(FileCustomProfiles, _PROFILE, ProfileActivated, id="profile"),
    ],
)
def test_overwriting_an_active_item_records_one_deactivation_from_the_agent(
    items_type: CustomItemsType, source: str, event_type: EventType, tmp_path: Path
) -> None:
    activations = FakeActivationLog()
    items = items_type(tmp_path / "custom" / "items", activations=activations)
    first = items.write("marker", source)
    assert first.last_error is None, first.last_error
    assert items.activate("marker")
    if isinstance(items, FileCustomCapabilities):
        promote_capability(items, "marker")
    already_recorded = len(activations.events)

    items.write("marker", source)

    recorded = activations.events[already_recorded:]
    acts = [(type(event), event.action, event.via) for event in recorded if not isinstance(event, CapabilityPromoted)]
    assert acts == [(event_type, "deactivate", "agent")]
    if isinstance(items, FileCustomCapabilities):
        assert [(event.action, event.via) for event in recorded if isinstance(event, CapabilityPromoted)] == [("deactivate", "agent")]
        assert json.loads((items.directory / "promoted.json").read_text(encoding="utf-8"))["names"] == []


@pytest.mark.parametrize(
    "previous",
    [
        pytest.param(None, id="new"),
        pytest.param(CustomStep(name="double", module_file="double.py", status="disabled"), id="disabled"),
    ],
)
def test_an_overwrite_of_a_new_or_disabled_item_records_nothing_and_revokes_nothing(previous: CustomStep | None) -> None:
    activations = FakeActivationLog()
    revoked: list[str] = []

    def revoke(name: str) -> bool:
        revoked.append(name)
        return True

    record_overwrite(previous, "double", event_type=StepActivated, activations=activations, revoke=revoke)

    assert activations.events == []
    assert revoked == []


def test_an_overwrite_of_an_active_item_records_its_deactivation_before_revoking() -> None:
    activations = FakeActivationLog()
    recorded_when_revoked: list[int] = []

    def revoke(name: str) -> bool:
        assert name == "double"
        recorded_when_revoked.append(len(activations.events))
        return True

    previous = CustomStep(name="double", module_file="double.py", status="active")
    record_overwrite(previous, "double", event_type=StepActivated, activations=activations, revoke=revoke)

    assert [(type(event), event.name, event.action, event.via) for event in activations.events] == [
        (StepActivated, "double", "deactivate", "agent")
    ]
    assert recorded_when_revoked == [1]
