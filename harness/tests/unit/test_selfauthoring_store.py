"""`FileCustomCapabilities`: a custom capability lands disabled, never active, until it is.

The vendor `capability_creation` store activates a cleanly-validated capability the moment it
is written; pending is the only landing state here instead, so `write()` always forces the
manifest entry disabled and only `activate()`/`deactivate()` flip it, each attested.
`FileCustomCapabilities` is a subclass-only diff over the vendor store, so every test that
asserts the studio's new behavior also asserts, by constructing the vendor base class directly,
that the vendor's own behavior is untouched.
"""

from __future__ import annotations

import re

from typing import TYPE_CHECKING

import pytest

from pydantic_ai_harness.capability_creation import CapabilityStore

from harness.features.selfauthoring.infrastructure.activation_log import NoActivations
from harness.features.selfauthoring.infrastructure.capabilities import FileCustomCapabilities
from harness.features.selfauthoring.tools import PENDING_GUIDANCE, PendingCapabilityCreation
from tests.support.selfauthoring import FakeActivationLog

if TYPE_CHECKING:
    from pathlib import Path

_VALID_SOURCE = """
from pydantic_ai.capabilities import AbstractCapability


class Marker(AbstractCapability):
    '''A capability with no overrides -- valid, and does nothing.'''
"""

_BROKEN_SOURCE = "this is not python at all !!!"


def _store(tmp_path: Path) -> tuple[FileCustomCapabilities, FakeActivationLog]:
    activations = FakeActivationLog()
    return FileCustomCapabilities(tmp_path / "capabilities", activations=activations), activations


def test_a_cleanly_validated_write_lands_disabled(tmp_path: Path) -> None:
    store, _activations = _store(tmp_path)

    record = store.write("marker", _VALID_SOURCE)

    assert record.status == "disabled", "pending is the only landing state, even for valid code"
    assert record.last_error is None
    [listed] = store.list_all()
    assert listed.status == "disabled"


def test_a_write_that_fails_validation_also_lands_disabled(tmp_path: Path) -> None:
    store, _activations = _store(tmp_path)

    record = store.write("broken", _BROKEN_SOURCE)

    assert record.status == "disabled"
    assert record.last_error is not None


def test_reauthoring_an_active_capability_attests_the_deactivation(tmp_path: Path) -> None:
    """Overwriting an ACTIVE record is itself a deactivation -- the reviewed code is gone."""
    store, activations = _store(tmp_path)
    store.write("marker", _VALID_SOURCE)
    store.activate("marker")
    activations.events.clear()

    store.write("marker", _VALID_SOURCE)

    assert [(event.name, event.action, event.via, event.kind) for event in activations.events] == [
        ("marker", "deactivate", "agent", "capability_activated")
    ]


def test_reauthoring_a_pending_capability_attests_nothing(tmp_path: Path) -> None:
    store, activations = _store(tmp_path)
    store.write("marker", _VALID_SOURCE)
    activations.events.clear()

    store.write("marker", _VALID_SOURCE)

    assert activations.events == []


def test_activating_a_capability_with_a_stored_error_is_refused_naming_it(tmp_path: Path) -> None:
    store, activations = _store(tmp_path)
    record = store.write("broken", _BROKEN_SOURCE)
    assert record.last_error is not None

    with pytest.raises(ValueError, match=re.escape(record.last_error)):
        store.activate("broken")

    assert activations.events == [], "the refusal must not attest anything -- nothing changed"
    [listed] = store.list_all()
    assert listed.status == "disabled"


def test_the_vendor_store_activates_on_write_unchanged(tmp_path: Path) -> None:
    """The subclass-only diff: constructing the vendor base class directly still activates."""
    vendor_store = CapabilityStore(tmp_path / "capabilities")

    record = vendor_store.write("marker", _VALID_SOURCE)

    assert record.status == "active", "the vendor default is untouched by the studio subclass"


def test_a_disabled_authored_capability_is_not_loaded(tmp_path: Path) -> None:
    store, _activations = _store(tmp_path)
    store.write("marker", _VALID_SOURCE)

    assert store.load_active() == [], "written but never activated -- not loaded this run nor any later one"


def test_activation_loads_it_and_is_attested(tmp_path: Path) -> None:
    store, activations = _store(tmp_path)
    store.write("marker", _VALID_SOURCE)

    assert store.activate("marker") is True

    [instance] = store.load_active()
    assert type(instance).__name__ == "Marker"
    assert [(event.name, event.action, event.via) for event in activations.events] == [("marker", "activate", "cli")]


def test_activating_an_unknown_name_is_reported_and_not_attested(tmp_path: Path) -> None:
    store, activations = _store(tmp_path)

    assert store.activate("nobody") is False
    assert activations.events == [], "nothing happened, so nothing is attested"


def test_deactivation_stops_loading_and_is_attested(tmp_path: Path) -> None:
    store, activations = _store(tmp_path)
    store.write("marker", _VALID_SOURCE)
    store.activate("marker")
    activations.events.clear()

    assert store.deactivate("marker") is True

    assert store.load_active() == []
    assert [(event.name, event.action, event.via) for event in activations.events] == [("marker", "deactivate", "cli")]


def test_the_agents_own_disable_tool_also_attests_but_from_its_own_door(tmp_path: Path) -> None:
    """Every deactivation is attested, whichever entry it came through.

    `disable_custom_capability` (the model's own tool) calls the inherited `disable()`, not
    `deactivate()`.
    """
    store, activations = _store(tmp_path)
    store.write("marker", _VALID_SOURCE)
    store.activate("marker")
    activations.events.clear()

    assert store.disable("marker") is True

    assert store.load_active() == []
    assert [(event.name, event.action, event.via) for event in activations.events] == [("marker", "deactivate", "agent")]


def test_disabling_an_unknown_name_attests_nothing(tmp_path: Path) -> None:
    store, activations = _store(tmp_path)

    assert store.disable("nobody") is False
    assert activations.events == []


def test_the_default_activation_log_is_used_when_none_is_given(tmp_path: Path) -> None:
    """No fake injected: the store still works, writing through its own default `ActivationLog`."""
    store = FileCustomCapabilities(tmp_path / "capabilities", activations=FakeActivationLog())
    store.write("marker", _VALID_SOURCE)

    assert store.activate("marker") is True
    [instance] = store.load_active()
    assert type(instance).__name__ == "Marker"


def test_pending_guidance_states_the_true_contract_not_the_vendor_default(tmp_path: Path) -> None:
    """`guidance=PENDING_GUIDANCE` must not repeat the vendor default's false claim.

    The vendor default says authoring makes a capability active on the next run; here it stays
    pending, and the guidance must say so.
    """
    creation = PendingCapabilityCreation[object](directory=tmp_path / "capabilities", guidance=PENDING_GUIDANCE, activations=NoActivations())

    instructions = creation.get_instructions()

    assert isinstance(instructions, str), "PENDING_GUIDANCE is a plain string, not a template or a hook"
    assert "become active on the next agent run" not in instructions, "the vendor default's false claim must not survive"
    assert "stays disabled until the owner runs `harness activate capability <name>`" in instructions
    assert "list_custom_capabilities" in instructions, "the agent's source of truth for real status"


def test_pending_guidance_mentions_promotion_is_owner_run() -> None:
    """`write_custom_capability`'s guidance may mention promotion exists -- but not that the agent can do it."""
    assert "harness promote capability <name>" in PENDING_GUIDANCE
    assert "Promotion is the owner's act. You cannot do it yourself." in PENDING_GUIDANCE
# --- delete() -------------------------------------------------------------------------------


def test_delete_removes_a_disabled_capability_its_file_and_attests(tmp_path: Path) -> None:
    store, activations = _store(tmp_path)
    store.write("marker", _VALID_SOURCE)

    deleted = store.delete("marker")

    assert deleted
    assert store.list_all() == []
    assert not (tmp_path / "capabilities" / "marker.py").exists()
    assert (activations.events[-1].name, activations.events[-1].action) == ("marker", "delete")


def test_delete_refuses_an_active_capability_naming_the_fix(tmp_path: Path) -> None:
    store, _activations = _store(tmp_path)
    store.write("marker", _VALID_SOURCE)
    store.activate("marker")

    with pytest.raises(ValueError, match="disable"):
        store.delete("marker")


def test_delete_of_an_unknown_name_is_false(tmp_path: Path) -> None:
    store, _activations = _store(tmp_path)

    assert not store.delete("ghost")
