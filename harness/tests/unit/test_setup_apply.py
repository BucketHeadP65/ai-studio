"""`_apply_setup`: the checkout's boot-time profile and orchestrator tier, each applied only when unset."""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from harness.adapters.model_list import ModelList
from harness.adapters.model_tiers import DEFAULT_TIERS, ModelTiers, TierSetting, set_tier, stored_tiers, tier_in_effect
from harness.features.sessions.project_config import SetupConfig
from harness.main import _apply_setup
from tests.support.fakes.model_listing import FakeModelListing
from tests.support.state import a_state
from tests.support.tiers import never_built

if TYPE_CHECKING:
    from pathlib import Path


def _apply_setup_with(tiers: ModelTiers, home: Path, setup: SetupConfig) -> None:
    """`_apply_setup` against `tiers`, with the reviewed custom root under `home`."""
    _apply_setup(setup, tiers, home / "custom")


@pytest.fixture
def tiers(tmp_path: Path) -> ModelTiers:
    """Tier resolution under this test's own home, its list the pinned catalog alone."""
    return ModelTiers(state=a_state(tmp_path), models=ModelList(listing=FakeModelListing()), build=never_built)


class TestOrchestratorTier:
    def test_an_orchestrator_already_set_is_left_alone(self, tiers: ModelTiers, tmp_path: Path) -> None:
        set_tier(tiers, "orchestrator", TierSetting(model="gpt-5.4", thinking="low"))

        _apply_setup_with(tiers, tmp_path, SetupConfig(model="deepseek-v4-flash-0731"))

        assert tier_in_effect(tiers, "orchestrator").setting == TierSetting(model="gpt-5.4", thinking="low")

    def test_nothing_set_takes_the_setups_model_at_the_default_thinking(self, tiers: ModelTiers, tmp_path: Path) -> None:
        assert "orchestrator" not in stored_tiers(tiers)

        _apply_setup_with(tiers, tmp_path, SetupConfig(model="deepseek-v4-flash-0731"))

        assert tier_in_effect(tiers, "orchestrator").setting == TierSetting(
            model="deepseek-v4-flash-0731", thinking=DEFAULT_TIERS["orchestrator"].thinking
        )

    def test_an_unusable_configured_model_warns_and_leaves_the_default_standing(self, tiers: ModelTiers, tmp_path: Path) -> None:
        _apply_setup_with(tiers, tmp_path, SetupConfig(model="not-a-real-model"))

        assert tier_in_effect(tiers, "orchestrator").default
