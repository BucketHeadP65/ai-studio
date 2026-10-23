"""The `/api` surface the studio client calls, pinned as a set of (method, path) pairs.

The client is built against these exact pairs, so a route that moves, gains a prefix or loses one is
a break in the client rather than a refactor. Every served pair is listed here. Adding a route means
adding its pair, and removing one means removing it on purpose.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Protocol, runtime_checkable

import pytest

from core.cqrs.contracts import MemoryJobs
from core.wiring.contracts import ContainerResolver, resolver_of
from httpx import ASGITransport, AsyncClient

from studio.knowledge_base.http.api import CONTROLLERS, create_app
from studio.knowledge_base.settings import ApplicationSettings
from tests.support.http_app import build_app, fake_broadcaster, raw_stream, stop_stream, wiring, write_settings_json

if TYPE_CHECKING:
    from collections.abc import Iterable, Iterator, Sequence
    from pathlib import Path

    from fastapi import FastAPI
    from logfire.testing import CaptureLogfire

ROUTE_BASELINE: frozenset[tuple[str, str]] = frozenset(
    {
        ("DELETE", "/api/knowledge-bases/{name}/registration"),
        ("DELETE", "/api/knowledge-bases/{name}/schedule"),
        ("DELETE", "/api/knowledge-bases/{name}/watch-list"),
        ("GET", "/api/events"),
        ("GET", "/api/graph"),
        ("GET", "/api/jobs/{job_id}"),
        ("GET", "/api/knowledge-bases"),
        ("GET", "/api/knowledge-bases/{name}"),
        ("GET", "/api/knowledge-bases/{name}/decisions"),
        ("GET", "/api/knowledge-bases/{name}/refresh-runs"),
        ("GET", "/api/knowledge-bases/{name}/schema"),
        ("GET", "/api/templates"),
        ("PATCH", "/api/knowledge-bases/{name}/pages/{page_id}"),
        ("POST", "/api/knowledge-bases"),
        ("POST", "/api/knowledge-bases/{name}/notes"),
        ("POST", "/api/knowledge-bases/{name}/notes/preview"),
        ("POST", "/api/knowledge-bases/{name}/refresh"),
        ("POST", "/api/knowledge-bases/{name}/source-queue"),
        ("POST", "/api/knowledge-bases/{name}/source-queue/{key}/retry"),
        ("POST", "/api/knowledge-bases/{name}/watch-list"),
        ("PUT", "/api/knowledge-bases/{name}/registration"),
        ("PUT", "/api/knowledge-bases/{name}/schedule"),
    }
)


@runtime_checkable
class _IncludedRouter(Protocol):
    """A routes entry standing for a whole router included under the application."""

    def effective_candidates(self) -> Sequence[object]:
        """Every route that router contributes, each at the path it is served under."""
        ...


@runtime_checkable
class _ServedRoute(Protocol):
    """A routes entry that answers one path: the path it is served under and the methods it takes."""

    methods: set[str] | None
    path: str


def _entries(routes: Iterable[object]) -> Iterator[_ServedRoute]:
    """Every answering entry among `routes`, descending into the routers included under them.

    An included router keeps its routes under one entry of its own rather than flattening them
    onto the application, so a router mounted that way is reachable only through that entry.
    """
    for route in routes:
        if isinstance(route, _IncludedRouter):
            yield from _entries(route.effective_candidates())
        elif isinstance(route, _ServedRoute):
            yield route


def served_routes(app: FastAPI) -> list[tuple[str, str]]:
    """Every `/api` pair the application answers, methods sorted, `HEAD` and `OPTIONS` dropped."""
    return [
        (method, route.path)
        for route in _entries(app.routes)
        if route.path.startswith("/api")
        for method in sorted(route.methods or ())
        if method not in {"HEAD", "OPTIONS"}
    ]


class TestTheServedSurface:
    async def test_the_served_pairs_are_exactly_the_baseline(self, tmp_path: Path):
        app = build_app(tmp_path)

        async with app.router.lifespan_context(app):
            served = frozenset(served_routes(app))

        assert served == ROUTE_BASELINE

    async def test_no_pair_is_served_twice(self, tmp_path: Path):
        app = build_app(tmp_path)

        async with app.router.lifespan_context(app):
            served = served_routes(app)

        duplicates = sorted({pair for pair in served if served.count(pair) > 1})
        assert duplicates == []


def _app_over_fakes(tmp_path: Path) -> FastAPI:
    """The served application with its database leaves overridden, so a route answers off fakes."""
    tmp_path.mkdir(parents=True, exist_ok=True)
    settings_path = write_settings_json(tmp_path / "application_settings.json")
    fakes = wiring()
    return create_app(
        ApplicationSettings(json_file=settings_path),
        container=fakes.container,
        settings_path=settings_path,
        jobs=MemoryJobs(),
        broadcaster=fake_broadcaster(),
    )


class TestEveryControllerAnswers:
    """One request through each controller's cheapest route, with the database leaves overridden."""

    @pytest.mark.parametrize(
        ("method", "path", "status"),
        [
            ("GET", "/api/templates", 200),
            ("GET", "/api/knowledge-bases", 200),
            ("GET", "/api/graph", 400),
            ("GET", "/api/knowledge-bases/ghost/decisions", 404),
            ("GET", "/api/jobs/1", 404),
        ],
    )
    async def test_the_route_answers_without_touching_a_database(self, tmp_path: Path, method: str, path: str, status: int):
        app = _app_over_fakes(tmp_path)

        async with (
            AsyncClient(transport=ASGITransport(app=app, raise_app_exceptions=False), base_url="http://localhost") as client,
            app.router.lifespan_context(app),
        ):
            response = await client.request(method, path)

        assert response.status_code == status

    async def test_the_events_controller_opens_a_stream(self, tmp_path: Path):
        # Driven over raw ASGI and then cancelled: the change stream only ends on a disconnect, so a
        # buffering client would wait for a body that never completes.
        app = _app_over_fakes(tmp_path)

        async with app.router.lifespan_context(app):
            start, _, task = await raw_stream(app, "/api/events")
            try:
                assert start["status"] == 200
                assert dict(start["headers"])[b"content-type"].startswith(b"text/event-stream")
            finally:
                await stop_stream(task)

    async def test_a_request_records_no_span_of_fastapis_own(self, tmp_path: Path, capfire: CaptureLogfire):
        # capfire configures a real global tracer provider, which FastAPI's native telemetry records into when it is on.
        app = _app_over_fakes(tmp_path)

        async with (
            AsyncClient(transport=ASGITransport(app=app, raise_app_exceptions=False), base_url="http://localhost") as client,
            app.router.lifespan_context(app),
        ):
            response = await client.get("/api/templates")

        assert response.status_code == 200
        names = [str(span["name"]) for span in capfire.exporter.exported_spans_as_dict()]
        assert [name for name in names if name.startswith(("GET ", "fastapi."))] == [], names

    def test_every_controller_is_mounted(self):
        assert sorted(controller.__name__ for controller in CONTROLLERS) == [
            "EventsController",
            "GraphController",
            "KnowledgeBasesController",
            "NotesController",
            "RefreshController",
            "TemplatesController",
            "WatchListController",
        ]


class TestEachApplicationOwnsItsContainer:
    def test_two_applications_are_two_containers(self, tmp_path: Path):
        first = build_app(tmp_path / "first")
        second = build_app(tmp_path / "second")

        first_resolver = resolver_of(first)
        second_resolver = resolver_of(second)
        assert isinstance(first_resolver, ContainerResolver)
        assert isinstance(second_resolver, ContainerResolver)

        assert first.state.resolver.container is not second.state.resolver.container
        assert first_resolver.container is not second_resolver.container
