"""`HttpxPageFetcher.fetch_bytes`: every redirect hop checked, and a body over the cap refused."""

from __future__ import annotations

import httpx
import pytest

from core.net import FetchError, HttpxPageFetcher


class FakeInternet:
    """A fake transport answering each request from a script keyed by URL, recording every request."""

    def __init__(self, responses: dict[str, httpx.Response]) -> None:
        self.responses = responses
        self.requested: list[str] = []

    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self._answer)

    def _answer(self, request: httpx.Request) -> httpx.Response:
        self.requested.append(str(request.url))
        return self.responses[str(request.url)]


async def _public(host: str) -> tuple[str, ...]:
    del host
    return ("93.184.216.34",)


def _fetcher(internet: FakeInternet) -> HttpxPageFetcher:
    return HttpxPageFetcher(httpx.AsyncClient(transport=internet.transport()), resolve=_public)


async def test_fetch_bytes_follows_a_public_redirect_to_its_body() -> None:
    internet = FakeInternet(
        {
            "https://data.example.org/a.zip": httpx.Response(302, headers={"location": "https://cdn.example.org/a.zip"}),
            "https://cdn.example.org/a.zip": httpx.Response(200, content=b"PK\x03\x04data"),
        }
    )

    body = await _fetcher(internet).fetch_bytes("https://data.example.org/a.zip", max_bytes=1_000)

    assert body == b"PK\x03\x04data"


async def test_fetch_bytes_refuses_a_redirect_to_a_private_address_before_requesting_it() -> None:
    internet = FakeInternet(
        {
            "https://data.example.org/a.zip": httpx.Response(302, headers={"location": "http://10.0.0.1/a.zip"}),
            "http://10.0.0.1/a.zip": httpx.Response(200, content=b"secret"),
        }
    )

    with pytest.raises(FetchError, match=r"10\.0\.0\.1"):
        await _fetcher(internet).fetch_bytes("https://data.example.org/a.zip", max_bytes=1_000)

    assert internet.requested == ["https://data.example.org/a.zip"]


async def test_fetch_bytes_refuses_a_body_over_the_cap() -> None:
    internet = FakeInternet({"https://data.example.org/a.zip": httpx.Response(200, content=b"x" * 2_000)})

    with pytest.raises(FetchError, match="1000 bytes"):
        await _fetcher(internet).fetch_bytes("https://data.example.org/a.zip", max_bytes=1_000)
