"""The session-scoped step routes: list the run's live lines, tail one's stream, steer one.

Every test builds lines directly on a pooled session's `active.conversations` -- `StepConversations.open`,
appended chunks, `settle`/`close` -- rather than running a model, since these routes only read and
write that state. Driven over ASGI so what is asserted is what a browser would see.
"""

from __future__ import annotations

import asyncio
import json

from contextlib import asynccontextmanager
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import TYPE_CHECKING, Any, cast

import pytest

from core.ai.workflows.contracts import Briefing, Shape

from harness.features.running import controller as running_controller
from harness.features.running.conversations import STEP_STREAM_BUFFER_BYTES, StepConversations
from tests.support.controllers import a_client_of
from tests.support.running import a_pool, a_running_controller

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator

    import httpx

    from harness.features.running.conversations import StepConversation
    from harness.session import SessionPool

VERDICT = {"type": "object", "title": "Verdict", "properties": {"healthy": {"type": "boolean"}}, "required": ["healthy"]}
UNKNOWN = "bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb"


def _task(name: str = "draft") -> Briefing:
    return Briefing(
        name=name, instructions="Read the ledger and say whether the quarter looks healthy.", inputs={}, output=Shape(json_schema=VERDICT)
    )


@asynccontextmanager
async def _studio() -> AsyncGenerator[tuple[httpx.AsyncClient, SessionPool]]:
    """The running controller over ASGI, with the pool its session-scoped routes resolve in."""
    pool = a_pool()
    with TemporaryDirectory() as state:
        async with a_client_of(a_running_controller(pool, Path(state))) as client:
            yield client, pool


def _open(conversations: StepConversations, key: tuple[str, str]) -> StepConversation:
    """A freshly opened line at `key`, executing, with no messages yet."""
    return conversations.open(key, step=_task(key[0]), output_type=dict, instructions="", monitor=None)


def _sse_data(body: str) -> list[str]:
    """The `data: ...` payloads of an SSE body, in order, `[DONE]` included verbatim."""
    return [line.removeprefix("data: ") for line in body.splitlines() if line.startswith("data: ")]


def _frame(payload: str) -> str:
    r"""One chunk exactly as `line.stream` holds it: an already-framed `data: ...\n\n` string."""
    return f"data: {payload}\n\n"


def _floor_data(payload: str) -> dict[str, object]:
    """The `data-replay-floor` chunk's own `data` object, read out of one `_sse_data` payload."""
    parsed = json.loads(payload)
    assert parsed["type"] == "data-replay-floor", parsed
    return cast("dict[str, object]", parsed["data"])


async def test_steps_lists_every_live_line_in_insertion_order() -> None:
    async with _studio() as (client, pool):
        runtime = await pool.create(None)
        conversations = runtime.active.conversations
        _open(conversations, ("fetch", ""))
        conversations.settle(("fetch", ""), messages=[cast("Any", object())])
        _open(conversations, ("draft", ""))

        listed = await client.get(f"/api/sessions/{runtime.session_id}/run/steps")

        assert listed.status_code == 200
        assert listed.json()["steps"] == [
            {"node": "fetch", "item": "", "executing": False, "settled": True},
            {"node": "draft", "item": "", "executing": True, "settled": False},
        ]


async def test_steps_answers_an_empty_list_with_no_live_lines() -> None:
    async with _studio() as (client, pool):
        runtime = await pool.create(None)

        listed = await client.get(f"/api/sessions/{runtime.session_id}/run/steps")

        assert listed.status_code == 200
        assert listed.json() == {"steps": []}


async def test_steps_404s_on_an_unknown_session() -> None:
    async with _studio() as (client, _pool):
        listed = await client.get(f"/api/sessions/{UNKNOWN}/run/steps")

        assert listed.status_code == 404


async def test_a_map_items_line_is_keyed_by_node_and_item() -> None:
    async with _studio() as (client, pool):
        runtime = await pool.create(None)
        conversations = runtime.active.conversations
        _open(conversations, ("summarize", "row-1"))
        _open(conversations, ("summarize", "row-2"))

        listed = await client.get(f"/api/sessions/{runtime.session_id}/run/steps")

        assert {(row["node"], row["item"]) for row in listed.json()["steps"]} == {
            ("summarize", "row-1"),
            ("summarize", "row-2"),
        }


async def test_stream_replays_since_then_closes_on_done() -> None:
    async with _studio() as (client, pool):
        runtime = await pool.create(None)
        line = _open(runtime.active.conversations, ("draft", ""))
        line.stream.append(_frame("a"))
        line.stream.append(_frame("b"))
        line.stream.append(_frame("c"))
        line.stream.close()

        streamed = await client.get(f"/api/sessions/{runtime.session_id}/run/steps/draft/stream", params={"since": 1})

        assert streamed.status_code == 200
        assert streamed.headers["content-type"].startswith("text/event-stream")
        data = _sse_data(streamed.text)
        assert _floor_data(data[0]) == {"floor": 1, "requested": 1}
        assert data[1:] == ["b", "c", "[DONE]"]


async def test_stream_defaults_since_to_zero_and_item_to_empty() -> None:
    async with _studio() as (client, pool):
        runtime = await pool.create(None)
        line = _open(runtime.active.conversations, ("draft", ""))
        line.stream.append(_frame("a"))
        line.stream.close()

        streamed = await client.get(f"/api/sessions/{runtime.session_id}/run/steps/draft/stream")

        data = _sse_data(streamed.text)
        assert _floor_data(data[0]) == {"floor": 0, "requested": 0}
        assert data[1:] == ["a", "[DONE]"]


async def test_stream_first_frame_is_the_dropped_floor_when_since_is_below_it() -> None:
    """`since` asks for chunk 0, but the buffer already evicted it -- the floor names where replay actually starts."""
    async with _studio() as (client, pool):
        runtime = await pool.create(None)
        line = _open(runtime.active.conversations, ("draft", ""))
        line.stream.append(_frame("x" * STEP_STREAM_BUFFER_BYTES))
        line.stream.append(_frame("y"))  # pushes the buffer over the cap; the first frame is evicted
        line.stream.close()

        streamed = await client.get(f"/api/sessions/{runtime.session_id}/run/steps/draft/stream", params={"since": 0})

        assert _floor_data(_sse_data(streamed.text)[0]) == {"floor": 1, "requested": 0}


async def test_stream_tails_chunks_appended_and_closed_after_the_request_started() -> None:
    """A reader that arrives while the line is still open sees exactly what a writer appends later, in order."""
    async with _studio() as (client, pool):
        runtime = await pool.create(None)
        line = _open(runtime.active.conversations, ("draft", ""))

        async def write_after_the_request_is_underway() -> None:
            await asyncio.sleep(0.01)
            line.stream.append(_frame("a"))
            line.stream.append(_frame("b"))
            line.stream.close()

        writer = asyncio.create_task(write_after_the_request_is_underway())
        async with client.stream("GET", f"/api/sessions/{runtime.session_id}/run/steps/draft/stream") as streamed:
            assert streamed.status_code == 200
            body = (await streamed.aread()).decode()
        await writer

        data = _sse_data(body)
        assert _floor_data(data[0]) == {"floor": 0, "requested": 0}
        assert data[1:] == ["a", "b", "[DONE]"]


async def test_step_frames_floor_matches_whatever_chunk_it_actually_delivers_first() -> None:
    """Nothing is buffered when the frame generator starts.

    While it is suspended waiting for the first chunk, a scripted append evicts the backlog that
    would otherwise have answered it. The floor frame the route sends must name exactly the
    absolute index of whatever chunk goes out next -- never a value computed separately, earlier,
    against a buffer that has since moved on.
    """
    line = StepConversations().open(("draft", ""), step=_task(), output_type=dict, instructions="", monitor=None)
    frames: list[str] = []

    async def collect() -> None:
        frames.extend([frame async for frame in running_controller._step_frames(line, 0)])

    task = asyncio.create_task(collect())
    await asyncio.sleep(0.001)  # let the generator reach `replay`'s own suspension point with nothing buffered yet

    size = STEP_STREAM_BUFFER_BYTES // 2 + 100_000
    line.stream.append(_frame("a" * size))  # no eviction yet
    line.stream.append(_frame("b" * size))  # evicts "a"*size -- dropped == 1, the actual answer moves under the frame
    line.stream.close()
    await task

    data = _sse_data("".join(frames))
    assert _floor_data(data[0]) == {"floor": 1, "requested": 0}, "floor names the second chunk's own index, not the evicted first one"
    assert data[1:] == ["b" * size, "[DONE]"], "the first chunk was evicted before the replay ever reached it"


async def test_stream_422s_on_a_negative_since() -> None:
    async with _studio() as (client, pool):
        runtime = await pool.create(None)
        _open(runtime.active.conversations, ("draft", ""))

        streamed = await client.get(f"/api/sessions/{runtime.session_id}/run/steps/draft/stream", params={"since": -1})

        assert streamed.status_code == 422


async def test_stream_404s_on_an_unknown_line() -> None:
    async with _studio() as (client, pool):
        runtime = await pool.create(None)

        streamed = await client.get(f"/api/sessions/{runtime.session_id}/run/steps/nope/stream")
        unknown_session = await client.get(f"/api/sessions/{UNKNOWN}/run/steps/draft/stream")

        assert streamed.status_code == 404
        assert unknown_session.status_code == 404


async def test_steer_appends_to_the_inbox_and_the_stream() -> None:
    async with _studio() as (client, pool):
        runtime = await pool.create(None)
        line = _open(runtime.active.conversations, ("draft", ""))

        steered = await client.post(f"/api/sessions/{runtime.session_id}/run/steps/draft/steer", json={"message": "go faster"})

        assert steered.status_code == 200
        assert steered.json() == {"ok": True}
        assert line.steer_inbox == ["go faster"]
        assert len(line.stream.chunks) == 1
        frame = line.stream.chunks[0]
        assert frame.startswith("data: ")
        payload = json.loads(frame.removeprefix("data: ").strip())
        assert payload["type"] == "data-steer"
        assert payload["data"]["message"] == "go faster"
        assert payload["data"]["at"]


async def test_steer_409s_on_a_settled_line() -> None:
    async with _studio() as (client, pool):
        runtime = await pool.create(None)
        conversations = runtime.active.conversations
        _open(conversations, ("draft", ""))
        conversations.settle(("draft", ""), messages=[cast("Any", object())])

        steered = await client.post(f"/api/sessions/{runtime.session_id}/run/steps/draft/steer", json={"message": "too late"})

        assert steered.status_code == 409


async def test_steer_404s_on_an_unknown_line_or_session() -> None:
    async with _studio() as (client, pool):
        runtime = await pool.create(None)

        unknown_node = await client.post(f"/api/sessions/{runtime.session_id}/run/steps/nope/steer", json={"message": "x"})
        unknown_session = await client.post(f"/api/sessions/{UNKNOWN}/run/steps/draft/steer", json={"message": "x"})

        assert unknown_node.status_code == 404
        assert unknown_session.status_code == 404


async def test_steer_422s_on_an_empty_or_whitespace_message() -> None:
    async with _studio() as (client, pool):
        runtime = await pool.create(None)
        _open(runtime.active.conversations, ("draft", ""))

        empty = await client.post(f"/api/sessions/{runtime.session_id}/run/steps/draft/steer", json={"message": ""})
        blank = await client.post(f"/api/sessions/{runtime.session_id}/run/steps/draft/steer", json={"message": "   "})

        assert empty.status_code == 422
        assert blank.status_code == 422


if __name__ == "__main__":  # pragma: no cover
    pytest.main([__file__])
