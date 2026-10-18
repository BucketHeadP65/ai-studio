"""The catalog against the real gateway: skipped without a gateway key in the owner's settings file, never mocked.

Three claims per run:

- Every catalog model answers a plain-prose turn, which is the chat entries' mode.
- The DeepSeek route round-trips thinking through a tool call and a second turn, which is what a
  worker does on every delegation. The API requires the thinking parts sent back on every
  subsequent request while tools are in play, and the profile is what does that.
- A real `delegate_task` runs a `quick` worker through the delegate plugin and the parent relays it.
"""

from __future__ import annotations

import os

from decimal import Decimal
from typing import TYPE_CHECKING, cast

import pytest

from core.settings import InfrastructureSettings, read_infrastructure
from pydantic_ai import Agent
from pydantic_ai.settings import ModelSettings
from pydantic_ai_harness.spend import Budget, SpendLimits

from harness.adapters.contracts import chat_workspace
from harness.adapters.models import CATALOG, ModelFactory, model_for
from harness.features.authoring.workers import delegation_plugins
from harness.session import Runtime
from harness.settings import ENGRAM_SETTINGS, WORKSPACE, HarnessSettings
from tests.support.state import a_state
from tests.support.tiers import tiers_under

if TYPE_CHECKING:
    from pathlib import Path

_ROUTES = HarnessSettings().models
"""The routes and the response ceiling this deployment declares, the same ones the app builds with."""


@pytest.fixture
def infrastructure() -> InfrastructureSettings:
    """This deployment's own infrastructure section: these cases talk to the real gateway on purpose."""
    try:
        settings = read_infrastructure(ENGRAM_SETTINGS)
    except (OSError, ValueError) as exc:
        pytest.skip(f"the gateway smoke needs the owner's settings file: {exc}")
    if not settings.gateway.api_key:
        pytest.skip("the gateway smoke needs infrastructure.gateway.api_key in the owner's settings file")
    return settings


@pytest.mark.parametrize("model_id", list(CATALOG))
async def test_each_catalog_model_answers_in_plain_prose(model_id: str, infrastructure: InfrastructureSettings) -> None:
    agent = Agent(model_for(model_id, infrastructure=infrastructure, models=_ROUTES), instructions="Answer in one short sentence.")

    result = await agent.run("Say hello and name the model you are.")

    assert isinstance(result.output, str)
    assert result.output


async def test_the_deepseek_route_round_trips_thinking_through_a_tool_call(infrastructure: InfrastructureSettings) -> None:
    agent = Agent(
        model_for("deepseek-v4-flash-0731", infrastructure=infrastructure, models=_ROUTES),
        model_settings=ModelSettings(thinking="high"),
        instructions="Use the tool when asked for the secret word. Answer in one short sentence.",
    )

    @agent.tool_plain
    def secret_word() -> str:
        """The secret word, only reachable through this tool."""
        return "marmalade"

    first = await agent.run("Call the tool and tell me the secret word.")
    assert "marmalade" in first.output.lower()

    second = await agent.run("Now say the secret word again, in uppercase.", message_history=first.all_messages())

    assert "MARMALADE" in second.output


async def test_a_real_delegation_runs_a_quick_worker_through_the_delegate_door(tmp_path: Path, infrastructure: InfrastructureSettings) -> None:
    spend = SpendLimits[Runtime](budgets=[Budget(usd=Decimal(1), window="run")])
    delegate, _ = delegation_plugins(tiers=tiers_under(tmp_path, build=ModelFactory(infrastructure, _ROUTES)), reads=None, spend=spend, state=a_state(tmp_path))
    parent = Agent(
        model_for("deepseek-v4-flash-0731", infrastructure=infrastructure, models=_ROUTES),
        deps_type=Runtime,
        instructions="Delegate the task to the worker on the quick tier and relay its answer in one short sentence.",
        # A delegate runs in its parent's workspace, as under the chat agent.
        capabilities=[chat_workspace(WORKSPACE, environ=os.environ), delegate],
    )

    result = await parent.run(
        "Ask the worker to read harness/README.md in the checkout and report the exact text of its first line.", deps=cast("Runtime", None)
    )

    assert "harness" in result.output.lower()
