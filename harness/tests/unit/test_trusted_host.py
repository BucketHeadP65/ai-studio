"""The app answers only a `Host` of localhost or 127.0.0.1, so a page on a rebound hostname cannot call it from a browser."""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from tests.support.container import a_client, an_app

if TYPE_CHECKING:
    from collections.abc import AsyncIterator
    from pathlib import Path

    import httpx

PREFLIGHT = {"Origin": "http://localhost:3031", "Access-Control-Request-Method": "GET"}


@pytest.fixture
async def client(tmp_path: Path) -> AsyncIterator[httpx.AsyncClient]:
    # No lifespan: the host check is middleware, answered before any route or session exists.
    async with a_client(an_app(state=tmp_path)) as opened:
        yield opened


async def test_a_foreign_host_is_refused(client: httpx.AsyncClient) -> None:
    response = await client.options("/api/library", headers={"Host": "evil.example.com", **PREFLIGHT})

    assert response.status_code == 400


@pytest.mark.parametrize("host", ["localhost", "127.0.0.1", "localhost:8770", "127.0.0.1:8770"])
async def test_localhost_and_127_0_0_1_are_answered(client: httpx.AsyncClient, host: str) -> None:
    response = await client.options("/api/library", headers={"Host": host, **PREFLIGHT})

    assert response.status_code == 200
