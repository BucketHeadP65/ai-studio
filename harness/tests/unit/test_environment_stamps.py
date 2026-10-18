"""`environment_stamps(custom)`: what this run contributes to every fingerprint it writes or checks.

`promoted_capabilities` is the mutable half of promotion `custom_capability_types` cannot see on
its own: re-authoring and re-promoting the SAME name is a legitimate, reviewed act that still
changes what runs, so a digest over every promoted capability's name and source bytes has to move
when that happens -- `drift()` already reports any environment stamp change, no `WorkflowFingerprint` schema
change needed.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from harness.adapters.model_list import ModelList
from harness.adapters.model_tiers import ModelTiers, TierSetting, clear_tier, environment_stamps, set_tier
from harness.features.selfauthoring.infrastructure.capabilities import FileCustomCapabilities
from harness.features.selfauthoring.infrastructure.promotion import promote_capability
from tests.support.fakes.model_listing import FakeModelListing
from tests.support.selfauthoring import FakeActivationLog
from tests.support.state import a_state
from tests.support.tiers import never_built

if TYPE_CHECKING:
    from pathlib import Path


_SOURCE = """
from dataclasses import dataclass

from pydantic_ai.capabilities import AbstractCapability


@dataclass
class Marker(AbstractCapability):
    '''Meets the promotion contract.'''
"""

_CHANGED_SOURCE = """
from dataclasses import dataclass

from pydantic_ai.capabilities import AbstractCapability


@dataclass
class Marker(AbstractCapability):
    '''The same class, but the source bytes differ -- that is the whole point of this fixture.'''
"""


@pytest.fixture
def custom(tmp_path: Path) -> Path:
    """The reviewed custom-item root this test's stamps are read from, empty until a case writes into it."""
    return tmp_path / "custom"


@pytest.fixture
def tiers(tmp_path: Path) -> ModelTiers:
    """The tier resolution under this test's own home, its list the pinned catalog alone."""
    return ModelTiers(state=a_state(tmp_path), models=ModelList(listing=FakeModelListing()), build=never_built)


def _custom_capabilities(tmp_path: Path) -> FileCustomCapabilities:
    return FileCustomCapabilities(tmp_path / "custom" / "capabilities", activations=FakeActivationLog())


def test_no_model_id_is_stamped(custom: Path) -> None:
    """The environment is the steps, tools and capabilities a workflow needs, never the orchestrator's model."""
    assert "model" not in environment_stamps(custom)


def test_a_set_orchestrator_tier_leaves_the_stamp_where_it_was(custom: Path, tiers: ModelTiers) -> None:
    before = environment_stamps(custom)
    set_tier(tiers, "orchestrator", TierSetting(model="gpt-5.4", thinking="low"))

    assert environment_stamps(custom) == before

    clear_tier(tiers, "orchestrator")

    assert environment_stamps(custom) == before


def test_a_set_worker_tier_leaves_the_stamp_where_it_was(custom: Path, tiers: ModelTiers) -> None:
    before = environment_stamps(custom)
    set_tier(tiers, "worker", TierSetting(model="gpt-5.4", thinking="low"))

    assert environment_stamps(custom) == before


def test_no_promoted_capabilities_is_a_stable_stamp(custom: Path) -> None:
    first = environment_stamps(custom)["promoted_capabilities"]
    second = environment_stamps(custom)["promoted_capabilities"]

    assert first == second


def test_promoting_a_capability_changes_the_stamp(custom: Path, tmp_path: Path) -> None:
    before = environment_stamps(custom)["promoted_capabilities"]
    store = _custom_capabilities(tmp_path)
    store.write("marker", _SOURCE)
    store.activate("marker")
    promote_capability(store, "marker")

    after = environment_stamps(custom)["promoted_capabilities"]

    assert after != before


def test_reauthoring_and_repromoting_the_same_name_changes_the_stamp(custom: Path, tmp_path: Path) -> None:
    """Re-author, re-activate, and re-promote the SAME name.

    The stamp must move even though the promoted NAME never changed, because the SOURCE did.
    """
    store = _custom_capabilities(tmp_path)
    store.write("marker", _SOURCE)
    store.activate("marker")
    promote_capability(store, "marker")
    original = environment_stamps(custom)["promoted_capabilities"]

    store.write("marker", _CHANGED_SOURCE)
    store.activate("marker")
    promote_capability(store, "marker")

    changed = environment_stamps(custom)["promoted_capabilities"]
    assert changed != original


def test_demoting_a_capability_changes_the_stamp_back(custom: Path, tmp_path: Path) -> None:
    store = _custom_capabilities(tmp_path)
    before = environment_stamps(custom)["promoted_capabilities"]
    store.write("marker", _SOURCE)
    store.activate("marker")
    promote_capability(store, "marker")

    store.deactivate("marker")

    assert environment_stamps(custom)["promoted_capabilities"] == before
