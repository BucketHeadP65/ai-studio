"""An owner's `harness activate` takes effect on the very next chat turn, never before.

Never the run that authored the capability, and never a later run absent an explicit activate,
however many turns pass. This is the vendor `capability_creation` README's own orchestrator
contract (`capabilities=creation.store.load_active()`, read fresh per `agent.run()`), proven
here through the studio's real chat path (`harness.features.authoring.controller`)
rather than against the store in isolation: what a person can actually observe is a tool
appearing or disappearing from what the model was offered on a given turn.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, cast

import pytest

from core.ai.workflows.contracts import StepRegistry
from pydantic_ai import Agent
from pydantic_ai.messages import ModelResponse, TextPart

from harness.features.selfauthoring.infrastructure.capabilities import FileCustomCapabilities
from harness.session import NoSessionLog, Runtime
from tests.support.selfauthoring import FakeActivationLog
from tests.support.serving import a_served_client
from tests.support.state import a_state
from tests.support.streaming import streaming_function_model, submit

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Sequence
    from pathlib import Path

    import httpx

    from pydantic_ai.messages import ModelMessage
    from pydantic_ai.models.function import AgentInfo

    Harness = tuple[httpx.AsyncClient, FileCustomCapabilities, list[list[str]], str]

_MARKER_SOURCE = """
from pydantic_ai.capabilities import AbstractCapability
from pydantic_ai.toolsets import FunctionToolset


class Marker(AbstractCapability):
    def get_toolset(self):
        toolset = FunctionToolset()

        def marker_tool() -> str:
            '''Only present when this custom capability is active.'''
            return "marked"

        toolset.add_function(marker_tool, takes_ctx=False)
        return toolset
"""


def _reporting_agent(seen: list[list[str]]) -> Agent[Runtime, str]:
    """An agent whose transcripts the sorted tool names it was actually offered, then answers."""

    def report(messages: Sequence[ModelMessage], info: AgentInfo) -> ModelResponse:
        del messages
        seen.append(sorted(tool.name for tool in info.function_tools))
        return ModelResponse(parts=[TextPart(content="ok")])

    return Agent(streaming_function_model(report), deps_type=Runtime, output_type=str)


async def _turn(client: httpx.AsyncClient, session_id: str, text: str) -> None:
    """One turn through the chat path, asserted to have run rather than been refused."""
    answered = await client.post(f"/api/sessions/{session_id}/messages", json=submit(text))
    assert answered.status_code == 200, answered.text


@pytest.fixture
async def harness(tmp_path: Path) -> AsyncIterator[Harness]:
    store = FileCustomCapabilities(tmp_path / "capabilities", activations=FakeActivationLog())
    seen: list[list[str]] = []
    session = Runtime(registry=StepRegistry(), library=cast("Any", None), agent=_reporting_agent(seen), custom_capabilities=store, session_log=NoSessionLog(), state=a_state(tmp_path))
    async with a_served_client(state=tmp_path, answering={Runtime: session}) as client:
        opened = await client.post("/api/sessions", json={})
        assert opened.status_code == 200
        yield client, store, seen, str(opened.json()["session"]["id"])


async def test_authored_is_not_loaded_the_run_that_authored_it(
    harness: Harness,
) -> None:
    client, store, seen, session_id = harness
    store.write("marker", _MARKER_SOURCE)

    await _turn(client, session_id, "hi")

    assert "marker_tool" not in seen[-1], "written pending -- not loaded the run it was authored in"


async def test_authored_stays_unloaded_on_a_later_run_absent_activation(
    harness: Harness,
) -> None:
    client, store, seen, session_id = harness
    store.write("marker", _MARKER_SOURCE)
    await _turn(client, session_id, "hi")

    await _turn(client, session_id, "still pending?")

    assert "marker_tool" not in seen[-1], "pending has no expiry -- only an explicit activate loads it"


async def test_activation_loads_it_on_the_very_next_run(
    harness: Harness,
) -> None:
    client, store, seen, session_id = harness
    store.write("marker", _MARKER_SOURCE)
    await _turn(client, session_id, "hi")

    assert store.activate("marker") is True
    await _turn(client, session_id, "now?")

    assert "marker_tool" in seen[-1]


async def test_disabling_it_unloads_it_on_the_next_run(
    harness: Harness,
) -> None:
    client, store, seen, session_id = harness
    store.write("marker", _MARKER_SOURCE)
    store.activate("marker")
    await _turn(client, session_id, "hi")
    assert "marker_tool" in seen[-1], "sanity: it was live before disabling"

    assert store.deactivate("marker") is True
    await _turn(client, session_id, "bye")

    assert "marker_tool" not in seen[-1]
