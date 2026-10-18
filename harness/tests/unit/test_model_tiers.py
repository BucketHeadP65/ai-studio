"""The tier store: three roles, each a model and a thinking level, read fresh per request, standing aside when stale."""

from __future__ import annotations

import json

from dataclasses import replace
from typing import TYPE_CHECKING

import pytest

from pydantic_ai import Agent
from pydantic_ai.messages import ModelMessage, ModelResponse, TextPart
from pydantic_ai.models.function import AgentInfo, FunctionModel

from harness.adapters import model_list, model_tiers
from harness.adapters.model_tiers import DEFAULT_TIERS, ModelTiers, TierModel, TierSetting
from tests.support.fakes.model_listing import FakeModelListing
from tests.support.staging import staged_names
from tests.support.state import a_state

if TYPE_CHECKING:
    from pathlib import Path

    from core.thinking import Thinking
    from pydantic_ai.models import Model


@pytest.fixture
def tiers(tmp_path: Path) -> ModelTiers:
    """The tier resolution under this test's own home: one remote id beside the pinned catalog."""
    listed = model_list.ModelList(listing=FakeModelListing(entries=[_entry("openai/gpt-5.4-mini")]))
    return ModelTiers(state=a_state(tmp_path), models=listed, build=_Factory())


def _entry(model_id: str) -> model_list.ModelEntry:
    return model_list.ModelEntry(
        id=model_id,
        name=model_id,
        route="openrouter",
        context_length=64_000,
        price_in_per_m=None,
        price_out_per_m=None,
        reasoning="effort",
        pinned=False,
    )


def _answering(label: str) -> FunctionModel:
    def respond(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        del messages, info
        return ModelResponse(parts=[TextPart(f"answered by {label}")])

    return FunctionModel(respond, model_name=label)


class _Factory:
    """A `build` seam that records what it was asked for and answers a labelled model."""

    def __init__(self) -> None:
        self.asked: list[tuple[str, Thinking]] = []

    def __call__(self, model_id: str, *, thinking: Thinking) -> Model:
        self.asked.append((model_id, thinking))
        return _answering(f"{model_id}@{thinking}")


def test_every_tier_defaults_to_the_default_model_at_the_top_of_the_scale(tiers: ModelTiers, tmp_path: Path) -> None:
    assert set(DEFAULT_TIERS) == {"orchestrator", "worker", "helper"}
    for tier in model_tiers.TIERS:
        effect = model_tiers.tier_in_effect(tiers, tier)
        assert effect.setting == TierSetting(model="glm-5.3-flash", thinking="xhigh")
        assert effect.default


def test_a_set_tier_is_in_effect_on_the_next_read(tiers: ModelTiers, tmp_path: Path) -> None:
    model_tiers.set_tier(tiers, "worker", TierSetting(model="deepseek-v4-flash-0731", thinking="low"))

    effect = model_tiers.tier_in_effect(tiers, "worker")

    assert effect.setting.model == "deepseek-v4-flash-0731"
    assert effect.setting.thinking == "low"
    assert not effect.default
    assert model_tiers.tier_in_effect(tiers, "orchestrator").default


async def test_a_remote_id_may_be_set_once_the_list_carries_it(tiers: ModelTiers, tmp_path: Path) -> None:
    await tiers.models.warm()

    model_tiers.set_tier(tiers, "helper", TierSetting(model="openai/gpt-5.4-mini", thinking="medium"))

    assert model_tiers.tier_in_effect(tiers, "helper").setting.model == "openai/gpt-5.4-mini"


def test_an_unlisted_id_is_refused_and_nothing_changes(tiers: ModelTiers, tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="nobody/nothing"):
        model_tiers.set_tier(tiers, "helper", TierSetting(model="nobody/nothing", thinking="high"))

    assert model_tiers.tier_in_effect(tiers, "helper").default


def test_a_stored_entry_naming_a_model_the_list_no_longer_offers_stands_aside(tiers: ModelTiers, tmp_path: Path) -> None:
    model_tiers.tiers_path(tiers.state).write_text(json.dumps({"worker": {"model": "gone/model", "thinking": "high"}}), encoding="utf-8")

    effect = model_tiers.tier_in_effect(tiers, "worker")

    assert effect.default
    assert effect.setting == DEFAULT_TIERS["worker"]


def test_a_malformed_file_stands_aside_for_every_default(tiers: ModelTiers, tmp_path: Path) -> None:
    model_tiers.tiers_path(tiers.state).write_text("{not json", encoding="utf-8")

    assert all(effect.default for effect in model_tiers.tiers_in_effect(tiers).values())


def test_clearing_a_tier_restores_its_default(tiers: ModelTiers, tmp_path: Path) -> None:
    model_tiers.set_tier(tiers, "worker", TierSetting(model="gpt-5.4", thinking="low"))

    model_tiers.clear_tier(tiers, "worker")

    assert model_tiers.tier_in_effect(tiers, "worker").default


def test_the_tier_model_builds_what_the_store_says_and_rebuilds_only_on_change(tiers: ModelTiers, tmp_path: Path) -> None:
    factory = _Factory()
    tiered = TierModel("worker", replace(tiers, build=factory))

    first = tiered.wrapped
    again = tiered.wrapped
    model_tiers.set_tier(tiers, "worker", TierSetting(model="gpt-5.4", thinking="low"))
    changed = tiered.wrapped

    assert first is again
    assert changed is not first
    assert factory.asked == [("glm-5.3-flash", "xhigh"), ("gpt-5.4", "low")]


async def test_an_agent_on_a_tier_model_follows_a_change_between_two_runs(tiers: ModelTiers, tmp_path: Path) -> None:
    agent = Agent(TierModel("orchestrator", tiers))

    before = await agent.run("hello")
    model_tiers.set_tier(tiers, "orchestrator", TierSetting(model="gpt-5.4", thinking="medium"))
    after = await agent.run("hello")

    assert before.output == "answered by glm-5.3-flash@xhigh"
    assert after.output == "answered by gpt-5.4@medium"


def test_no_tier_reaches_the_environment_stamp(tiers: ModelTiers, tmp_path: Path) -> None:
    """A tier says which model runs, never what a workflow needs, so neither tier moves the stamp."""
    before = model_tiers.environment_stamps(tmp_path / "custom")
    model_tiers.set_tier(tiers, "worker", TierSetting(model="gpt-5.4", thinking="low"))
    model_tiers.set_tier(tiers, "orchestrator", TierSetting(model="deepseek-v4-pro-0813", thinking="high"))

    assert model_tiers.environment_stamps(tmp_path / "custom") == before


async def test_the_context_window_follows_the_tiers_model(tiers: ModelTiers, tmp_path: Path) -> None:
    await tiers.models.warm()
    assert model_tiers.tier_context_window(tiers, "orchestrator") == 1_310_720

    model_tiers.set_tier(tiers, "orchestrator", TierSetting(model="openai/gpt-5.4-mini", thinking="high"))

    assert model_tiers.tier_context_window(tiers, "orchestrator") == 64_000


def test_setting_a_tier_stages_the_file_under_a_hidden_temp_name(tiers: ModelTiers, monkeypatch: pytest.MonkeyPatch) -> None:
    """A listing or a glob that skips dotfiles never sees the tier file still being written."""
    staged = staged_names(monkeypatch)

    model_tiers.set_tier(tiers, "worker", TierSetting(model="deepseek-v4-flash-0731", thinking="low"))

    assert staged, "the tier file is swapped into place"
    assert all(name.startswith(".") for name in staged)
