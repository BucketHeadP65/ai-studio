"""The chat path: what it answers, what it refuses, and what it no longer serves."""

from __future__ import annotations

import json

from typing import TYPE_CHECKING

import pytest

from pydantic_ai import Agent
from pydantic_ai.messages import ModelResponse, TextPart
from pydantic_ai_harness.step_persistence import InMemoryStepStore, StepPersistence

from harness.features.authoring.service import EMPTY_TURN
from harness.session import Runtime, SessionPool
from tests.support.runtime import a_runtime
from tests.support.serving import a_served_client
from tests.support.state import a_session_log, a_state
from tests.support.streaming import streaming_function_model, submit

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Sequence
    from pathlib import Path

    import httpx

    from pydantic_ai.messages import ModelMessage
    from pydantic_ai.models.function import AgentInfo

GONE = ("/api/chat", "/api/chat/stream")


def _kinds(body: str) -> list[str]:
    """Every chunk type the streamed body carried, in order."""
    lines = [line.removeprefix("data: ") for line in body.splitlines() if line.startswith("data: ")]
    return [json.loads(line)["type"] for line in lines if line != "[DONE]"]


@pytest.fixture
def prompts() -> list[str]:
    """Every prompt the model was given, in order."""
    return []


@pytest.fixture
async def served(tmp_path: Path, prompts: list[str]) -> AsyncIterator[tuple[httpx.AsyncClient, SessionPool]]:
    """The served application, its session over a model that records what it was asked."""

    def function(messages: Sequence[ModelMessage], info: AgentInfo) -> ModelResponse:
        del info
        prompts.extend(str(part.content) for message in messages for part in message.parts if part.part_kind == "user-prompt")
        return ModelResponse(parts=[TextPart("ok")])

    store = InMemoryStepStore()
    agent: Agent[Runtime, str] = Agent(
        streaming_function_model(function),
        deps_type=Runtime,
        capabilities=[StepPersistence(store=store, agent_name="harness-author")],
    )
    pool = SessionPool(a_runtime(agent=agent, store=store, session_log=a_session_log(tmp_path), state=a_state(tmp_path)))
    async with a_served_client(state=tmp_path, answering={Runtime: pool.default, SessionPool: pool}) as client:
        yield client, pool


@pytest.fixture
async def client(served: tuple[httpx.AsyncClient, SessionPool]) -> httpx.AsyncClient:
    """The client alone, for a case that never reaches for the pool behind it."""
    return served[0]


@pytest.fixture
async def session_id(client: httpx.AsyncClient) -> str:
    """A session this process holds, opened the way the studio opens one."""
    opened = await client.post("/api/sessions", json={})
    assert opened.status_code == 200
    return str(opened.json()["session"]["id"])


async def test_a_turn_streams_the_protocol_the_studio_reads(client: httpx.AsyncClient, session_id: str) -> None:
    """The answer is the Vercel AI data stream, with this app's own step and settle chunks in it."""
    answered = await client.post(f"/api/sessions/{session_id}/messages", json=submit("hello"))

    assert answered.status_code == 200
    assert answered.headers["content-type"].startswith("text/event-stream")
    kinds = _kinds(answered.text)
    assert kinds[0] == "start"
    assert "text-delta" in kinds
    assert "data-step" in kinds
    assert "data-settle" in kinds


async def test_the_page_context_rides_the_body(client: httpx.AsyncClient, session_id: str, prompts: list[str]) -> None:
    """The rail's page context reaches the prompt, on the same body as the message."""
    context = {"url": "/runs/r-1", "page": "run-inspector", "ids": {"run": "r-1"}, "tab": "", "trail": [], "extras": {}}

    await client.post(f"/api/sessions/{session_id}/messages", json=submit("what failed?", context=context))

    assert "run-inspector" in prompts[-1]
    assert prompts[-1].endswith("what failed?")


async def test_an_unknown_session_is_a_404(client: httpx.AsyncClient) -> None:
    """A session nobody opened is answered, never streamed at."""
    answered = await client.post("/api/sessions/00000000-0000-4000-8000-000000000000/messages", json=submit("hello"))

    assert answered.status_code == 404


async def test_a_busy_session_is_refused_with_a_sentence(served: tuple[httpx.AsyncClient, SessionPool], session_id: str) -> None:
    """One conversation, one turn: a second message into a session mid-turn is a 409 that says why."""
    client, pool = served
    hold = await pool.begin_turn(session_id)
    try:
        refused = await client.post(f"/api/sessions/{session_id}/messages", json=submit("again"))
    finally:
        hold.release()

    assert refused.status_code == 409
    assert "already streaming" in refused.json()["detail"]


async def test_a_body_with_nothing_to_say_and_nothing_to_answer_is_a_422(client: httpx.AsyncClient, session_id: str) -> None:
    """Neither words nor answers is a request that is wrong, not a session that is busy or missing."""
    answered = await client.post(f"/api/sessions/{session_id}/messages", json=submit(""))

    assert answered.status_code == 422
    assert answered.json()["detail"] == EMPTY_TURN


async def test_the_routes_it_replaced_are_gone(client: httpx.AsyncClient, session_id: str) -> None:
    """One chat path. The two unscoped entries and the old scoped stream answer nothing."""
    for path in (*GONE, f"/api/sessions/{session_id}/chat/stream"):
        answered = await client.post(path, json={"message": "hello"})
        assert answered.status_code == 404, path


if __name__ == "__main__":  # pragma: no cover
    pytest.main([__file__])
