"""The chat retry policy retries a failed connection and the statuses the OpenAI SDK retries, three times with growing waits."""

from __future__ import annotations

import asyncio
import gc
import json

from typing import TYPE_CHECKING

import httpx2
import openai
import pytest

from pydantic_ai.retries import AsyncHTTPX2TenacityTransport

from core.ai.models import chat_http_client, chat_retry_config

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

_ERROR_BODY = {"error": {"message": "scripted"}}


class TrackedBody(httpx2.AsyncByteStream):
    """A response body that arrives unread over the wire and records whether it was read and whether it was closed."""

    def __init__(self, content: bytes) -> None:
        self.content = content
        self.read = False
        self.closed = False

    async def __aiter__(self) -> AsyncIterator[bytes]:
        self.read = True
        yield self.content

    async def aclose(self) -> None:
        self.closed = True


class ScriptedTransport(httpx2.AsyncBaseTransport):
    """Responds to each request with the next scripted outcome: a status code with headers, or an error to raise."""

    def __init__(self, *outcomes: int | tuple[int, dict[str, str]] | Exception) -> None:
        self.outcomes = list(outcomes)
        self.sent = 0
        self.bodies: list[TrackedBody] = []
        self.closed = False

    async def handle_async_request(self, request: httpx2.Request) -> httpx2.Response:
        self.sent += 1
        outcome = self.outcomes.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        status, headers = outcome if isinstance(outcome, tuple) else (outcome, {})
        body = TrackedBody(json.dumps(_ERROR_BODY).encode())
        self.bodies.append(body)
        raw_headers = [(b"content-type", b"application/json"), *((name.encode(), value.encode("latin-1")) for name, value in headers.items())]
        return httpx2.Response(status, headers=raw_headers, stream=body, request=request)

    async def aclose(self) -> None:
        self.closed = True


class Sleeps:
    """Records the waits instead of sleeping."""

    def __init__(self) -> None:
        self.seconds: list[float] = []

    async def __call__(self, seconds: float) -> None:
        self.seconds.append(seconds)


async def _post(transport: ScriptedTransport, sleeps: Sleeps) -> httpx2.Response:
    async with chat_http_client(wrapped=transport, config=chat_retry_config(sleep=sleeps)) as client:
        return await client.post("https://example.test/responses", content=b"{}")


def _sdk_client(transport: ScriptedTransport) -> openai.AsyncOpenAI:
    http_client = chat_http_client(wrapped=transport, config=chat_retry_config(sleep=Sleeps()))
    return openai.AsyncOpenAI(api_key="test-key", base_url="https://example.test/v1/", max_retries=0, http_client=http_client)


@pytest.mark.parametrize(
    "failure", [httpx2.ConnectError("down"), httpx2.ConnectTimeout("slow to connect"), httpx2.RemoteProtocolError("Server disconnected")]
)
async def test_a_connection_that_fails_twice_is_retried_until_a_response_comes(failure):
    transport = ScriptedTransport(failure, failure, 200)
    sleeps = Sleeps()

    response = await _post(transport, sleeps)

    assert response.status_code == 200
    assert transport.sent == 3
    assert sleeps.seconds == [1.0, 2.0]


async def test_the_last_connection_error_is_raised_after_three_retries():
    transport = ScriptedTransport(*[httpx2.ConnectError("down")] * 4)
    sleeps = Sleeps()

    with pytest.raises(httpx2.ConnectError):
        await _post(transport, sleeps)

    assert transport.sent == 4
    assert sleeps.seconds == [1.0, 2.0, 4.0]


async def test_a_slow_model_is_never_retried():
    transport = ScriptedTransport(httpx2.ReadTimeout("slow"), 200)
    sleeps = Sleeps()

    with pytest.raises(httpx2.ReadTimeout):
        await _post(transport, sleeps)

    assert transport.sent == 1
    assert sleeps.seconds == []


@pytest.mark.parametrize("status", [408, 409, 429, 500, 503])
async def test_a_status_the_sdk_retries_is_retried_and_the_retried_response_closed_unread(status):
    transport = ScriptedTransport(status, 200)
    sleeps = Sleeps()

    response = await _post(transport, sleeps)

    assert response.status_code == 200
    assert transport.sent == 2
    assert sleeps.seconds == [1.0]
    retried, last = transport.bodies
    assert retried.closed
    assert not retried.read
    assert last.read


async def test_a_429_waits_as_long_as_its_retry_after_header_says():
    transport = ScriptedTransport((429, {"Retry-After": "7"}), 200)
    sleeps = Sleeps()

    response = await _post(transport, sleeps)

    assert response.status_code == 200
    assert sleeps.seconds == [7.0]


async def test_a_retry_after_header_is_capped_at_twenty_seconds():
    transport = ScriptedTransport((429, {"Retry-After": "90"}), 200)
    sleeps = Sleeps()

    await _post(transport, sleeps)

    assert sleeps.seconds == [20.0]


async def test_a_retry_after_header_that_is_not_a_whole_number_falls_back_to_the_backoff():
    transport = ScriptedTransport((429, {"Retry-After": "\N{SUPERSCRIPT TWO}"}), 200)
    sleeps = Sleeps()

    response = await _post(transport, sleeps)

    assert response.status_code == 200
    assert sleeps.seconds == [1.0]


async def test_the_last_response_is_handed_back_unread_once_retries_run_out():
    transport = ScriptedTransport(*[429] * 4)
    sleeps = Sleeps()

    response = await _post(transport, sleeps)

    assert response.status_code == 429
    assert response.json() == _ERROR_BODY
    assert transport.sent == 4
    assert sleeps.seconds == [1.0, 2.0, 4.0]
    assert [body.read for body in transport.bodies] == [False, False, False, True]
    assert all(body.closed for body in transport.bodies[:3])


async def test_the_sdk_raises_its_own_rate_limit_error_once_retries_run_out():
    transport = ScriptedTransport(*[429] * 4)

    with pytest.raises(openai.RateLimitError):
        await _sdk_client(transport).responses.create(model="some-deployment", input="hello")

    assert transport.sent == 4


async def test_the_sdk_raises_its_own_connection_error_once_retries_run_out():
    transport = ScriptedTransport(*[httpx2.ConnectError("down")] * 4)

    with pytest.raises(openai.APIConnectionError):
        await _sdk_client(transport).responses.create(model="some-deployment", input="hello")

    assert transport.sent == 4


@pytest.mark.parametrize("status", [400, 401, 404])
async def test_any_other_response_is_returned_without_a_retry(status):
    transport = ScriptedTransport(status)
    sleeps = Sleeps()

    response = await _post(transport, sleeps)

    assert response.status_code == status
    assert transport.sent == 1
    assert sleeps.seconds == []


def test_the_client_keeps_the_sdks_connection_limits_and_follows_redirects():
    client = chat_http_client()

    retrying = client._transport
    assert isinstance(retrying, AsyncHTTPX2TenacityTransport)
    network = retrying.wrapped
    assert isinstance(network, httpx2.AsyncHTTPTransport)
    pool = network._pool
    assert (pool._max_connections, pool._max_keepalive_connections) == (1000, 100)
    assert client.follow_redirects


async def test_a_client_nothing_holds_any_more_closes_itself():
    transport = ScriptedTransport()
    client = chat_http_client(wrapped=transport)

    del client
    gc.collect()
    await asyncio.sleep(0)

    assert transport.closed
