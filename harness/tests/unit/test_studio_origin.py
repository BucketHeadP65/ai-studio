"""The studio client is a different origin, and the browser asks permission on its behalf.

The Workflow studio lives in studio-client (Next.js on :3031) while this app serves :8770.
Same machine, different port -- which to a browser is a different origin, so every fetch the
studio makes is preflighted. Without an answer here the studio cannot read a single endpoint,
and the failure would be a wall of opaque `TypeError: Failed to fetch` in the console rather
than anything that names CORS. These tests pin the two answers that matter: the studio's dev
origin is allowed, and an arbitrary origin is not.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from starlette.middleware.cors import CORSMiddleware

from harness.settings import AI_FACTORY_STUDIO_ORIGINS
from tests.support.container import a_client, an_app

if TYPE_CHECKING:
    from collections.abc import AsyncIterator
    from pathlib import Path

    import httpx

PREFLIGHT = {"Access-Control-Request-Method": "GET"}


@pytest.fixture
async def client(tmp_path: Path) -> AsyncIterator[httpx.AsyncClient]:
    # No lifespan on purpose: CORS is middleware, answered before any route or session
    # exists, and booting the real environment (store connection, agent) to test it would tie
    # this to credentials the suite must not need.
    async with a_client(an_app(state=tmp_path)) as opened:
        yield opened


async def test_the_studio_dev_origin_may_read_the_api(client: httpx.AsyncClient) -> None:
    response = await client.options("/api/library", headers={"Origin": "http://localhost:3031", **PREFLIGHT})
    assert response.headers.get("access-control-allow-origin") == "http://localhost:3031"
    # The studio's fetch layer sends `credentials: "include"`, and a browser DISCARDS a
    # credentialed cross-origin response unless the server says allow-credentials — the
    # failure mode is a bare "Failed to fetch" that names neither CORS nor credentials.
    assert response.headers.get("access-control-allow-credentials") == "true"


async def test_the_loopback_spelling_of_the_same_origin_works_too(client: httpx.AsyncClient) -> None:
    """Localhost and 127.0.0.1 are one machine and two origins.

    Allowing one but not the other turns into a bug report that depends on which URL somebody
    happened to type.
    """
    response = await client.options("/api/library", headers={"Origin": "http://127.0.0.1:3031", **PREFLIGHT})
    assert response.headers.get("access-control-allow-origin") == "http://127.0.0.1:3031"


async def test_an_arbitrary_origin_is_not_invited(client: httpx.AsyncClient) -> None:
    response = await client.options("/api/library", headers={"Origin": "https://evil.example", **PREFLIGHT})
    assert response.headers.get("access-control-allow-origin") is None


async def test_cors_admits_exactly_the_origins_the_terminal_gate_reads(tmp_path: Path) -> None:
    """CORS and the terminal's origin gate read one list, so the two cannot drift apart."""
    app = an_app(state=tmp_path)
    cors = next(entry for entry in app.user_middleware if entry.cls is CORSMiddleware)

    assert cors.kwargs["allow_origins"] == list(AI_FACTORY_STUDIO_ORIGINS)
    async with a_client(app) as client:
        for origin in AI_FACTORY_STUDIO_ORIGINS:
            response = await client.options("/api/library", headers={"Origin": origin, **PREFLIGHT})
            assert response.headers.get("access-control-allow-origin") == origin
