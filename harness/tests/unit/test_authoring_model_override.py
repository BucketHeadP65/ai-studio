"""The orchestrator tier reaches the chat agent's model, not just the store.

A turn runs the agent on `TierModel("orchestrator", tiers)`, read fresh per request, so `set_tier` takes
effect on the very next turn with no restart and `clear_tier` puts the tier's default back -- proven here by
a build seam that labels every model it is asked for, rather than the real gateway.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from pydantic_ai import Agent
from pydantic_ai.messages import ModelResponse, TextPart
from pydantic_ai.models.function import FunctionModel

from harness.adapters.model_list import ModelList
from harness.adapters.model_tiers import ModelTiers, TierModel, TierSetting, clear_tier, set_tier
from harness.session import Runtime, SessionPool
from tests.support.authoring import a_chat, a_turn, one_session
from tests.support.fakes.model_listing import FakeModelListing
from tests.support.runtime import a_runtime
from tests.support.state import a_state
from tests.support.streaming import submit

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Sequence
    from pathlib import Path

    from core.thinking import Thinking
    from pydantic_ai.messages import ModelMessage
    from pydantic_ai.models import Model
    from pydantic_ai.models.function import AgentInfo

    from harness.features.authoring.service import ChatService


def _canned_model(text: str) -> FunctionModel:
    """A model whose every reply is `text`, plain prose -- streamed or not."""

    def function(messages: Sequence[ModelMessage], info: AgentInfo) -> ModelResponse:
        del messages, info
        return ModelResponse(parts=[TextPart(text)])

    async def stream_function(messages: Sequence[ModelMessage], info: AgentInfo) -> AsyncIterator[str]:
        del messages, info
        for i in range(0, len(text), 6):
            yield text[i : i + 6]

    return FunctionModel(function=function, stream_function=stream_function)


def _labelled(model_id: str, *, thinking: Thinking) -> Model:
    """The `build` seam every tier here resolves through: one distinguishable model per id."""
    del thinking
    return _canned_model(f"answered by {model_id}")


@pytest.fixture
def tiers(tmp_path: Path) -> ModelTiers:
    """Tier resolution under this test's own home, its list the pinned catalog alone, its models labelled."""
    return ModelTiers(state=a_state(tmp_path), models=ModelList(listing=FakeModelListing()), build=_labelled)


@pytest.fixture
def sessions(tiers: ModelTiers) -> SessionPool:
    """One session whose agent resolves the orchestrator tier through the labelling seam."""
    return SessionPool(a_runtime(agent=Agent(TierModel("orchestrator", tiers), deps_type=Runtime)))


@pytest.fixture
def chat(sessions: SessionPool) -> ChatService:
    """The turn itself, driven on the service rather than through a client.

    Every case here is about the answer a turn settles, never about the wire that carried it.
    """
    return a_chat(sessions)


@pytest.fixture
def session_id(sessions: SessionPool) -> str:
    """The session every turn here answers into."""
    return one_session(sessions)


async def _settled_message(chat: ChatService, session_id: str) -> str:
    """What one turn settled onto the panel, read off the chunk that carries it."""
    chunks = await a_turn(chat, session_id, submit("hi"))
    settles = [chunk for chunk in chunks if chunk.get("type") == "data-settle"]
    assert len(settles) == 1, "the turn's settled payload rides exactly one data-settle chunk"
    return str(settles[0]["data"]["message"])


async def test_nothing_set_reaches_the_tiers_default_model(chat: ChatService, session_id: str) -> None:
    assert await _settled_message(chat, session_id) == "answered by glm-5.3-flash"


async def test_setting_the_tier_reaches_the_very_next_turn(chat: ChatService, session_id: str, tiers: ModelTiers) -> None:
    set_tier(tiers, "orchestrator", TierSetting(model="gpt-5.4", thinking="low"))

    assert await _settled_message(chat, session_id) == "answered by gpt-5.4"


async def test_switching_the_tier_reaches_the_turn_right_after_the_switch(chat: ChatService, session_id: str, tiers: ModelTiers) -> None:
    set_tier(tiers, "orchestrator", TierSetting(model="gpt-5.4", thinking="low"))
    set_tier(tiers, "orchestrator", TierSetting(model="deepseek-v4-flash-0731", thinking="high"))

    assert await _settled_message(chat, session_id) == "answered by deepseek-v4-flash-0731"


async def test_clearing_the_tier_restores_its_default(chat: ChatService, session_id: str, tiers: ModelTiers) -> None:
    set_tier(tiers, "orchestrator", TierSetting(model="gpt-5.4", thinking="low"))

    clear_tier(tiers, "orchestrator")

    assert await _settled_message(chat, session_id) == "answered by glm-5.3-flash"
