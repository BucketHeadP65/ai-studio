"""Driving one controller over HTTP without composing a whole application.

A controller's routes are declared on an unbound class, and an application only binds them to an
instance once its lifespan has run. A test about what a route ANSWERS on the wire -- a status code,
a path the closed set refuses -- wants the binding and none of the composition, so `mounted` does
exactly what the lifespan does: clone the router, point every handler at this controller.
"""

from __future__ import annotations

from copy import deepcopy
from typing import TYPE_CHECKING, Protocol

import httpx

from fastapi import FastAPI
from fastapi.routing import APIRoute, APIWebSocketRoute
from httpx import ASGITransport

if TYPE_CHECKING:
    from fastapi import APIRouter


class HasRouter(Protocol):
    """A controller: an object whose class declares the router its methods are routes on."""

    router: APIRouter


def mounted(controller: HasRouter) -> FastAPI:
    """A bare application serving `controller`'s routes, bound to that instance.

    Args:
        controller: The controller under test, built with whatever collaborators the test scripted.

    Returns:
        An application carrying that controller's routes and nothing else.
    """
    app = FastAPI()
    router = deepcopy(type(controller).router)
    for route in router.routes:
        if isinstance(route, (APIRoute, APIWebSocketRoute)):
            route.endpoint = getattr(controller, route.endpoint.__name__)
            route.dependant.call = route.endpoint
    app.include_router(router)
    return app


def a_client_of(controller: HasRouter) -> httpx.AsyncClient:
    """An async client over `controller`'s routes alone."""
    return httpx.AsyncClient(transport=ASGITransport(app=mounted(controller)), base_url="http://test")
