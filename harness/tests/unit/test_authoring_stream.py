"""One turn of the conversation, heard as the Vercel AI data stream.

The wire is pydantic-ai's own Vercel AI event stream: text-delta chunks as text arrives, a
tool-input-start chunk naming a tool call, an error chunk on a turn that cannot finish, and a
`data-settle` chunk carrying the payload the panel reads. The session log hears the turn too:
started, then settled, failed, or abandoned.

Every case here is about what a turn does, so each one builds the service over its own session and
asks it for the turn; drawing the streamed response is what runs it.
"""

from __future__ import annotations

import asyncio

from typing import TYPE_CHECKING

import anyio
import pytest

from pydantic_ai import Agent, ModelRetry
from pydantic_ai.exceptions import ModelHTTPError
from pydantic_ai.messages import ModelRequest, ModelResponse, TextPart, ToolCallPart, ToolReturnPart, UserPromptPart
from pydantic_ai.models.function import DeltaToolCall, FunctionModel
from pydantic_ai.toolsets import FunctionToolset
from pydantic_ai_harness.step_persistence import ContinuableSnapshot, InMemoryStepStore, RunRecord, StepPersistence

from harness.adapters.session_log import SessionLog
from harness.domain.events import TurnFailed, TurnStarted
from harness.features.authoring import service as authoring_service
from harness.features.authoring.api_models import ChatRequest
from harness.features.authoring.wire import messages_of
from harness.session import TURN_CUT_OFF, PoolComposition, Runtime, SessionPool
from tests.support.authoring import a_chat, a_turn, narrates_nowhere, one_session
from tests.support.fakes.step_store import FakeStepStore
from tests.support.runtime import a_runtime
from tests.support.state import a_session_log, a_state
from tests.support.streaming import streaming_function_model, submit

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Sequence
    from datetime import datetime
    from pathlib import Path

    from pydantic_ai.messages import ModelMessage
    from pydantic_ai.models.function import AgentInfo, DeltaToolCalls
    from pydantic_ai_harness.step_persistence import StepStore

    from harness.domain.events import Recorded, SessionEvent
    from harness.domain.ports import SessionTranscript
    from harness.features.authoring.service import ChatService
    from harness.session import SessionRefresh


def _agent(text: str) -> Agent[Runtime, str]:
    """An agent that answers `text` on the first turn, streamed or not -- no tool call."""

    def function(messages: Sequence[ModelMessage], info: AgentInfo) -> ModelResponse:
        del messages, info
        return ModelResponse(parts=[TextPart(text)])

    async def stream_function(messages: Sequence[ModelMessage], info: AgentInfo) -> AsyncIterator[str]:
        del messages, info
        for i in range(0, len(text), 6):
            yield text[i : i + 6]

    return Agent(FunctionModel(function=function, stream_function=stream_function), deps_type=Runtime)


def _agent_with_tool(text: str) -> Agent[Runtime, str]:
    """An agent that calls `poke` once, then answers `text` -- streamed or not."""
    toolset: FunctionToolset[Runtime] = FunctionToolset()

    def poke() -> str:
        return "poked"

    toolset.add_function(poke, takes_ctx=False)

    def ran(messages: Sequence[ModelMessage]) -> bool:
        return any(isinstance(part, ToolReturnPart) and part.tool_name == "poke" for message in messages for part in message.parts)

    def function(messages: Sequence[ModelMessage], info: AgentInfo) -> ModelResponse:
        del info
        if not ran(messages):
            return ModelResponse(parts=[ToolCallPart("poke", {})])
        return ModelResponse(parts=[TextPart(text)])

    async def stream_function(messages: Sequence[ModelMessage], info: AgentInfo) -> AsyncIterator[str | DeltaToolCalls]:
        del info
        if not ran(messages):
            yield {0: DeltaToolCall(name="poke", json_args="{}", tool_call_id="call-1")}
            return
        for i in range(0, len(text), 6):
            yield text[i : i + 6]

    return Agent(FunctionModel(function=function, stream_function=stream_function), deps_type=Runtime, toolsets=[toolset])


def _insatiable_agent() -> Agent[Runtime, str]:
    """An agent whose model calls a tool on every request, forever -- the budget must stop it."""
    toolset: FunctionToolset[Runtime] = FunctionToolset()

    def poke() -> str:
        return "again"

    toolset.add_function(poke, takes_ctx=False)

    def always_poke(messages: Sequence[ModelMessage], info: AgentInfo) -> ModelResponse:
        del messages, info
        return ModelResponse(parts=[ToolCallPart(tool_name="poke", args={})])

    async def always_poke_stream(messages: Sequence[ModelMessage], info: AgentInfo) -> AsyncIterator[DeltaToolCalls]:
        del info
        yield {0: DeltaToolCall(name="poke", json_args="{}", tool_call_id=f"call-{len(messages)}")}

    return Agent(
        FunctionModel(function=always_poke, stream_function=always_poke_stream),
        deps_type=Runtime,
        output_type=str,
        toolsets=[toolset],
    )


def _agent_with_a_tool_past_its_retries(agent_name: str, store: StepStore) -> Agent[Runtime, str]:
    """An agent whose one tool always retries and immediately exceeds its budget of zero.

    `agent_name` names the runs this agent registers, so a run it wrote is recognisable in `store`.
    """
    toolset: FunctionToolset[Runtime] = FunctionToolset()

    def poke() -> str:
        message = "try something else"
        raise ModelRetry(message)

    toolset.add_function(poke, takes_ctx=False, retries=0)

    def function(messages: Sequence[ModelMessage], info: AgentInfo) -> ModelResponse:
        del messages, info
        return ModelResponse(parts=[TextPart("let me check"), ToolCallPart(tool_name="poke", args={}, tool_call_id="call-1")])

    async def stream_function(messages: Sequence[ModelMessage], info: AgentInfo) -> AsyncIterator[DeltaToolCalls]:
        del messages, info
        yield {0: DeltaToolCall(name="poke", json_args="{}", tool_call_id="call-1")}

    return Agent(
        FunctionModel(function=function, stream_function=stream_function),
        deps_type=Runtime,
        toolsets=[toolset],
        capabilities=[StepPersistence(store=store, agent_name=agent_name)],
    )


def _agent_that_fails_before_any_response(agent_name: str, store: StepStore) -> Agent[Runtime, str]:
    """An agent whose model call itself raises, before any `ModelResponse` is ever built."""

    def function(messages: Sequence[ModelMessage], info: AgentInfo) -> ModelResponse:
        del messages, info
        raise ModelHTTPError(status_code=402, model_name="test-model", body="no balance")

    return Agent(
        streaming_function_model(function), deps_type=Runtime, capabilities=[StepPersistence(store=store, agent_name=agent_name)]
    )


def _agent_with_a_blocking_tool(gate: asyncio.Event, entered: asyncio.Event, agent_name: str, store: StepStore) -> Agent[Runtime, str]:
    """An agent whose one tool call sets `entered`, then waits on `gate`.

    `gate` is the test's own hand on when the call may finish, so a turn can be cut off with that
    call still open.
    """
    toolset: FunctionToolset[Runtime] = FunctionToolset()

    async def poke() -> str:
        entered.set()
        await gate.wait()
        return "poked"

    toolset.add_function(poke, takes_ctx=False)

    def function(messages: Sequence[ModelMessage], info: AgentInfo) -> ModelResponse:
        del messages, info
        return ModelResponse(parts=[ToolCallPart(tool_name="poke", args={}, tool_call_id="call-1")])

    async def stream_function(messages: Sequence[ModelMessage], info: AgentInfo) -> AsyncIterator[DeltaToolCalls]:
        del messages, info
        yield {0: DeltaToolCall(name="poke", json_args="{}", tool_call_id="call-1")}

    return Agent(
        FunctionModel(function=function, stream_function=stream_function),
        deps_type=Runtime,
        toolsets=[toolset],
        capabilities=[StepPersistence(store=store, agent_name=agent_name)],
    )


def _talking_to(
    agent: Agent[Runtime, str], *, state: Path, refresh: SessionRefresh = narrates_nowhere, store: SessionTranscript | None = None
) -> tuple[ChatService, Runtime, SessionPool, str]:
    """The chat service, the one session it answers for, its pool, and that session's id.

    `store` is the record this session writes through, named by a caller whose agent writes through
    the same one; a caller that never reads it back leaves it out and gets a record of its own.
    """
    session = a_runtime(
        agent=agent, session_log=a_session_log(state), store=store if store is not None else InMemoryStepStore(), state=a_state(state)
    )
    pool = SessionPool(session, composition=PoolComposition(refresh=refresh))
    return a_chat(pool, refresh=refresh), session, pool, one_session(pool)


async def _cut_off_mid_turn(chat: ChatService, session_id: str, entered: asyncio.Event, gate: asyncio.Event) -> None:
    """One turn drawn until its tool is running, then cut off the way an interrupt cuts one off.

    The task drawing the turn's events is cancelled, which is what a real disconnect delivers here:
    the turn's own teardown is what must record and rescue it.
    """
    turn = await chat.send_message(session_id, messages_of(ChatRequest.model_validate(submit("go now"))))

    async def drain() -> None:
        async for _event in turn.events:
            pass

    task = asyncio.create_task(drain())
    await asyncio.wait_for(entered.wait(), timeout=2)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    gate.set()


def _journal_kinds(home: Path) -> list[str]:
    return [record.event.kind for record in a_session_log(home).events()]


async def test_a_turn_streams_its_text_and_settles_one_payload(tmp_path: Path) -> None:
    reply = "Here is a draft."
    chat, _session, _pool, session_id = _talking_to(_agent(reply), state=tmp_path)

    chunks = await a_turn(chat, session_id, submit("hello"))

    settles = [chunk for chunk in chunks if chunk.get("type") == "data-settle"]
    assert len(settles) == 1, "the turn's settled payload rides exactly one data-settle chunk"
    assert settles[0]["data"]["message"] == reply
    assert any(chunk.get("type") == "text-delta" for chunk in chunks), "the text streamed before it settled"
    assert _journal_kinds(tmp_path)[-2:] == ["turn_started", "turn_settled"]


async def test_stream_announces_a_tool_call_by_name_before_the_result(tmp_path: Path) -> None:
    chat, _session, _pool, session_id = _talking_to(_agent_with_tool("Done."), state=tmp_path)

    chunks = await a_turn(chat, session_id, submit("go"))

    tool_starts = [chunk for chunk in chunks if chunk.get("type") == "tool-input-start"]
    assert tool_starts, "a tool call in flight must be announced"
    assert tool_starts[0]["toolName"] == "poke", "the chunk names the tool"
    settle_index = next(i for i, chunk in enumerate(chunks) if chunk.get("type") == "data-settle")
    start_index = chunks.index(tool_starts[0])
    assert start_index < settle_index, "the announcement precedes the settled result"


async def test_stream_carries_one_data_step_chunk_per_model_response_before_the_finish_chunk(tmp_path: Path) -> None:
    chat, _session, _pool, session_id = _talking_to(_agent_with_tool("Done."), state=tmp_path)

    chunks = await a_turn(chat, session_id, submit("go"))

    step_indices = [i for i, chunk in enumerate(chunks) if chunk.get("type") == "data-step"]
    assert len(step_indices) == 2, "one response called the tool, the second answered"
    steps = [chunks[i]["data"] for i in step_indices]
    assert steps[0]["tool_call_ids"] == ["call-1"], "the first response's own call rides its step"
    assert steps[1]["tool_call_ids"] == [], "the answering response made no call"
    for step in steps:
        usage = step["usage"]
        for field in ("input_tokens", "output_tokens", "cache_read_tokens", "cache_write_tokens"):
            assert isinstance(usage[field], int)
            assert usage[field] >= 0
        assert isinstance(step["timestamp"], str)
    finish_index = next(i for i, chunk in enumerate(chunks) if chunk.get("type") == "finish")
    assert all(i < finish_index for i in step_indices), "every step must reach the wire before the stream ends"


async def test_stream_emits_an_error_chunk_when_the_turn_does_not_finish(tmp_path: Path) -> None:
    chat, _session, _pool, session_id = _talking_to(_insatiable_agent(), state=tmp_path)

    chunks = await a_turn(chat, session_id, submit("loop forever"))

    assert any(chunk.get("type") == "error" for chunk in chunks), "a turn that never settles must close as an error, not a silent drop"
    assert not any(chunk.get("type") == "data-settle" for chunk in chunks), "a failed turn must never also carry a settled payload"
    assert _journal_kinds(tmp_path)[-2:] == ["turn_started", "turn_failed"]


async def test_stream_journals_the_prompt_excerpt_on_a_failed_turn(tmp_path: Path) -> None:
    """A session whose first turn crashes has no settled run to read its title from later.

    `turn_failed` carries the turn's own opening words instead, so the projection still has
    something to show besides a short-id fallback.
    """
    chat, _session, _pool, session_id = _talking_to(_insatiable_agent(), state=tmp_path)

    await a_turn(chat, session_id, submit("loop forever"))

    failed = next(record.event for record in a_session_log(tmp_path).events() if isinstance(record.event, TurnFailed))
    assert failed.prompt_excerpt == "loop forever"


class _RefusesTurnStarts(SessionLog):
    """A session log whose disk refuses every `turn_started` entry and takes every other one."""

    def record(self, event: SessionEvent, *, at: datetime | None = None) -> Recorded | None:
        if isinstance(event, TurnStarted):
            return None
        return super().record(event, at=at)


@pytest.mark.parametrize(("agent", "ending"), [(_agent("Here is a draft."), "turn_settled"), (_insatiable_agent(), "turn_failed")])
async def test_a_turn_whose_start_the_disk_refused_records_no_end(agent: Agent[Runtime, str], ending: str, tmp_path: Path) -> None:
    """An end names the start it closes, so a turn with no start on the log writes no end either."""
    session = a_runtime(
        agent=agent, session_log=_RefusesTurnStarts(a_state(tmp_path).session_log, topics=None), store=InMemoryStepStore(), state=a_state(tmp_path)
    )
    pool = SessionPool(session, composition=PoolComposition(refresh=narrates_nowhere))
    chat = a_chat(pool, refresh=narrates_nowhere)

    await a_turn(chat, one_session(pool), submit("hello"))

    assert ending not in _journal_kinds(tmp_path)


async def test_stream_refreshes_the_projection_when_the_turn_fails(tmp_path: Path) -> None:
    """The success path refreshes the projection the moment it settles; a failed turn must too.

    Otherwise the session's projected row is stuck at whatever it held before this turn ran --
    for a brand new session, "0 turns" and a short-id title even though the person's message is
    right there on the session log.
    """
    refreshed: list[str | None] = []

    async def refresh(runtime: Runtime) -> None:
        refreshed.append(runtime.session_id)

    chat, _session, _pool, session_id = _talking_to(_insatiable_agent(), state=tmp_path, refresh=refresh)

    await a_turn(chat, session_id, submit("loop forever"))
    await asyncio.sleep(0)

    assert refreshed, "a failed turn must still schedule its own projection refresh"


async def test_a_turn_that_dies_before_any_model_response_keeps_the_owners_prompt(tmp_path: Path) -> None:
    """A run that never got a model response has no snapshot to rescue -- the owner's words stand in.

    The model then sees two owner messages in a row for its next turn, which every provider
    accepts, rather than losing what the owner asked entirely.
    """
    store = InMemoryStepStore()
    chat, session, _pool, session_id = _talking_to(
        _agent_that_fails_before_any_response("rescue-no-snapshot", store), state=tmp_path, store=store
    )

    await a_turn(chat, session_id, submit("hello there"))

    history = session.conversation.history
    assert len(history) == 1
    only = history[0]
    assert isinstance(only, ModelRequest)
    assert [part.content for part in only.parts if isinstance(part, UserPromptPart)] == ["hello there"]


async def test_rescue_falls_back_to_the_prompt_when_the_snapshot_cannot_be_read(tmp_path: Path) -> None:
    """A read the record refuses must fall through to the prompt fallback, never raise.

    A transcript can refuse the run it was asked for -- a torn read, a message shape it no longer
    parses, a half-written record -- and raise rather than answering `None`. `rescue_turn` must
    survive that: the turn still opens on what the person said.
    """
    run_id = "damaged-run"
    store = FakeStepStore(broken=frozenset({run_id}))
    session = a_runtime(store=store, session_log=a_session_log(tmp_path), state=a_state(tmp_path))
    session_id = authoring_service.ensure_session_id(session)
    await store.register_run(RunRecord(run_id=run_id, conversation_id=session_id))

    await authoring_service.rescue_turn(
        session,
        authoring_service.RescuedTurn(session_id=session_id, prompt="please help", before_run_id=None, ended=TURN_CUT_OFF),
        refresh=None,
    )

    history = session.conversation.history
    assert len(history) == 1, "the unreadable snapshot must not raise, and must not be mistaken for a real rescue"
    only = history[0]
    assert isinstance(only, ModelRequest)
    assert [part.content for part in only.parts if isinstance(part, UserPromptPart)] == ["please help"]


async def test_rescue_reads_a_snapshot_shorter_than_the_current_history_when_the_run_id_moved(tmp_path: Path) -> None:
    """Identity, not size, decides whether to rescue.

    A compacting agent can trim its own snapshot below the length of the history the turn started
    with. Five settled messages stand in for what earlier turns already left in
    `session.conversation.history`; the new run's own snapshot, one response with one open call,
    is shorter than that but is what must be read back, because its run id differs from the one
    this turn started with.
    """
    store = InMemoryStepStore()
    session = a_runtime(store=store, session_log=a_session_log(tmp_path), state=a_state(tmp_path))
    session_id = authoring_service.ensure_session_id(session)
    session.conversation.history = [ModelRequest(parts=[UserPromptPart(f"turn {i}")]) for i in range(5)]
    run_id = "compacted-run"
    await store.register_run(RunRecord(run_id=run_id, conversation_id=session_id))
    await store.save_snapshot(
        ContinuableSnapshot(
            run_id=run_id,
            step_index=0,
            messages=[ModelResponse(parts=[ToolCallPart(tool_name="poke", args={}, tool_call_id="call-1")])],
            conversation_id=session_id,
            state="interrupted",
        )
    )

    await authoring_service.rescue_turn(
        session, authoring_service.RescuedTurn(session_id=session_id, prompt="go", before_run_id=None, ended=TURN_CUT_OFF), refresh=None
    )

    history = session.conversation.history
    assert len(history) == 2, "the compacted snapshot plus its own closing request, not the 5-message pre-run history"
    closing = history[-1]
    assert isinstance(closing, ModelRequest)
    returns = [part for part in closing.parts if isinstance(part, ToolReturnPart)]
    assert len(returns) == 1
    assert returns[0].tool_call_id == "call-1"


async def test_rescue_turn_completes_despite_an_already_cancelled_enclosing_scope(tmp_path: Path) -> None:
    """The shield lets `rescue_turn`'s own reads finish despite an already-cancelled scope.

    This is the exact mechanism a real client disconnect triggers under this app's ASGI server
    (`anyio.CancelScope.cancel()`, not a bare `asyncio.Task.cancel()`, is what a real disconnect
    delivers, per uvicorn's declared ASGI `spec_version` and Starlette's own dispatch on it).
    Unshielded, `continue_run`'s file read would be cut off before it returns, and the turn's open
    call would never be closed.
    """
    store = InMemoryStepStore()
    session = a_runtime(store=store, session_log=a_session_log(tmp_path), state=a_state(tmp_path))
    session_id = authoring_service.ensure_session_id(session)
    run_id = "shield-proves-run"
    await store.register_run(RunRecord(run_id=run_id, conversation_id=session_id))
    await store.save_snapshot(
        ContinuableSnapshot(
            run_id=run_id,
            step_index=0,
            messages=[ModelResponse(parts=[ToolCallPart(tool_name="poke", args={}, tool_call_id="call-1")])],
            conversation_id=session_id,
            state="interrupted",
        )
    )

    with anyio.CancelScope() as scope:
        scope.cancel()
        await authoring_service.rescue_turn(
            session, authoring_service.RescuedTurn(session_id=session_id, prompt="go", before_run_id=None, ended=TURN_CUT_OFF), refresh=None
        )

    history = session.conversation.history
    assert history, "the rescue must still have written the closing return"
    closing = history[-1]
    assert isinstance(closing, ModelRequest)
    returns = [part for part in closing.parts if isinstance(part, ToolReturnPart)]
    assert len(returns) == 1
    assert returns[0].tool_call_id == "call-1"


async def test_the_enclosing_cancellation_still_wins_once_the_shielded_rescue_returns(tmp_path: Path) -> None:
    """The shield protects only the rescue's own reads.

    Once `rescue_turn` returns, the cancellation that was already pending on the enclosing scope
    is still there and still ends the turn, at the very next checkpoint -- the shield does not
    swallow it, only defer it past the one call it protects.
    """
    session = a_runtime(store=InMemoryStepStore(), session_log=a_session_log(tmp_path), state=a_state(tmp_path))
    session_id = authoring_service.ensure_session_id(session)

    with anyio.CancelScope() as scope:
        scope.cancel()
        await authoring_service.rescue_turn(
            session, authoring_service.RescuedTurn(session_id=session_id, prompt="go", before_run_id=None, ended=TURN_CUT_OFF), refresh=None
        )
        with pytest.raises(asyncio.CancelledError):
            await asyncio.sleep(0)


async def test_a_turn_that_dies_on_a_tool_past_its_retries_is_rescued(tmp_path: Path) -> None:
    """The owner's prompt, the model's words, its call, and a closing return all survive.

    Left alone, the failure path never touches `session.conversation.history`, so the next turn
    would start from whatever the turn BEFORE this one settled -- this one's own words gone,
    including the tool call the agent is mid-way through.
    """
    store = InMemoryStepStore()
    chat, session, _pool, session_id = _talking_to(
        _agent_with_a_tool_past_its_retries("rescue-failed", store), state=tmp_path, store=store
    )

    await a_turn(chat, session_id, submit("please poke it"))

    history = session.conversation.history
    assert len(history) == 3, "the owner's prompt, the model's response, and one closing request"
    prompt_request, model_response, closing = history
    assert isinstance(prompt_request, ModelRequest)
    assert any(isinstance(part, UserPromptPart) and part.content == "please poke it" for part in prompt_request.parts)
    assert isinstance(model_response, ModelResponse)
    assert any(isinstance(part, ToolCallPart) and part.tool_name == "poke" for part in model_response.parts)
    assert isinstance(closing, ModelRequest)
    assert len(closing.parts) == 1
    part = closing.parts[0]
    assert isinstance(part, ToolReturnPart)
    assert part.tool_name == "poke"
    assert part.tool_call_id == "call-1"
    content = part.content
    assert isinstance(content, str)
    assert content.startswith("not answered, the turn ended here (")
    assert "exceeded max retries" in content
    assert content.endswith("The owner's next message follows, answer it before calling this again.")


async def test_a_cut_off_turn_with_an_open_tool_call_is_rescued(tmp_path: Path) -> None:
    """An interrupted turn's open call is closed with the cut-off sentence, not the failed one."""
    gate = asyncio.Event()
    entered = asyncio.Event()
    store = InMemoryStepStore()
    chat, session, _pool, session_id = _talking_to(
        _agent_with_a_blocking_tool(gate, entered, "rescue-cut-off", store), state=tmp_path, store=store
    )

    await _cut_off_mid_turn(chat, session_id, entered, gate)

    history = session.conversation.history
    assert history, "the cut-off turn's messages must survive in the session"
    closing = history[-1]
    assert isinstance(closing, ModelRequest)
    returns = [part for part in closing.parts if isinstance(part, ToolReturnPart)]
    assert len(returns) == 1
    assert returns[0].tool_call_id == "call-1"
    assert returns[0].content == (
        "not answered, the turn was cut off here. The owner's next message follows, answer it before calling this again."
    )


async def test_a_client_disconnect_mid_stream_hands_the_session_back_and_journals_the_abandonment(tmp_path: Path) -> None:
    """A stream nobody is listening to any more gives its session back, and says so on the timeline."""
    gate = asyncio.Event()
    entered = asyncio.Event()
    store = InMemoryStepStore()
    chat, _session, pool, session_id = _talking_to(
        _agent_with_a_blocking_tool(gate, entered, "abandoned", store), state=tmp_path, store=store
    )

    await _cut_off_mid_turn(chat, session_id, entered, gate)

    assert _journal_kinds(tmp_path)[-1] == "turn_abandoned", "the disconnect is on the timeline, not waiting for the boot sweep"
    assert not pool.is_answering(session_id), "the abandoned turn released the session it was holding"


async def test_a_client_disconnect_mid_stream_also_schedules_the_projection_refresh(tmp_path: Path) -> None:
    """The abandonment path shares the failure path's gap.

    Without its own refresh, the session's projected row is stuck at whatever it held before this
    turn ran.
    """
    refreshed: list[str | None] = []

    async def refresh(runtime: Runtime) -> None:
        refreshed.append(runtime.session_id)

    gate = asyncio.Event()
    entered = asyncio.Event()
    store = InMemoryStepStore()
    chat, _session, _pool, session_id = _talking_to(
        _agent_with_a_blocking_tool(gate, entered, "abandoned-refresh", store), state=tmp_path, refresh=refresh, store=store
    )

    await _cut_off_mid_turn(chat, session_id, entered, gate)
    await asyncio.sleep(0)

    assert refreshed, "an abandoned turn must still schedule its own projection refresh"


async def test_a_reply_that_looks_like_the_old_envelope_streams_and_settles_verbatim(tmp_path: Path) -> None:
    """The envelope is gone: a reply text is shown exactly as the model wrote it, whatever it looks like.

    An agent explaining a JSON payload, quoting a tool result, or writing an example envelope is
    not mistaken for one -- there is no re-voicer left to unwrap it, on the stream or in the settle.
    """
    reply = '{"message": "Hello **environment**."}'
    chat, _session, _pool, session_id = _talking_to(_agent(reply), state=tmp_path)

    chunks = await a_turn(chat, session_id, submit("hi"))

    streamed_text = "".join(chunk["delta"] for chunk in chunks if chunk.get("type") == "text-delta")
    assert streamed_text == reply, "text deltas pass through untouched"
    settled = next(chunk for chunk in chunks if chunk.get("type") == "data-settle")
    assert settled["data"]["message"] == reply, "the settle payload carries the reply verbatim too"


def test_a_provider_refusal_becomes_one_plain_sentence() -> None:
    raw = "status_code: 402, model_name: deepseek-v4-pro, body: {'message': 'Insufficient Balance', 'type': 'unknown_error'}"

    said = authoring_service.plain_error(raw)

    assert "body" not in said
    assert "deepseek" not in said.lower()
    assert "balance" in said.lower()


if __name__ == "__main__":  # pragma: no cover
    pytest.main([__file__])
