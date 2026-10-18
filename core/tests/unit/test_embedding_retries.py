"""The embedding retry policy retries a connection error, a timeout, HTTP 429 and HTTP 5xx, waits between tries and stops."""

from __future__ import annotations

import httpx2
import pytest

from core.ai.models import embedding_http_client, embedding_retry_config


class ScriptedTransport(httpx2.AsyncBaseTransport):
    """Answers each request with the next scripted outcome: a status code with headers, or an error to raise."""

    def __init__(self, *outcomes: int | tuple[int, dict[str, str]] | Exception) -> None:
        self.outcomes = list(outcomes)
        self.sent = 0

    async def handle_async_request(self, request: httpx2.Request) -> httpx2.Response:
        self.sent += 1
        outcome = self.outcomes.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        status, headers = outcome if isinstance(outcome, tuple) else (outcome, {})
        return httpx2.Response(status, headers=headers, request=request)


class Sleeps:
    """Records the waits instead of sleeping."""

    def __init__(self) -> None:
        self.seconds: list[float] = []

    async def __call__(self, seconds: float) -> None:
        self.seconds.append(seconds)


async def _post(transport: ScriptedTransport, sleeps: Sleeps) -> httpx2.Response:
    async with embedding_http_client(wrapped=transport, config=embedding_retry_config(sleep=sleeps)) as client:
        return await client.post("https://example.test/embeddings", content=b"{}")


@pytest.mark.parametrize("failure", [httpx2.ConnectError("down"), httpx2.ReadTimeout("slow"), 429, 500, 503])
async def test_a_failure_a_later_try_can_fix_is_retried(failure):
    transport = ScriptedTransport(failure, 200)
    sleeps = Sleeps()

    response = await _post(transport, sleeps)

    assert response.status_code == 200
    assert transport.sent == 2
    assert len(sleeps.seconds) == 1


@pytest.mark.parametrize("status", [200, 400, 401, 404])
async def test_any_other_response_is_returned_without_a_retry(status):
    transport = ScriptedTransport(status)

    response = await _post(transport, Sleeps())

    assert response.status_code == status
    assert transport.sent == 1


async def test_attempts_stop_at_five_and_the_wait_grows_to_its_cap():
    transport = ScriptedTransport(*[503] * 5)
    sleeps = Sleeps()

    with pytest.raises(httpx2.HTTPStatusError):
        await _post(transport, sleeps)

    assert transport.sent == 5
    assert sleeps.seconds == sorted(sleeps.seconds)
    assert len(sleeps.seconds) == 4
    assert max(sleeps.seconds) <= 8.0


async def test_a_429_waits_as_long_as_its_retry_after_header_says():
    transport = ScriptedTransport((429, {"Retry-After": "7"}), 200)
    sleeps = Sleeps()

    await _post(transport, sleeps)

    assert sleeps.seconds == [7.0]


async def test_the_last_connection_error_is_raised_once_attempts_run_out():
    transport = ScriptedTransport(*[httpx2.ConnectError("down")] * 5)

    with pytest.raises(httpx2.ConnectError):
        await _post(transport, Sleeps())

    assert transport.sent == 5
