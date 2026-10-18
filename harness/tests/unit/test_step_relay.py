"""The step-stream relay and the steer capability: `step_stream.py`'s own two seams.

`run_relayed` is driven directly against a hand-built `Agent[None, object]`, no workflow indexer
needed -- it only cares about the `run_stream_events` seam pydantic-ai itself offers. `FunctionModel`'s
`stream_function` stands in for the provider, so a run drives real Vercel v6 encoding without a live
model anywhere in the test.
"""

from __future__ import annotations

import json

from types import NoneType
from typing import TYPE_CHECKING

import pytest

from core.ai.workflows.contracts import Briefing, Shape
from pydantic_ai import Agent
from pydantic_ai.messages import ToolReturnPart, UserPromptPart
from pydantic_ai.models.function import DeltaToolCall, FunctionModel
from pydantic_ai.toolsets import FunctionToolset
from pydantic_ai.usage import UsageLimits

from harness.features.running.conversations import StepConversation
from harness.features.running.step_stream import STEP_STEER_PREAMBLE, StepSteerDelivery, run_relayed, steer_chunk

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

    from pydantic_ai.messages import ModelMessage
    from pydantic_ai.models.function import AgentInfo, DeltaToolCalls

_BUDGET = UsageLimits(request_limit=10)


def _probe_task() -> Briefing:
    return Briefing(
        name="probe",
        instructions="Answer briefly using whatever the test scenario hands you to work with.",
        output=Shape(json_schema={"type": "string"}),
    )


def _kinds(line: StepConversation) -> list[str]:
    return [json.loads(chunk.removeprefix("data: ").rstrip())["type"] for chunk in line.stream.chunks]


async def test_relay_leaves_data_prefixed_chunks_with_a_text_delta_and_no_done_frame() -> None:
    line = StepConversation(step=_probe_task(), output_type=str)

    async def stream_function(messages: list[ModelMessage], info: AgentInfo) -> AsyncIterator[str]:
        del messages, info
        yield "hello there"

    worker = Agent(FunctionModel(stream_function=stream_function), deps_type=NoneType)

    await run_relayed(worker, line, "hi", usage_limits=_BUDGET)

    assert line.stream.chunks, "the relay appended at least one chunk"
    assert all(chunk.startswith("data: ") for chunk in line.stream.chunks), line.stream.chunks
    assert any("text-delta" in chunk for chunk in line.stream.chunks), line.stream.chunks
    assert not any("[DONE]" in chunk for chunk in line.stream.chunks), "the run's own [DONE] frame never reaches the buffer"


async def test_relay_carries_one_envelope_across_a_tool_round_trip() -> None:
    """One tool call, then a text answer -- the whole run rides one start...finish envelope, not one per node."""
    line = StepConversation(step=_probe_task(), output_type=str)
    toolset: FunctionToolset[None] = FunctionToolset()

    def search(query: str) -> str:
        return f"result for {query}"

    toolset.add_function(search, takes_ctx=False)

    def ran(messages: list[ModelMessage]) -> bool:
        return any(isinstance(part, ToolReturnPart) and part.tool_name == "search" for message in messages for part in message.parts)

    async def stream_function(messages: list[ModelMessage], info: AgentInfo) -> AsyncIterator[str | DeltaToolCalls]:
        del info
        if not ran(messages):
            yield {0: DeltaToolCall(name="search", json_args='{"query": "quantum computing"}', tool_call_id="call-1")}
            return
        yield "Quantum computing uses qubits."

    worker = Agent(FunctionModel(stream_function=stream_function), deps_type=NoneType, toolsets=[toolset])

    result = await run_relayed(worker, line, "tell me about quantum computing", usage_limits=_BUDGET)

    kinds = _kinds(line)
    assert kinds.count("start") == 1, kinds
    assert kinds.count("finish") == 1, kinds
    assert not any("[DONE]" in chunk for chunk in line.stream.chunks), "the run's own [DONE] frame never reaches the buffer"
    start, finish = kinds.index("start"), kinds.index("finish")
    tool_kinds = {"tool-input-start", "tool-input-delta", "tool-input-available", "tool-output-available"}
    assert tool_kinds & set(kinds), kinds
    assert all(start < kinds.index(kind) < finish for kind in tool_kinds if kind in kinds), kinds
    assert start < kinds.index("text-delta") < finish, kinds
    assert result.output == "Quantum computing uses qubits."


class _BoomError(RuntimeError):
    """A step's own failure, distinct from anything the library might raise on its own."""


async def test_a_raising_run_reraises_leaves_an_error_chunk_and_no_done_frame() -> None:
    """A run that raises mid-stream still closes the wire like `transform_stream` closes any other.

    An `error` chunk, never a lying `finish` with no error in sight -- and the exception itself
    still reaches the caller, unchanged, the same as a plain `worker.run(...)` would raise it.
    """
    line = StepConversation(step=_probe_task(), output_type=str)
    line.executing = True  # mirrors StepConversations.open() marking a line executing before its run

    async def stream_function(messages: list[ModelMessage], info: AgentInfo) -> AsyncIterator[str]:
        del messages, info
        yield "partial "
        msg = "boom"
        raise _BoomError(msg)

    worker = Agent(FunctionModel(stream_function=stream_function), deps_type=NoneType)

    async def run_it() -> None:
        try:
            await run_relayed(worker, line, "go", usage_limits=_BUDGET)
        finally:
            line.executing = False

    with pytest.raises(_BoomError):
        await run_it()

    assert line.executing is False, "the composition site's own finally still clears it on a raise"
    kinds = _kinds(line)
    assert "error" in kinds, kinds
    assert not any("[DONE]" in chunk for chunk in line.stream.chunks), "the run's own [DONE] frame never reaches the buffer"


async def test_a_second_turn_appends_its_own_envelope_onto_the_same_buffer() -> None:
    """Two calls against the same line -- a map item's two turns -- leave two start...finish envelopes."""
    line = StepConversation(step=_probe_task(), output_type=str)

    async def stream_function(messages: list[ModelMessage], info: AgentInfo) -> AsyncIterator[str]:
        del messages, info
        yield "ok"

    worker = Agent(FunctionModel(stream_function=stream_function), deps_type=NoneType)

    await run_relayed(worker, line, "first turn", usage_limits=_BUDGET)
    await run_relayed(worker, line, "second turn", usage_limits=_BUDGET)

    kinds = _kinds(line)
    assert kinds.count("start") == 2, kinds
    assert kinds.count("finish") == 2, kinds
    assert not any("[DONE]" in chunk for chunk in line.stream.chunks), "the run's own [DONE] frame never reaches the buffer"


def test_steer_chunk_is_one_data_frame_matching_the_wire_shape() -> None:
    frame = steer_chunk("hi", "2026-08-31T10:00:00+00:00")

    assert frame.startswith("data: ")
    assert frame.endswith("\n\n")
    payload = json.loads(frame.removeprefix("data: ").rstrip())
    assert payload == {"type": "data-steer", "data": {"message": "hi", "at": "2026-08-31T10:00:00+00:00"}}


async def test_a_steer_waiting_before_the_run_reaches_the_second_request_then_empties() -> None:
    """`ctx.enqueue` delivers one graph-node-boundary later than the call, as a redirect past the run's own end.

    The same mechanism the chat's `SteerDelivery` rides: what a capability enqueues never lands
    inside the very request the drain ran ahead of. So a line's inbox, seeded before the run
    starts, first drains on request one and reaches the model on request two, once request one's
    own answer would otherwise have ended the run.
    """
    line = StepConversation(step=_probe_task(), output_type=str)
    line.steer_inbox.append("check the budget before you answer")
    requests: list[list[ModelMessage]] = []

    async def stream_function(messages: list[ModelMessage], info: AgentInfo) -> AsyncIterator[str]:
        del info
        requests.append(list(messages))
        yield "done"

    worker = Agent(FunctionModel(stream_function=stream_function), deps_type=NoneType, capabilities=[StepSteerDelivery(line)])

    await run_relayed(worker, line, "go", usage_limits=_BUDGET)

    assert len(requests) == 2, "request one answers, finds the steer still pending, and is redirected into request two"
    second = requests[1]
    prompts = [part.content for message in second for part in message.parts if isinstance(part, UserPromptPart)]
    assert any(isinstance(prompt, str) and STEP_STEER_PREAMBLE in prompt for prompt in prompts), prompts
    assert any(isinstance(prompt, str) and "check the budget before you answer" in prompt for prompt in prompts), prompts
    assert line.steer_inbox == [], "the inbox is empty once its message reached the model"
