"""A chat turn that runs out of budget is something the panel is told -- never a 500.

The turn is capped at `CHAT_BUDGET` model requests. A model that never stops calling tools crosses
that cap mid-turn, and what the person must see is a sentence saying the turn did not finish, on
the stream's own error chunk, rather than a stream that dies half-written. The session is given
back with it, so the conversation carries on from where it stopped.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from core.ai.workflows.contracts import StepRegistry
from pydantic_ai import Agent
from pydantic_ai.messages import ModelResponse, ToolCallPart
from pydantic_ai.toolsets import FunctionToolset

from harness.session import Runtime, SessionPool
from tests.support.authoring import a_chat, a_turn, one_session
from tests.support.runtime import a_runtime
from tests.support.streaming import streaming_function_model, submit

if TYPE_CHECKING:
    from collections.abc import Sequence

    from pydantic_ai.messages import ModelMessage
    from pydantic_ai.models.function import AgentInfo


def _insatiable_agent() -> Agent[Runtime, str]:
    """An agent whose model calls a tool on every request, forever -- the budget must stop it."""
    toolset = FunctionToolset[Runtime]()

    def poke() -> str:
        return "again"

    toolset.add_function(poke, takes_ctx=False)

    def always_poke(messages: Sequence[ModelMessage], info: AgentInfo) -> ModelResponse:
        del messages, info
        return ModelResponse(parts=[ToolCallPart(tool_name="poke", args={})])

    return Agent(streaming_function_model(always_poke), deps_type=Runtime, output_type=str, toolsets=[toolset])


async def test_a_starved_turn_is_a_sentence_not_a_dead_stream() -> None:
    """About the answer, so the service is driven directly rather than through a client."""
    sessions = SessionPool(a_runtime(agent=_insatiable_agent(), registry=StepRegistry()))
    session_id = one_session(sessions)

    chunks = await a_turn(a_chat(sessions), session_id, submit("loop forever"))

    errors = [chunk for chunk in chunks if chunk.get("type") == "error"]
    assert len(errors) == 1, "exhaustion is news for the panel, never a stream that just stops"
    assert errors[0]["errorText"].strip(), "the bubble is given a sentence, not an empty error"
    assert not any(chunk.get("type") == "data-settle" for chunk in chunks), "a turn that never finished settles nothing"
    assert not sessions.is_answering(session_id), "the starved turn gives the session back; the conversation carries on"
