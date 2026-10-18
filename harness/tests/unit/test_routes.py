"""What this application serves: the pinned set, no pair declared twice, and every controller answering."""

from __future__ import annotations

import re

from http import HTTPStatus
from pathlib import Path
from typing import TYPE_CHECKING

import pytest

from fastapi.routing import APIWebSocketRoute
from starlette.websockets import WebSocketDisconnect

from harness.features.terminal.controller import FEATURE_OFF, POLICY_VIOLATION
from harness.settings import HarnessSettings
from tests.support.container import A_PROJECT, app_of, serving
from tests.unit import route_baseline
from tests.unit.route_baseline import ROUTE_BASELINE

if TYPE_CHECKING:
    from collections.abc import Iterator, Sequence

    from fastapi import FastAPI
    from starlette.routing import BaseRoute

BASELINE_ROW = re.compile(r'^\s*\("([A-Z]+)", "([^"]+)"\),$')
"""One pinned pair as the baseline file writes it, so its own order can be read back."""

IGNORED_METHODS = frozenset({"HEAD", "OPTIONS"})

CHEAPEST_ROUTE = [
    ("AuthoringController", "GET", "/api/conversation"),
    ("BrowsingController", "GET", "/api/steps"),
    ("InspectionController", "GET", "/api/inspection/findings"),
    ("ProjectsController", "GET", f"/api/projects/{A_PROJECT}/files"),
    ("RunningController", "GET", "/api/pending"),
    ("SchedulingController", "GET", "/api/schedules"),
    ("SelfAuthoringController", "GET", "/api/harness/plugins"),
    ("SessionsController", "GET", "/api/sessions"),
]
"""One route per controller, the cheapest each one has. A controller whose graph cannot be resolved
fails here rather than at the first request in the browser. The terminal controller is not on the
list: its one route is a websocket, and `TestTheTerminalSocketIsStillGated` below drives it instead."""


def baseline_rows() -> list[tuple[str, str]]:
    """Every pinned pair in the order the file writes it.

    Read off the source, because a frozenset keeps no order at all and the file's own is the only
    one a reader ever sees.
    """
    text = Path(route_baseline.__file__).read_text(encoding="utf-8")
    matched = (BASELINE_ROW.match(line) for line in text.splitlines())
    return [(str(found.group(1)), str(found.group(2))) for found in matched if found is not None]


def unwrapped(route: object) -> object:
    """What actually carries a route's path and methods.

    A controller's routes reach the application inside an included router, whose candidates each
    wrap the route they stand for -- or carry the path and the methods themselves and leave the
    wrapped route `None`, which is what an HTTP candidate does.
    """
    return getattr(route, "starlette_route", None) or route


def websocket_routes(routes: Sequence[BaseRoute]) -> Iterator[tuple[str, str]]:
    """Every websocket path this application matches, walked because they carry no OpenAPI entry."""
    for route in routes:
        nested = getattr(route, "effective_candidates", None)
        wrapped = unwrapped(route)
        if isinstance(wrapped, APIWebSocketRoute):
            yield ("WEBSOCKET", wrapped.path)
        elif nested is not None:
            yield from websocket_routes(nested())


def declared_routes(routes: Sequence[BaseRoute]) -> list[tuple[str, str]]:
    """Every `(method, path)` this application will match, one entry per declaration.

    Walked rather than read off the OpenAPI schema, which is a mapping and so cannot show the same
    pair twice: a pair declared by two controllers shows up twice here.
    """
    pairs: list[tuple[str, str]] = []
    for route in routes:
        nested = getattr(route, "effective_candidates", None)
        if nested is not None:
            pairs.extend(declared_routes(nested()))
            continue
        wrapped = unwrapped(route)
        if isinstance(wrapped, APIWebSocketRoute):
            pairs.append(("WEBSOCKET", wrapped.path))
            continue
        path, methods = getattr(wrapped, "path", None), getattr(wrapped, "methods", None)
        if path and methods:
            pairs.extend((method, path) for method in methods if method not in IGNORED_METHODS)
    return pairs


def served_routes(application: FastAPI) -> set[tuple[str, str]]:
    """Every `(method, path)` pair the application answers, websockets included."""
    application.openapi_schema = None
    schema = application.openapi()
    documented = {
        (method.upper(), path)
        for path, operations in schema["paths"].items()
        for method in operations
        if method.upper() not in IGNORED_METHODS
    }
    return documented | set(websocket_routes(application.routes))


class TestTheServedRoutes:
    def test_the_served_set_is_the_one_the_client_knows(self, tmp_path: Path) -> None:
        with serving(HarnessSettings(), state=tmp_path) as client:
            assert served_routes(app_of(client)) == ROUTE_BASELINE

    def test_no_method_and_path_pair_is_declared_twice(self, tmp_path: Path) -> None:
        with serving(HarnessSettings(), state=tmp_path) as client:
            declared = declared_routes(app_of(client).routes)

        assert sorted(declared) == sorted(set(declared))

    def test_the_baseline_was_actually_built(self) -> None:
        assert len(ROUTE_BASELINE) == 115

    def test_the_baseline_is_written_in_order(self) -> None:
        """Method then path: a new route lands where a reader looks for it, never at the end of a block."""
        rows = baseline_rows()

        assert len(rows) == len(ROUTE_BASELINE), "every pinned pair was read back off the file"
        assert rows == sorted(rows)


class TestEveryControllerAnswersOnItsCheapestRoute:
    @pytest.mark.parametrize(("controller", "method", "path"), CHEAPEST_ROUTE)
    def test_one_request_reaches_the_route(self, controller: str, method: str, path: str, tmp_path: Path) -> None:
        with serving(HarnessSettings(), state=tmp_path) as client:
            response = client.request(method, path)

        assert response.status_code == HTTPStatus.OK, f"{controller} answered {response.status_code}"


class TestTheTerminalSocketIsStillGated:
    def test_it_refuses_when_the_feature_is_off(self, tmp_path: Path) -> None:
        """The first gate, named: the socket has four, and the other three would refuse this too."""
        with (
            serving(HarnessSettings(), state=tmp_path) as client,
            pytest.raises(WebSocketDisconnect) as closed,
            client.websocket_connect("ws://localhost/api/terminal/ws?cwd=/tmp"),
        ):
            pass

        assert closed.value.code == POLICY_VIOLATION
        assert closed.value.reason == FEATURE_OFF
