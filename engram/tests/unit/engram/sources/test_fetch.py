from __future__ import annotations

import httpx
import pytest

from core.net import FetchError, HttpxPageFetcher, refuse_unfetchable

HTML = """<html><head><title>Agents that plan</title></head><body>
<nav>Home About</nav>
<article><h1>Agents that plan</h1><p>Planning agents keep a task list the model owns.</p>
<p>The list is re-injected every turn so it survives compaction.</p></article>
<footer>Copyright</footer></body></html>"""


class FakeInternet:
    """A fake transport: every request is answered from a script keyed by URL, and every one is recorded."""

    def __init__(self, responses: dict[str, httpx.Response]) -> None:
        self.responses = responses
        self.requested: list[str] = []

    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self._answer)

    def _answer(self, request: httpx.Request) -> httpx.Response:
        self.requested.append(str(request.url))
        return self.responses[str(request.url)]


async def _public(host: str) -> tuple[str, ...]:
    """A resolver answering with one public address, so no test here ever asks the real DNS."""
    del host
    return ("93.184.216.34",)


async def test_a_redirect_to_a_private_address_is_refused_and_never_requested() -> None:
    internet = FakeInternet(
        {
            "https://blog.example.org/agents": httpx.Response(302, headers={"location": "http://127.0.0.1:8100/api/knowledge_bases"}),
            "http://127.0.0.1:8100/api/knowledge_bases": httpx.Response(200, text="secrets"),
        }
    )
    fetcher = HttpxPageFetcher(httpx.AsyncClient(transport=internet.transport()), resolve=_public)

    with pytest.raises(FetchError, match="not a public address"):
        await fetcher.fetch("https://blog.example.org/agents")

    assert internet.requested == ["https://blog.example.org/agents"], "the private hop is refused before it is requested"


async def test_a_file_scheme_is_refused_before_any_request() -> None:
    internet = FakeInternet({})
    fetcher = HttpxPageFetcher(httpx.AsyncClient(transport=internet.transport()), resolve=_public)

    with pytest.raises(FetchError, match="only http and https"):
        await fetcher.fetch("file:///etc/passwd")

    assert internet.requested == []


async def test_a_public_redirect_is_followed_to_its_page() -> None:
    internet = FakeInternet(
        {
            "https://blog.example.org/agents": httpx.Response(301, headers={"location": "https://blog.example.org/posts/agents"}),
            "https://blog.example.org/posts/agents": httpx.Response(200, text=HTML),
        }
    )
    fetcher = HttpxPageFetcher(httpx.AsyncClient(transport=internet.transport()), resolve=_public)

    text = await fetcher.fetch("https://blog.example.org/agents")

    assert "Planning agents" in text
    assert internet.requested == ["https://blog.example.org/agents", "https://blog.example.org/posts/agents"]


@pytest.mark.parametrize("url", ["http://127.0.0.1/x", "http://169.254.169.254/latest/meta-data", "http://10.1.2.3/x", "http://[::1]/x"])
async def test_the_guard_refuses_loopback_link_local_and_private_hosts_by_address(url: str) -> None:
    with pytest.raises(FetchError, match="not a public address"):
        await refuse_unfetchable(url, _public)


async def test_the_guard_refuses_a_host_that_resolves_into_the_private_network() -> None:
    async def resolves_inward(host: str) -> tuple[str, ...]:
        del host
        return ("192.168.1.10",)

    with pytest.raises(FetchError, match="not a public address"):
        await refuse_unfetchable("https://intranet.example.org/x", resolves_inward)


async def test_the_guard_lets_a_public_page_through() -> None:
    await refuse_unfetchable("https://blog.example.org/agents", _public)
