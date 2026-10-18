"""Fetch a URL as text or bytes, refusing one that resolves to a private or internal address.

Any fetch of a URL an outsider chose goes through this one guarded fetcher.
Every redirect hop is checked the same way as the first URL.
"""

from __future__ import annotations

import asyncio
import ipaddress
import socket

from typing import Protocol
from urllib.parse import urlsplit

import httpx

MAX_REDIRECTS = 5
"""How many hops one fetch follows before it gives up. Each hop is checked like the first URL."""


class FetchError(Exception):
    """Raised when a URL, or somewhere it redirects to, may not be fetched, or the fetch itself fails."""


class PageFetcher(Protocol):
    """The one network call a URL-driven conversion or scan makes."""

    async def fetch(self, url: str) -> str:
        """The response body for `url`, decoded as text.

        Args:
            url: The URL to fetch.

        Returns:
            The body as text.

        Raises:
            FetchError: The URL, or somewhere it redirects to, may not be fetched.
        """
        ...


class ByteFetcher(Protocol):
    """A download of a file a connector reads whole, such as an archive."""

    async def fetch_bytes(self, url: str, *, max_bytes: int) -> bytes:
        """The response body for `url`, as bytes, at most `max_bytes` long.

        Args:
            url: The URL to fetch.
            max_bytes: The most bytes the body may hold.

        Returns:
            The body.

        Raises:
            FetchError: The URL, or somewhere it redirects to, may not be fetched, or the body passes `max_bytes`.
        """
        ...


class HostResolver(Protocol):
    """Every address one hostname stands for."""

    async def __call__(self, host: str) -> tuple[str, ...]:
        """The addresses `host` resolves to, as strings.

        Args:
            host: The hostname to resolve.

        Returns:
            Each address the host stands for, as a string.

        Raises:
            OSError: The name does not resolve.
        """
        ...


async def resolve_host(host: str) -> tuple[str, ...]:
    """The addresses `host` stands for, through the system resolver, off the event loop thread.

    Args:
        host: The hostname to resolve.

    Returns:
        Each distinct address, as a string.

    Raises:
        OSError: The name does not resolve.
    """
    infos = await asyncio.get_running_loop().getaddrinfo(host, None, proto=socket.IPPROTO_TCP)
    return tuple({str(info[4][0]) for info in infos})


async def refuse_unfetchable(url: str, resolve: HostResolver = resolve_host) -> None:
    """Refuse a URL this process must not fetch: a non-web scheme, or a host inside this machine or network.

    Only `http` and `https` are fetched, and only when every address the host stands for is a
    public one.

    Args:
        url: The URL about to be fetched, or a redirect's destination.
        resolve: How a hostname becomes addresses. An address literal never reaches it.

    Raises:
        FetchError: The scheme is not `http`/`https`, the URL names no host, the host does not
            resolve, or it resolves to a loopback, link-local, private or otherwise non-public address.
    """
    parts = urlsplit(url)
    if parts.scheme not in {"http", "https"}:
        msg = f"refusing {url}: only http and https pages are fetched"
        raise FetchError(msg)
    host = parts.hostname
    if not host:
        msg = f"refusing {url}: it names no host"
        raise FetchError(msg)
    for address in await _addresses(url, host, resolve):
        if not address.is_global:
            msg = f"refusing {url}: {host} is {address}, which is not a public address"
            raise FetchError(msg)


async def _addresses(url: str, host: str, resolve: HostResolver) -> tuple[ipaddress.IPv4Address | ipaddress.IPv6Address, ...]:
    """`host` as addresses: an address literal stands for itself, a name goes through `resolve`."""
    try:
        return (ipaddress.ip_address(host),)
    except ValueError:
        pass
    try:
        resolved = await resolve(host)
    except OSError as error:
        msg = f"refusing {url}: {host} does not resolve ({error})"
        raise FetchError(msg) from error
    try:
        return tuple(ipaddress.ip_address(address) for address in resolved)
    except ValueError as error:
        msg = f"refusing {url}: {host} resolved to something that is not an address ({error})"
        raise FetchError(msg) from error


class HttpxPageFetcher:
    """A `PageFetcher` over httpx that follows redirects one hop at a time and checks each hop.

    Each hop is checked before its request goes out. A fetch has a bounded timeout.
    """

    def __init__(self, client: httpx.AsyncClient | None = None, *, timeout: float = 20.0, resolve: HostResolver = resolve_host) -> None:
        """Build the fetcher.

        Args:
            client: The client to use. Without one, each fetch opens a fresh client.
            timeout: The timeout of each request, in seconds.
            resolve: How a hostname becomes addresses.
        """
        self._client = client
        self._timeout = timeout
        self._resolve = resolve

    async def fetch(self, url: str) -> str:
        """GET `url` and return its text.

        Args:
            url: The URL to fetch.

        Returns:
            The body of the final response, as text.

        Raises:
            FetchError: `url`, or a URL it redirects to, may not be fetched, or the chain is
                longer than `MAX_REDIRECTS`.
            httpx.HTTPStatusError: The final response has a status outside 2xx.
        """
        if self._client is not None:
            return await self._follow(self._client, url)
        async with httpx.AsyncClient(follow_redirects=False, timeout=self._timeout) as client:
            return await self._follow(client, url)

    async def fetch_bytes(self, url: str, *, max_bytes: int) -> bytes:
        """GET `url` and return its body as bytes, reading no more than `max_bytes`.

        Args:
            url: The URL to fetch.
            max_bytes: The most bytes the body may hold.

        Returns:
            The body of the final response.

        Raises:
            FetchError: `url`, or a URL it redirects to, may not be fetched, the chain is longer than
                `MAX_REDIRECTS`, or the body passes `max_bytes`.
            httpx.HTTPStatusError: The final response has a status outside 2xx.
        """
        if self._client is not None:
            return await self._follow_bytes(self._client, url, max_bytes)
        async with httpx.AsyncClient(follow_redirects=False, timeout=self._timeout) as client:
            return await self._follow_bytes(client, url, max_bytes)

    async def _follow_bytes(self, client: httpx.AsyncClient, url: str, max_bytes: int) -> bytes:
        """Walk the redirect chain as `_follow` does, then stream the final body up to `max_bytes`."""
        current = url
        for _ in range(MAX_REDIRECTS + 1):
            await refuse_unfetchable(current, self._resolve)
            response = await client.send(client.build_request("GET", current, timeout=self._timeout), stream=True, follow_redirects=False)
            try:
                location = response.headers.get("location") if response.is_redirect else None
                if location is None:
                    response.raise_for_status()
                    return await _read_capped(response, url, max_bytes)
            finally:
                await response.aclose()
            current = str(httpx.URL(current).join(location))
        msg = f"refusing {url}: more than {MAX_REDIRECTS} redirects"
        raise FetchError(msg)

    async def _follow(self, client: httpx.AsyncClient, url: str) -> str:
        """Walk the redirect chain, checking each hop before requesting it."""
        current = url
        for _ in range(MAX_REDIRECTS + 1):
            await refuse_unfetchable(current, self._resolve)
            response = await client.get(current, follow_redirects=False, timeout=self._timeout)
            location = response.headers.get("location") if response.is_redirect else None
            if location is None:
                response.raise_for_status()
                return response.text
            current = str(httpx.URL(current).join(location))
        msg = f"refusing {url}: more than {MAX_REDIRECTS} redirects"
        raise FetchError(msg)


async def _read_capped(response: httpx.Response, url: str, max_bytes: int) -> bytes:
    """The streamed body of `response`, refused once it passes `max_bytes`."""
    chunks: list[bytes] = []
    total = 0
    async for chunk in response.aiter_bytes():
        total += len(chunk)
        if total > max_bytes:
            msg = f"refusing {url}: the body passes {max_bytes} bytes"
            raise FetchError(msg)
        chunks.append(chunk)
    return b"".join(chunks)
