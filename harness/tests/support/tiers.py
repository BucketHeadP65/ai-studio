"""The tier resolution a test hands its subject, over a home of its own and a build seam nothing reaches."""

from __future__ import annotations

from typing import TYPE_CHECKING

from pydantic_ai.messages import ModelResponse
from pydantic_ai.models.function import FunctionModel

from harness.adapters.model_list import ModelList
from harness.adapters.model_tiers import ModelBuilder, ModelTiers
from tests.support.state import a_state

if TYPE_CHECKING:
    from pathlib import Path

    from core.thinking import Thinking
    from pydantic_ai.models import Model


def never_built(model_id: str, /, *, thinking: Thinking = "xhigh") -> Model:
    """A build seam a test never reaches: it names its tier, it does not run one."""
    del thinking
    return FunctionModel(lambda _messages, _info: ModelResponse(parts=[]), model_name=model_id)


def answering_on(model: Model) -> ModelBuilder:
    """A build seam that answers every tier with `model` itself: for a test that names no tier of its own."""

    def build(model_id: str, /, *, thinking: Thinking = "xhigh") -> Model:
        del model_id, thinking
        return model

    return build


def tiers_under(home: Path, *, models: ModelList | None = None, build: ModelBuilder | None = None) -> ModelTiers:
    """How a tier name becomes a model in a test: the store under `home`, the pinned catalog, `never_built`.

    `models` is the list a stored setting is checked against, `build` how an id becomes a model.
    """
    return ModelTiers(
        state=a_state(home),
        models=models if models is not None else ModelList(listing=None),
        build=build if build is not None else never_built,
    )
