"""Every agentic step opens an observable line: `StepStream`'s live wire, and the open/settle contract.

`StepStream` is exercised on its own -- replay, live tail, close, and the eviction that keeps a
line's buffer under `STEP_STREAM_BUFFER_BYTES`. The rest drives `build_environment`'s own `run_defined_step`,
the same seam `test_turn_conversations.py` and `test_worker_memory.py` already use, so a line's
lifecycle (open before the model is asked, settled or cleared after) is proven exactly as the
indexer drives it.
"""

from __future__ import annotations

import asyncio

from typing import TYPE_CHECKING, Any, cast

import pytest

from core.ai.workflows.contracts import Briefing, FromNode, FromParameter, ParameterSpec, RunNode, Shape, Workflow, prepare, run_workflow
from pydantic_ai.messages import ModelResponse, ToolCallPart
from pydantic_ai_harness.warn_on_cache_busts import WarnOnCacheBusts

from harness.composition import build_environment
from harness.features.running.conversations import STEP_STREAM_BUFFER_BYTES, StepConversations, StepStream
from harness.session import ActiveRun, NoSessionLog
from harness.settings import WebSearchSettings
from tests.conftest import ApproveAll
from tests.support.environment import fake_engram
from tests.support.state import records_under
from tests.support.streaming import streaming_function_model
from tests.support.tiers import tiers_under

if TYPE_CHECKING:
    from pathlib import Path

    from pydantic_ai.messages import ModelMessage
    from pydantic_ai.models.function import AgentInfo, FunctionModel

LEDGER = {"type": "object", "title": "LedgerIn", "properties": {"net": {"type": "integer"}}, "required": ["net"]}
VERDICT = {"type": "object", "title": "Verdict", "properties": {"healthy": {"type": "boolean"}}, "required": ["healthy"]}


# --- StepStream: the live wire, on its own -----------------------------------------------------


async def test_tail_replays_appended_chunks_then_streams_live_and_ends_on_close() -> None:
    stream = StepStream()
    stream.append("a")
    stream.append("b")
    received: list[str] = []

    async def reader() -> None:
        received.extend([chunk async for chunk in stream.tail()])

    async def writer() -> None:
        await asyncio.sleep(0)
        stream.append("c")
        stream.close()

    await asyncio.gather(reader(), writer())

    assert received == ["a", "b", "c"]


async def test_tail_since_skips_the_first_n_chunks() -> None:
    stream = StepStream()
    stream.append("a")
    stream.append("b")
    stream.append("c")
    stream.close()

    received = [chunk async for chunk in stream.tail(since=1)]

    assert received == ["b", "c"]


def test_append_evicts_the_oldest_whole_frames_once_the_cap_is_exceeded() -> None:
    stream = StepStream()
    first = "a" * (STEP_STREAM_BUFFER_BYTES // 2 + 100_000)
    second = "b" * (STEP_STREAM_BUFFER_BYTES // 2)
    third = "c" * 10

    stream.append(first)
    stream.append(second)  # first + second alone already exceed the cap: the oldest whole frame is evicted
    stream.append(third)

    assert stream.dropped == 1, "the oldest whole frame, and only it, was dropped"
    assert stream.chunks == [second, third], "the newest frames are what the buffer kept"


async def test_tail_below_the_floor_starts_at_the_floor_with_absolute_indices() -> None:
    stream = StepStream()
    first = "a" * (STEP_STREAM_BUFFER_BYTES // 2 + 100_000)
    second = "b" * (STEP_STREAM_BUFFER_BYTES // 2)
    stream.append(first)
    stream.append(second)  # evicts `first`; dropped == 1, so absolute index 0 is gone
    stream.append("c")
    stream.close()

    assert stream.floor(0) == 1, "since 0 asks for chunk 0, but the buffer no longer holds it"
    received = [chunk async for chunk in stream.tail(0)]

    assert received == [second, "c"], "the replay starts at the floor, never the evicted frame"


async def test_a_reader_suspended_in_tail_survives_two_evictions_with_no_crash_or_duplicate() -> None:
    """Three appends evict twice while a `tail(0)` reader is waiting in `_wake.wait()`.

    The reader must not crash on a negative offset and must not repeat or invent a chunk -- it
    jumps forward to whatever the buffer holds once it is actually scheduled again.
    """
    stream = StepStream()
    received: list[str] = []

    async def reader() -> None:
        received.extend([chunk async for chunk in stream.tail(0)])

    task = asyncio.create_task(reader())
    await asyncio.sleep(0.001)  # let the reader reach `await self._wake.wait()` on an empty buffer

    size = STEP_STREAM_BUFFER_BYTES // 2 + 100_000
    stream.append("a" * size)  # no eviction yet
    stream.append("b" * size)  # evicts "a"*size -- dropped == 1
    stream.append("c" * size)  # evicts "b"*size -- dropped == 2, all three land before the reader runs again
    stream.append("d")
    stream.close()
    await task

    assert received == ["c" * size, "d"], "the reader jumps to the current floor once scheduled, then keeps going in order"


async def test_a_reader_suspended_in_tail_survives_one_eviction_and_yields_from_the_new_floor() -> None:
    stream = StepStream()
    received: list[str] = []

    async def reader() -> None:
        received.extend([chunk async for chunk in stream.tail(0)])

    task = asyncio.create_task(reader())
    await asyncio.sleep(0.001)

    size = STEP_STREAM_BUFFER_BYTES // 2 + 100_000
    stream.append("a" * size)  # no eviction yet
    stream.append("b" * size)  # evicts "a"*size -- dropped == 1
    stream.close()
    await task

    assert received == ["b" * size], "the reader starts exactly at the new floor, never the evicted frame"


# --- a one-shot briefing opens and settles its line ----------------------------------------------


def _briefing_task(name: str = "draft") -> Briefing:
    return Briefing(
        name=name,
        instructions="Read the ledger and say whether the quarter looks healthy.",
        inputs={"ledger": Shape(json_schema=LEDGER)},
        output=Shape(json_schema=VERDICT),
    )


def _briefing_spec(task: Briefing) -> Workflow:
    return Workflow(
        name="weigh-once",
        version=1,
        description="One briefing, run once, no map, no turn identity.",
        parameters={"ledger": ParameterSpec(description="the ledger", json_schema=LEDGER)},
        inline=(task,),
        nodes=(RunNode(id=task.name, work=task.name, inputs={"ledger": FromParameter(parameter="ledger")}),),
        output=FromNode(node=task.name),
    )


def _healthy_model() -> FunctionModel:
    def respond(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        del messages
        return ModelResponse(parts=[ToolCallPart(info.output_tools[0].name, {"healthy": True})])

    return streaming_function_model(respond)


async def test_a_one_shot_briefing_leaves_a_settled_line_behind(tmp_path: Path) -> None:
    task = _briefing_task()
    active = ActiveRun()
    environment = await build_environment(_healthy_model(), engram=fake_engram(), active=active, external_agents=dict, session_log=NoSessionLog(), custom=tmp_path / "custom", records=records_under(tmp_path), web_search=WebSearchSettings(), tiers=tiers_under(tmp_path))

    await run_workflow(_briefing_spec(task), environment, params={"ledger": {"net": 400}}, approval=ApproveAll())

    line = active.conversations.conversations[(task.name, "")]
    assert line.messages, "the line holds the run's own messages"
    assert line.executing is False


async def test_the_line_is_executing_while_the_step_runs(tmp_path: Path) -> None:
    task = _briefing_task()
    active = ActiveRun()
    seen: list[bool] = []

    def respond(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        del messages
        line = active.conversations.conversations.get((task.name, ""))
        seen.append(line is not None and line.executing)
        return ModelResponse(parts=[ToolCallPart(info.output_tools[0].name, {"healthy": True})])

    environment = await build_environment(streaming_function_model(respond), engram=fake_engram(), active=active, external_agents=dict, session_log=NoSessionLog(), custom=tmp_path / "custom", records=records_under(tmp_path), web_search=WebSearchSettings(), tiers=tiers_under(tmp_path))

    await run_workflow(_briefing_spec(task), environment, params={"ledger": {"net": 400}}, approval=ApproveAll())

    assert seen == [True], "the line was open and executing at the moment the model was asked"


async def test_a_raising_step_still_clears_executing(tmp_path: Path) -> None:
    task = _briefing_task()
    active = ActiveRun()

    def respond(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        del messages, info
        msg = "boom"
        raise RuntimeError(msg)

    environment = await build_environment(streaming_function_model(respond), engram=fake_engram(), active=active, external_agents=dict, session_log=NoSessionLog(), custom=tmp_path / "custom", records=records_under(tmp_path), web_search=WebSearchSettings(), tiers=tiers_under(tmp_path))
    ready = prepare(_briefing_spec(task), environment)
    assert ready.report.ok, ready.report.issues
    implementation = ready.registry.get(task.name)
    assert implementation is not None

    with pytest.raises(RuntimeError):
        await implementation.function(ledger={"net": 400})

    line = active.conversations.conversations[(task.name, "")]
    assert line.executing is False, "the finally clears it even though the run raised"


async def test_close_all_closes_every_lines_stream(tmp_path: Path) -> None:
    active = ActiveRun()
    environment = await build_environment(_healthy_model(), engram=fake_engram(), active=active, external_agents=dict, session_log=NoSessionLog(), custom=tmp_path / "custom", records=records_under(tmp_path), web_search=WebSearchSettings(), tiers=tiers_under(tmp_path))
    first = _briefing_task("first")
    second = _briefing_task("second")

    await run_workflow(_briefing_spec(first), environment, params={"ledger": {"net": 400}}, approval=ApproveAll())
    await run_workflow(_briefing_spec(second), environment, params={"ledger": {"net": 400}}, approval=ApproveAll())
    active.conversations.close_all()

    assert active.conversations.conversations[("first", "")].stream.closed
    assert active.conversations.conversations[("second", "")].stream.closed


async def test_reset_closes_every_held_lines_stream_before_dropping_it() -> None:
    """A reader still holding a line's stream from before the reset sees it close, not hang forever."""
    conversations = StepConversations()
    line = conversations.open(
        ("draft", ""), step=_briefing_task(), output_type=bool, instructions="read the ledger", monitor=None
    )
    assert not line.stream.closed

    conversations.reset()

    assert line.stream.closed, "the stream a caller may still be tailing is closed, even after its line is dropped"
    assert conversations.conversations == {}


def test_open_on_an_existing_line_refreshes_its_terms_but_keeps_its_history() -> None:
    """A rerun's own step, output type, instructions and monitor land on the line; its messages, stream and inbox do not."""
    conversations = StepConversations()
    line = conversations.open(
        ("draft", ""), step=_briefing_task("first_name"), output_type=bool, instructions="read the ledger", monitor=None
    )
    line.messages = [cast("Any", object())]
    line.stream.append("kept")
    line.steer_inbox.append("kept too")
    monitor = WarnOnCacheBusts()

    reused = conversations.open(
        ("draft", ""), step=_briefing_task("second_name"), output_type=str, instructions="polish it", monitor=monitor
    )

    assert reused is line, "the same line, reused whole"
    assert reused.step.name == "second_name", "the rerun's own step lands on the line"
    assert reused.output_type is str
    assert reused.instructions == "polish it"
    assert reused.monitor is monitor
    assert reused.messages, "the standing history survives a reuse"
    assert reused.stream.chunks == ["kept"], "the live wire survives a reuse"
    assert reused.steer_inbox == ["kept too"], "the steer inbox survives a reuse"
