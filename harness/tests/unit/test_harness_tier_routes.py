"""The tier and model acts of the harness controller: GET/PUT/DELETE tiers, GET models.

The controller is constructed directly and handed the model list it renders against, a `ModelList`
over a scripted listing, so nothing here reaches OpenRouter. The two closed sets a browser meets as
a 422 are pinned as the declarations FastAPI answers off, because a controller route only exists on
the wire once an application's lifespan has run.
"""

from __future__ import annotations

from decimal import Decimal
from typing import TYPE_CHECKING, get_args, get_type_hints

import pytest

from fastapi import HTTPException
from fastapi.routing import APIRoute
from pydantic import ValidationError

from harness.adapters.model_list import ModelEntry, ModelList
from harness.adapters.model_tiers import ModelTiers
from harness.domain.events import TierSelected
from harness.features.selfauthoring.api_models import TierBody
from harness.features.selfauthoring.controller import SelfAuthoringController, SelfAuthoringDeployment
from harness.settings import RunSettings, SelfAuthoringSettings
from tests.support.fakes.model_listing import FakeModelListing
from tests.support.fakes.session_log import FakeSessionLog
from tests.support.fakes.sessions import FakeSessionPool
from tests.support.runtime import a_runtime
from tests.support.state import a_state
from tests.support.tiers import never_built

if TYPE_CHECKING:
    from pathlib import Path


A_REMOTE_MODEL = "openai/gpt-5.4-mini"
"""A model only the scripted listing offers, so a search proves the remote half reaches the answer."""

A_PINNED_MODEL = "gpt-5.4"
ANOTHER_PINNED_MODEL = "deepseek-v4-pro-0813"


def _remote(model_id: str, name: str = "") -> ModelEntry:
    return ModelEntry(
        id=model_id,
        name=name or model_id,
        route="openrouter",
        context_length=128_000,
        price_in_per_m=Decimal("0.5"),
        price_out_per_m=Decimal("1.5"),
        reasoning="effort",
        pinned=False,
    )


@pytest.fixture
async def models() -> ModelList:
    """The pinned catalog plus one warmed-in remote model, over a listing that never opens a socket."""
    listed = ModelList(listing=FakeModelListing(entries=[_remote(A_REMOTE_MODEL, "OpenAI: GPT 5.4 mini")]))
    await listed.warm()
    return listed


@pytest.fixture
def log() -> FakeSessionLog:
    """Where this controller's acts are attested."""
    return FakeSessionLog()


@pytest.fixture
def controller(tmp_path: Path, log: FakeSessionLog, models: ModelList) -> SelfAuthoringController:
    """The controller under test, its custom-item root and its tier store both under `tmp_path`."""
    return SelfAuthoringController(
        SelfAuthoringDeployment(
            state=a_state(tmp_path),
            self_authoring=SelfAuthoringSettings(custom_root=tmp_path / "custom"),
            run=RunSettings(),
            tiers=ModelTiers(state=a_state(tmp_path), models=models, build=never_built),
        ),
        log,
        FakeSessionPool(a_runtime()),
    )


async def test_get_answers_three_tiers_at_their_defaults(controller: SelfAuthoringController, models: ModelList) -> None:
    listing = await controller.get_tiers()

    assert [view.tier for view in listing.tiers] == ["orchestrator", "worker", "helper"]
    assert all(view.default and view.thinking == "xhigh" and view.model.id == "glm-5.3-flash" for view in listing.tiers)
    assert listing.tiers[0].model.pinned is True
    assert listing.tiers[0].role


async def test_put_sets_one_tier_and_the_others_stand(controller: SelfAuthoringController, models: ModelList) -> None:
    listing = await controller.put_tier("worker", TierBody(model=A_PINNED_MODEL, thinking="low"))

    by_name = {view.tier: view for view in listing.tiers}
    assert by_name["worker"].model.id == A_PINNED_MODEL
    assert by_name["worker"].thinking == "low"
    assert not by_name["worker"].default
    assert by_name["orchestrator"].default


async def test_put_with_an_unlisted_id_is_a_400_and_changes_nothing(controller: SelfAuthoringController, models: ModelList) -> None:
    with pytest.raises(HTTPException) as refused:
        await controller.put_tier("helper", TierBody(model="nobody/nothing", thinking="high"))

    assert refused.value.status_code == 400
    assert "nobody/nothing" in str(refused.value.detail)
    assert (await controller.get_tiers()).tiers[2].default


async def test_delete_puts_the_default_back(controller: SelfAuthoringController, models: ModelList) -> None:
    await controller.put_tier("worker", TierBody(model=A_PINNED_MODEL, thinking="low"))

    listing = await controller.delete_tier("worker")

    assert next(view for view in listing.tiers if view.tier == "worker").default


async def test_put_and_delete_are_recorded_as_tier_selections(
    controller: SelfAuthoringController, models: ModelList, log: FakeSessionLog
) -> None:
    await controller.put_tier("worker", TierBody(model=A_PINNED_MODEL, thinking="low"))
    await controller.delete_tier("worker")

    selections = [(event.tier, event.model, event.thinking) for event in log.recorded if isinstance(event, TierSelected)]
    assert selections == [("worker", A_PINNED_MODEL, "low"), ("worker", None, None)]


async def test_a_second_put_replaces_the_first_on_the_same_tier(controller: SelfAuthoringController, models: ModelList) -> None:
    await controller.put_tier("worker", TierBody(model=A_PINNED_MODEL, thinking="low"))

    listing = await controller.put_tier("worker", TierBody(model=ANOTHER_PINNED_MODEL, thinking="high"))

    worker = next(view for view in listing.tiers if view.tier == "worker")
    assert (worker.model.id, worker.thinking) == (ANOTHER_PINNED_MODEL, "high")


async def test_models_answers_the_search_pinned_first(controller: SelfAuthoringController, models: ModelList) -> None:
    listing = await controller.get_models(models, q="gpt")

    assert [entry.id for entry in listing.models] == ["gpt-5.4", "gpt-6-luna", A_REMOTE_MODEL]


async def test_models_with_no_query_answers_everything_up_to_the_limit(controller: SelfAuthoringController, models: ModelList) -> None:
    listing = await controller.get_models(models)

    assert len(listing.models) == 6


class TestTheClosedSetsTheRouteRefusesOn:
    """A browser meets these two as a 422; what is pinned here is the declaration FastAPI answers off."""

    def test_a_thinking_level_off_the_scale_is_not_a_body(self) -> None:
        with pytest.raises(ValidationError):
            TierBody.model_validate({"model": A_PINNED_MODEL, "thinking": "max"})

    def test_the_tier_path_parameter_is_the_three_named_tiers(self) -> None:
        declared = get_type_hints(SelfAuthoringController.put_tier)["tier"]

        assert get_args(declared) == ("orchestrator", "worker", "helper")


def test_the_tier_routes_are_declared_on_the_router() -> None:
    declared = {route.path for route in SelfAuthoringController.router.routes if isinstance(route, APIRoute)}

    assert {"/api/harness/tiers", "/api/harness/tiers/{tier}", "/api/harness/models"} <= declared
