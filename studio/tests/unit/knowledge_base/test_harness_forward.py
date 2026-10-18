"""The harness forward: each ended refresh run the harness hears about is posted to it, and delivered again until it answers."""

from __future__ import annotations

import json

import httpx
import pytest

from core.cqrs.contracts import ImmediateWake, MemoryJobs, MemoryOutbox, MessageBus, Worker

from studio.knowledge_base.refresh.contracts import RefreshAbandoned, RefreshCompleted, RefreshCounts, RefreshStarted
from studio.knowledge_base.refresh.exceptions import RefreshForwardFailedError
from studio.knowledge_base.refresh.infrastructure.harness_forward import HarnessRefreshForward

HARNESS = "http://harness.test"


def _completed(created: int = 6) -> RefreshCompleted:
    return RefreshCompleted(knowledge_base="notes", job_id=7, counts=RefreshCounts(sources_created=created, requested_by_entry={"x:someone": created}))


def _answering(status: int, seen: list[httpx.Request] | None = None) -> httpx.MockTransport:
    def answer(request: httpx.Request) -> httpx.Response:
        if seen is not None:
            seen.append(request)
        return httpx.Response(status, json={"repeat": False, "notified": True, "armed": []})

    return httpx.MockTransport(answer)


async def test_a_completed_refresh_run_is_posted_with_its_counts() -> None:
    seen: list[httpx.Request] = []
    event = _completed()

    await HarnessRefreshForward(HARNESS, transport=_answering(200, seen))(event)

    (request,) = seen
    assert (request.method, str(request.url)) == ("POST", "http://harness.test/api/schedules/refresh-runs")
    assert json.loads(request.content) == {
        "event_id": str(event.event_id),
        "knowledge_base": "notes",
        "sources_created": 6,
        "requested_by_entry": {"x:someone": 6},
    }


async def test_a_completed_refresh_run_that_created_nothing_is_still_posted() -> None:
    seen: list[httpx.Request] = []

    await HarnessRefreshForward(HARNESS, transport=_answering(200, seen))(_completed(created=0))

    assert len(seen) == 1


@pytest.mark.parametrize("event", [RefreshAbandoned(knowledge_base="notes", job_id=7), RefreshStarted(knowledge_base="notes", job_id=7)])
async def test_what_the_harness_does_not_hear_about_is_never_posted(event: RefreshAbandoned | RefreshStarted) -> None:
    seen: list[httpx.Request] = []

    await HarnessRefreshForward(HARNESS, transport=_answering(200, seen))(event)

    assert seen == []


@pytest.mark.parametrize("status", [404, 500, 503])
async def test_a_harness_without_the_route_or_failing_raises_so_the_event_is_delivered_again(status: int) -> None:
    with pytest.raises(RefreshForwardFailedError, match=str(status)):
        await HarnessRefreshForward(HARNESS, transport=_answering(status))(_completed())


async def test_an_unreachable_harness_raises() -> None:
    def refuse(request: httpx.Request) -> httpx.Response:
        msg = "connection refused"
        raise httpx.ConnectError(msg, request=request)

    with pytest.raises(RefreshForwardFailedError, match="could not be reached"):
        await HarnessRefreshForward(HARNESS, transport=httpx.MockTransport(refuse))(_completed())


async def test_a_body_the_harness_refused_counts_as_delivered() -> None:
    await HarnessRefreshForward(HARNESS, transport=_answering(422))(_completed())


async def test_the_worker_delivers_again_until_the_harness_answers() -> None:
    answers = [httpx.Response(503), httpx.Response(200, json={"repeat": False, "notified": True, "armed": []})]

    def answer(request: httpx.Request) -> httpx.Response:
        del request
        return answers.pop(0)

    outbox = MemoryOutbox()
    bus = MessageBus({}, {RefreshCompleted: []}, outbox=outbox)
    bus.register_subscriber(RefreshCompleted, HarnessRefreshForward(HARNESS, transport=httpx.MockTransport(answer)), name="harness_forward")
    worker = Worker(jobs=MemoryJobs(), outbox=outbox, bus=bus, wake=ImmediateWake(), name="test")
    await outbox.append([_completed()])

    assert await worker.deliver_once() == 0, "a failed delivery leaves the event in the outbox"
    assert await worker.deliver_once() == 1
    assert answers == []
