"""Which conversation a turn's runs belong to, and which run a settled turn stamps.

One record holds every session in this deployment, so both facts are the conversation id the turn
ran under: a project session's runs answer under its own id and under no other, and a settled turn
names the run it wrote while another session is writing one of its own.
"""

from __future__ import annotations

import asyncio
import json

from contextlib import asynccontextmanager
from typing import TYPE_CHECKING

import pytest

from pydantic_ai import Agent
from pydantic_ai.messages import ModelRequest, ModelResponse, TextPart, ToolCallPart, ToolReturnPart, UserPromptPart
from pydantic_ai.models.function import FunctionModel
from pydantic_ai_harness.step_persistence import ContinuableSnapshot, InMemoryStepStore, RunRecord, StepPersistence

from harness.adapters.record import RecordHistorySource
from harness.domain.events import session_spans
from harness.session import Runtime, SessionConversationSearch, SessionPool
from harness.workspace import open_workspace
from tests.support.runtime import a_runtime
from tests.support.serving import a_served_client
from tests.support.state import a_session_log, a_state
from tests.support.streaming import submit

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator, AsyncIterator, Sequence
    from pathlib import Path

    import httpx

    from pydantic_ai.messages import ModelMessage
    from pydantic_ai.models.function import AgentInfo
    from pydantic_ai_harness.step_persistence import StepStore

    from harness.domain.ports import SessionTranscript


class _Gate:
    """A streamed answer waiting half-written until `expected` turns are all inside the model."""

    def __init__(self, *, expected: int = 1, released: bool = False) -> None:
        self.arrived = asyncio.Event()
        self.release = asyncio.Event()
        if released:
            self.release.set()
        self.waiting = 0
        self._expected = expected

    async def stream(self, messages: Sequence[ModelMessage], info: AgentInfo) -> AsyncIterator[str]:
        """Half the reply, then a wait, then the rest."""
        del messages, info
        text = "ok"
        yield text[:1]
        self.waiting += 1
        if self.waiting >= self._expected:
            self.arrived.set()
        await self.release.wait()
        yield text[1:]


def _answering_agent(gate: _Gate, store: StepStore) -> Agent[Runtime, str]:
    """The studio's own step-persistence mount over a model that waits in `gate`."""

    def function(messages: Sequence[ModelMessage], info: AgentInfo) -> ModelResponse:
        del messages, info
        return ModelResponse(parts=[TextPart("ok")])

    return Agent(
        FunctionModel(function=function, stream_function=gate.stream),
        deps_type=Runtime,
        capabilities=[StepPersistence(store=store, agent_name="harness-author")],
    )


def _searching_agent(store: SessionTranscript) -> tuple[Agent[Runtime, str], list[str]]:
    """The studio's own recall mount over a model that searches once, plus what it was answered."""
    answers: list[str] = []

    def function(messages: Sequence[ModelMessage], info: AgentInfo) -> ModelResponse:
        del info
        answers.extend(
            part.content
            for message in messages
            for part in message.parts
            if isinstance(part, ToolReturnPart) and isinstance(part.content, str)
        )
        if answers:
            return ModelResponse(parts=[TextPart("read it")])
        return ModelResponse(parts=[ToolCallPart("search_conversation_history", {"query": "secret"})])

    agent: Agent[Runtime, str] = Agent(
        FunctionModel(function=function),
        deps_type=Runtime,
        output_type=str,
        capabilities=[SessionConversationSearch(RecordHistorySource(store, conversation_id=None), scope="all")],
    )
    return agent, answers


@asynccontextmanager
async def _studio(agent: Agent[Runtime, str], store: SessionTranscript, home: Path) -> AsyncGenerator[tuple[httpx.AsyncClient, SessionPool]]:
    """The real app over ASGI, its session built on `agent` and its record on `store`.

    An async client rather than `serving`'s `TestClient`, because one case drives two turns at once
    and a synchronous client cannot. The application is entered, which is what mounts its
    controllers, and the container answers the pool this test built, so a route resolves the very
    pool the assertions read.
    """
    pool = SessionPool(a_runtime(agent=agent, store=store, session_log=a_session_log(home), state=a_state(home)))
    async with a_served_client(state=home, answering={Runtime: pool.default, SessionPool: pool}) as client:
        yield client, pool


async def _new_session(client: httpx.AsyncClient) -> str:
    created = await client.post("/api/sessions", json={})
    assert created.status_code == 200
    session_id = created.json()["session"]["id"]
    assert isinstance(session_id, str)
    return session_id


def _settled(body: str) -> bool:
    """Whether this streamed body carried the turn's settled payload."""
    chunks = [line.removeprefix("data: ") for line in body.splitlines() if line.startswith("data: ")]
    return any(json.loads(chunk).get("type") == "data-settle" for chunk in chunks if chunk != "[DONE]")


async def _turn(client: httpx.AsyncClient, session_id: str, message: str) -> None:
    """One streamed turn in `session_id`, asserted to have settled."""
    answered = await client.post(f"/api/sessions/{session_id}/messages", json=submit(message))
    assert answered.status_code == 200
    assert _settled(answered.text), "the turn settled"


async def _seed_snapshot(store: StepStore, *, run_id: str, conversation_id: str, said: str) -> None:
    """One readable run of one conversation: what a recall search has to find, or fail to."""
    await store.register_run(RunRecord(run_id=run_id, conversation_id=conversation_id))
    await store.save_snapshot(
        ContinuableSnapshot(
            run_id=run_id,
            step_index=0,
            messages=[ModelRequest(parts=[UserPromptPart(content=said)])],
            conversation_id=conversation_id,
        )
    )


def _run_ids_by_session(pool: SessionPool) -> dict[str, tuple[str, ...]]:
    """Every run the checkout's timeline credits to each of its sessions."""
    return {span.session_id: span.run_ids for span in session_spans(pool.default.session_log.events())}


def _asked(runtime: Runtime) -> list[str]:
    """Every user prompt in this runtime's restored conversation."""
    return [
        part.content
        for message in runtime.conversation.history
        for part in message.parts
        if isinstance(part, UserPromptPart) and isinstance(part.content, str)
    ]


async def test_a_project_sessions_runs_answer_under_its_own_conversation_only(tmp_path: Path) -> None:
    """A pooled session's runs belong to its own conversation, never to another's."""
    home = tmp_path / "client-repo"
    home.mkdir()
    store = InMemoryStepStore()

    async with _studio(_answering_agent(_Gate(released=True), store), store, tmp_path) as (client, pool):
        runtime = await pool.create(home)
        assert runtime.session_id is not None
        checkout_session = await _new_session(client)

        await _turn(client, runtime.session_id, "hello")

        mine = await store.list_runs(conversation_id=runtime.session_id)
        assert [record.conversation_id for record in mine] == [runtime.session_id]
        assert await store.list_runs(conversation_id=checkout_session) == [], (
            "nothing about a project session answers under another conversation"
        )


async def test_a_settled_turn_is_readable_again_out_of_the_record(tmp_path: Path) -> None:
    """The run a turn wrote hydrates its session back: the write and the read are one record."""
    home = tmp_path / "client-repo"
    home.mkdir()
    store = InMemoryStepStore()

    async with _studio(_answering_agent(_Gate(released=True), store), store, tmp_path) as (client, pool):
        runtime = await pool.create(home)
        assert runtime.session_id is not None
        await _turn(client, runtime.session_id, "remember this")

        pool.evict_idle(older_than_minutes=0)
        rebuilt = await pool.get(runtime.session_id)

        assert rebuilt is not runtime, "the evicted runtime is rebuilt from the record it wrote"
        assert "remember this" in _asked(rebuilt), "a session hydrates from the runs its own turns wrote"


async def test_recall_searches_the_asking_sessions_own_conversation(tmp_path: Path) -> None:
    """The search corpus is the asking session's conversation. Another session's runs are unreachable."""
    home = tmp_path / "client-repo"
    home.mkdir()
    store = InMemoryStepStore()
    agent, answers = _searching_agent(store)
    await _seed_snapshot(store, run_id="r-checkout", conversation_id="a-checkout-session", said="the checkout keeps a secret pineapple")
    await _seed_snapshot(store, run_id="r-project", conversation_id="a-project-session", said="the project keeps a secret aubergine")
    runtime = a_runtime(agent=agent, store=store, session_id="a-project-session", workspace=open_workspace(home))

    await agent.run("look it up", deps=runtime, conversation_id=runtime.session_id)

    assert answers, "the model called the search tool"
    assert "aubergine" in answers[0]
    assert "pineapple" not in answers[0], "one session's recall must not reach into another's history"


async def test_two_turns_answering_at_once_stamp_their_own_runs(tmp_path: Path) -> None:
    """Two sessions in one record: each settled turn names the run it wrote, not the newer one."""
    gate = _Gate(expected=2)
    store = InMemoryStepStore()

    async with _studio(_answering_agent(gate, store), store, tmp_path) as (client, pool):
        first, second = await _new_session(client), await _new_session(client)
        turns = [
            asyncio.create_task(client.post(f"/api/sessions/{session_id}/messages", json=submit("hi")))
            for session_id in (first, second)
        ]
        await asyncio.wait_for(gate.arrived.wait(), timeout=5)
        gate.release.set()
        answered = await asyncio.gather(*turns)

        assert [response.status_code for response in answered] == [200, 200]
        assert all(_settled(response.text) for response in answered)
        credited = _run_ids_by_session(pool)
        assert len(credited[first]) == 1
        assert len(credited[second]) == 1
        assert credited[first] != credited[second], "a settled turn must not stamp the run another session just started"
        for session_id, run_ids in ((first, credited[first]), (second, credited[second])):
            record = await store.get_run(run_id=run_ids[0])
            assert record is not None
            assert record.conversation_id == session_id, "the stamped run is the one this session's own turn wrote"


if __name__ == "__main__":  # pragma: no cover
    pytest.main([__file__])
