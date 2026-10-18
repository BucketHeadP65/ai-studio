"""One turn through the service: the refusals before it starts, and what it leaves behind as it ends."""

from __future__ import annotations

import gc

from typing import TYPE_CHECKING

import pytest

from core.ai.workflows.contracts import StepRegistry
from pydantic_ai import Agent
from pydantic_ai.messages import ModelRequest, ModelResponse, TextPart, ToolCallPart, ToolReturnPart, UserPromptPart
from pydantic_ai.models.function import FunctionModel
from pydantic_ai.tools import DeferredToolRequests, ToolDefinition
from pydantic_ai.toolsets import ExternalToolset
from pydantic_ai_harness.step_persistence import InMemoryStepStore, StepPersistence

from harness.features.authoring.domain import ToolResultIn
from harness.features.authoring.exceptions import PartAnsweredError, SessionUnknownError, TurnEmptyError
from harness.features.authoring.service import CONTINUE_PROMPT, ChatService, ensure_session_id
from harness.session import Runtime, SessionBusyError, SessionPool
from tests.support.authoring import narrates_nowhere
from tests.support.fakes.history import FakeConversationHistory
from tests.support.runtime import EmptyLibrary, a_runtime
from tests.support.state import a_log_under, a_throwaway_state
from tests.support.streaming import streaming_function_model

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Sequence

    from pydantic_ai.messages import ModelMessage
    from pydantic_ai.models.function import AgentInfo

    from harness.features.authoring.internal_contracts import Turn


def _agent(store: InMemoryStepStore, seen: list[int] | None = None) -> Agent[Runtime, str]:
    """An agent that answers one line, writes its run into `store`, and counts what it was given."""

    def function(messages: Sequence[ModelMessage], info: AgentInfo) -> ModelResponse:
        del info
        if seen is not None:
            seen.append(len(messages))
        return ModelResponse(parts=[TextPart("ok")])

    async def stream_function(messages: Sequence[ModelMessage], info: AgentInfo) -> AsyncIterator[str]:
        del info
        if seen is not None:
            seen.append(len(messages))
        yield "ok"

    return Agent(
        FunctionModel(function=function, stream_function=stream_function),
        deps_type=Runtime,
        capabilities=[StepPersistence(store=store, agent_name="harness-author")],
    )


def _pool_over(agent: Agent[Runtime, str], store: InMemoryStepStore) -> SessionPool:
    """A pool whose default session holds `agent` and writes through `store`."""
    return SessionPool(a_runtime(agent=agent, store=store))


def _asked(text: str) -> list[ModelMessage]:
    """One new user message, the shape the wire hands the service."""
    return [ModelRequest(parts=[UserPromptPart(content=text)])]


async def test_an_unknown_session_is_refused_before_anything_streams() -> None:
    """A session nobody opened is a 404 the route can answer, never a stream that fails half-written."""
    store = InMemoryStepStore()
    service = ChatService(pool=_pool_over(_agent(store), store), history=FakeConversationHistory(), refresh=narrates_nowhere)

    with pytest.raises(SessionUnknownError):
        await service.send_message("not-a-session", _asked("hi"))


async def test_a_busy_session_is_refused_before_anything_streams() -> None:
    """Two turns cannot answer into one conversation, and the refusal happens before the stream opens."""
    store = InMemoryStepStore()
    pool = _pool_over(_agent(store), store)
    service = ChatService(pool=pool, history=FakeConversationHistory(), refresh=narrates_nowhere)
    session_id = (await pool.create(None)).session_id or ""
    await pool.begin_turn(session_id)

    with pytest.raises(SessionBusyError):
        await service.send_message(session_id, _asked("hi"))


async def test_a_settled_turn_records_itself_and_gives_the_session_back() -> None:
    """The turn narrates its own start and settle, and the next turn is admitted right after."""
    store = InMemoryStepStore()
    pool = _pool_over(_agent(store), store)
    service = ChatService(pool=pool, history=FakeConversationHistory(), refresh=narrates_nowhere)
    runtime = await pool.create(None)
    session_id = runtime.session_id or ""

    turn = await service.send_message(session_id, _asked("hi"))
    async for _event in turn.events:
        pass

    kinds = [record.event.kind for record in runtime.session_log.events()]
    assert "turn_started" in kinds
    assert "turn_settled" in kinds
    assert await turn.settle() is not None, "a turn that answered settles a payload the panel reads"
    assert not pool.is_answering(session_id), "the session is given back when the stream ends"


async def test_a_turn_nobody_ever_draws_gives_the_session_back() -> None:
    """A turn whose events are never asked for holds no session: the guard rides the events, not the call.

    The response carrying them can be dropped before its body ever runs -- the server drops one it
    answered for a client already gone -- and the session it holds would refuse every later turn.
    """
    store = InMemoryStepStore()
    pool = _pool_over(_agent(store), store)
    service = ChatService(pool=pool, history=FakeConversationHistory(), refresh=narrates_nowhere)
    session_id = (await pool.create(None)).session_id or ""

    turn = await service.send_message(session_id, _asked("hi"))
    assert pool.is_answering(session_id), "the turn holds its session while it is alive"
    del turn
    gc.collect()

    assert not pool.is_answering(session_id), "the dropped turn gave the session back"
    admitted = await service.send_message(session_id, _asked("again"))
    async for _event in admitted.events:
        pass
    assert await admitted.settle() is not None, "the next turn is admitted, and it answers"


async def test_a_turn_is_given_the_records_history_and_not_the_browsers() -> None:
    """What the model is given is the conversation's newest complete step, plus the new message."""
    store = InMemoryStepStore()
    seen: list[int] = []
    pool = _pool_over(_agent(store, seen), store)
    history = FakeConversationHistory()
    service = ChatService(pool=pool, history=history, refresh=narrates_nowhere)
    session_id = (await pool.create(None)).session_id or ""
    history.histories[session_id] = [
        ModelRequest(parts=[UserPromptPart(content="one")]),
        ModelResponse(parts=[TextPart("ok")]),
    ]

    turn = await service.send_message(session_id, _asked("two"))
    async for _event in turn.events:
        pass

    assert history.asked == [session_id]
    assert seen[0] == 3, "the record's two messages, then the new one"


async def test_the_readings_list_fills_as_the_turn_runs() -> None:
    """The step readings the wire drains are the turn's own, tapped off its model."""
    store = InMemoryStepStore()
    pool = _pool_over(_agent(store), store)
    service = ChatService(pool=pool, history=FakeConversationHistory(), refresh=narrates_nowhere)
    session_id = (await pool.create(None)).session_id or ""

    turn = await service.send_message(session_id, _asked("hi"))
    async for _event in turn.events:
        pass

    assert turn.usage, "a finished model response leaves a reading for the wire"


def _browser_agent(store: InMemoryStepStore, seen: list[Sequence[ModelMessage]]) -> Agent[Runtime, str | DeferredToolRequests]:
    """An agent that asks the browser to navigate, then answers once it reads the outcome back."""

    def function(messages: Sequence[ModelMessage], info: AgentInfo) -> ModelResponse:
        del info
        seen.append(list(messages))
        if any(isinstance(part, ToolReturnPart) for message in messages for part in message.parts):
            return ModelResponse(parts=[TextPart("done")])
        return ModelResponse(parts=[ToolCallPart(tool_name="navigate", args={"url": "/chat"}, tool_call_id="call-1")])

    return Agent(
        streaming_function_model(function),
        deps_type=Runtime,
        output_type=[str, DeferredToolRequests],
        toolsets=[ExternalToolset([ToolDefinition(name="navigate")], id="verbs")],
        capabilities=[StepPersistence(store=store, agent_name="harness-author")],
    )


def _recording_agent(store: InMemoryStepStore, seen: list[Sequence[ModelMessage]]) -> Agent[Runtime, str]:
    """An agent that answers one line and keeps every message it was handed."""

    def function(messages: Sequence[ModelMessage], info: AgentInfo) -> ModelResponse:
        del info
        seen.append(list(messages))
        return ModelResponse(parts=[TextPart("ok")])

    return Agent(streaming_function_model(function), deps_type=Runtime, capabilities=[StepPersistence(store=store, agent_name="harness-author")])


def _pool_holding(agent: Agent[Runtime, str | DeferredToolRequests], store: InMemoryStepStore) -> SessionPool:
    """A pool whose own session holds `agent`, built directly because its output can be deferred."""
    state = a_throwaway_state()
    return SessionPool(
        Runtime(registry=StepRegistry(), library=EmptyLibrary(), agent=agent, session_log=a_log_under(state), store=store, state=state)
    )


def _navigated(call_id: str = "call-1") -> list[ToolResultIn]:
    """The browser's answer to the verb the last turn stopped for."""
    return [ToolResultIn(tool_call_id=call_id, tool_name="navigate", output="navigated to /chat")]


async def _drawn(turn: Turn) -> None:
    """Draw the turn to its end, which is what runs it."""
    async for _event in turn.events:
        pass


async def test_a_turn_answering_the_browser_resumes_the_run_the_record_cannot_offer() -> None:
    """The calls a deferred run stopped on come from the session itself, never from the record.

    A run that stops for the browser settles no complete step, so the record answers nothing for
    it. Reading the pending calls from there dropped every browser answer and started a bare
    continuation instead, which is the bug this pins: the record here is empty throughout.
    """
    store = InMemoryStepStore()
    seen: list[Sequence[ModelMessage]] = []
    pool = _pool_holding(_browser_agent(store, seen), store)
    history = FakeConversationHistory()
    service = ChatService(pool=pool, history=history, refresh=narrates_nowhere)
    session_id = ensure_session_id(pool.default)

    first = await service.send_message(session_id, _asked("take me to chat"))
    await _drawn(first)
    assert await first.settle() is None, "a turn that stopped for the browser settles nothing"

    second = await service.send_message(session_id, _asked(""), tool_results=_navigated())
    await _drawn(second)

    assert history.asked == [session_id], "the record was read once, for the fresh turn; the resume read the session"
    returned = [part.content for message in seen[-1] for part in message.parts if isinstance(part, ToolReturnPart)]
    assert returned == ["navigated to /chat"], "the model saw the browser's answer rather than a bare continuation"
    typed = [part.content for message in seen[-1] for part in message.parts if isinstance(part, UserPromptPart)]
    assert typed[-1] == CONTINUE_PROMPT, "a resume with nothing typed still carries a user part beside the answers"
    assert await second.settle() is not None, "the resumed run reaches a reply"


async def test_an_answer_for_a_call_nobody_is_waiting_on_is_refused() -> None:
    """A stale card answers an id the turn never stopped on, which leaves the real call open."""
    store = InMemoryStepStore()
    seen: list[Sequence[ModelMessage]] = []
    pool = _pool_holding(_browser_agent(store, seen), store)
    service = ChatService(pool=pool, history=FakeConversationHistory(), refresh=narrates_nowhere)
    session_id = ensure_session_id(pool.default)
    await _drawn(await service.send_message(session_id, _asked("take me to chat")))

    with pytest.raises(PartAnsweredError):
        await service.send_message(session_id, _asked(""), tool_results=_navigated("call-9"))

    assert not pool.is_answering(session_id), "a refused continuation gives the session straight back"


async def test_a_request_with_nothing_said_and_nothing_answered_is_refused() -> None:
    """An empty message on a session waiting for nothing has no turn in it, and never runs as `Continue.`."""
    store = InMemoryStepStore()
    pool = _pool_over(_agent(store), store)
    service = ChatService(pool=pool, history=FakeConversationHistory(), refresh=narrates_nowhere)
    session_id = (await pool.create(None)).session_id or ""

    with pytest.raises(TurnEmptyError):
        await service.send_message(session_id, _asked(""))

    assert not pool.is_answering(session_id), "the refusal gives the session straight back"


async def test_the_text_of_a_message_split_across_parts_reaches_the_model_whole() -> None:
    """A client is free to split what was typed, and half a question is not what the person asked."""
    store = InMemoryStepStore()
    seen: list[Sequence[ModelMessage]] = []
    pool = _pool_over(_recording_agent(store, seen), store)
    service = ChatService(pool=pool, history=FakeConversationHistory(), refresh=narrates_nowhere)
    session_id = (await pool.create(None)).session_id or ""

    await _drawn(await service.send_message(session_id, [ModelRequest(parts=[UserPromptPart("first half"), UserPromptPart("second half")])]))

    typed = [part.content for message in seen[-1] for part in message.parts if isinstance(part, UserPromptPart)]
    assert typed[-1] == "first half\nsecond half"


async def test_a_stream_closed_early_records_the_abandonment_and_gives_the_session_back() -> None:
    """A reader that stops mid-turn ends it: no exception reaches the turn, and it must still close itself."""
    store = InMemoryStepStore()
    pool = _pool_over(_agent(store), store)
    service = ChatService(pool=pool, history=FakeConversationHistory(), refresh=narrates_nowhere)
    runtime = await pool.create(None)
    session_id = runtime.session_id or ""

    turn = await service.send_message(session_id, _asked("hi"))
    await anext(turn.events)
    await turn.events.aclose()

    kinds = [record.event.kind for record in runtime.session_log.events()]
    assert "turn_abandoned" in kinds, "a turn nobody finished reading is abandoned, not left open"
    assert "turn_settled" not in kinds
    assert not pool.is_answering(session_id), "the closed stream gave the session back"
