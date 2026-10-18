"""Building an application whose container reaches nothing, so a route test needs no network."""

from __future__ import annotations

from contextlib import asynccontextmanager, contextmanager
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import TYPE_CHECKING

import httpx

from core.ai.history.contracts import RequestParametersSink
from core.cqrs.contracts import Broadcaster, Wake
from core.observability import PriceUpdater, Tracing
from core.settings import DatabaseSettings, InfrastructureSettings, LogfireSettings
from fastapi import FastAPI
from fastapi.testclient import TestClient
from httpx import ASGITransport
from pydantic_ai_harness.step_persistence import InMemoryStepStore

from harness.adapters.logfire_query import Telemetry
from harness.adapters.model_list import ModelList
from harness.adapters.store import DatabaseBinding, RecordDatabase, ScheduleDatabase, SessionsDatabase
from harness.domain.ports import ConversationHistory, SessionTranscript
from harness.features.sessions.contracts import project_id_of
from harness.features.sessions.internal_contracts import ProjectionPool
from harness.main import create_app
from harness.session import ProjectIndex, Runtime
from harness.settings import CHECKOUT, HarnessSettings, StateSettings
from tests.support.fakes.broadcaster import FakeBroadcaster
from tests.support.fakes.history import FakeConversationHistory
from tests.support.fakes.model_listing import FakeModelListing
from tests.support.fakes.pools import FakePool, FakeRecordDatabase, FakeScheduleDatabase, FakeSessionsDatabase
from tests.support.fakes.prices import FakePriceUpdater
from tests.support.fakes.projects import FakeProjectIndex
from tests.support.fakes.request_parameters import FakeRequestParametersSink
from tests.support.fakes.studio_registry import FakeStudioApi
from tests.support.fakes.telemetry import FakeTelemetry
from tests.support.fakes.tracing import FakeTracing
from tests.support.fakes.wake import FakeWake
from tests.support.runtime import a_runtime

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator, Generator

    from wireup import AsyncContainer

A_PROJECT = project_id_of(CHECKOUT)
"""The project id this checkout's own root gets. The one project every deployment has, so a route
that resolves a project reaches its own tree rather than an id nothing knows."""

AN_INFRASTRUCTURE = InfrastructureSettings(
    database=DatabaseSettings(url="postgresql+asyncpg://owner:pw@db.example:5432/kb", reader_url="postgresql+asyncpg://reader:pw@db.example:5432/kb"),
    logfire=LogfireSettings(base_url="https://telemetry.example", read_token="a-token"),
)
"""What answers for the shared settings file's infrastructure section, which is this machine's own and is not in the repository.

Every provider that reads a section of it still runs: only the file read is replaced, so a test on any checkout reads
the same values.
"""

IO_LEAVES: tuple[tuple[type, object], ...] = (
    (Broadcaster, FakeBroadcaster()),
    (Runtime, a_runtime()),
    (SessionsDatabase, FakeSessionsDatabase()),
    (RecordDatabase, FakeRecordDatabase()),
    (SessionTranscript, InMemoryStepStore()),
    (ConversationHistory, FakeConversationHistory()),
    (RequestParametersSink, FakeRequestParametersSink()),
    (ProjectionPool, FakePool()),
    (ScheduleDatabase, FakeScheduleDatabase()),
    (ProjectIndex, FakeProjectIndex({A_PROJECT: CHECKOUT})),
    (DatabaseBinding, DatabaseBinding(description="two databases nobody opened", named=True)),
    (Wake, FakeWake()),
    (httpx.AsyncClient, FakeStudioApi()),
    (Telemetry, FakeTelemetry()),
    (ModelList, ModelList(listing=FakeModelListing())),
    (InfrastructureSettings, AN_INFRASTRUCTURE),
    (Tracing, FakeTracing()),
    (PriceUpdater, FakePriceUpdater()),
)
"""Every registration that would otherwise reach a network or change this process, and what answers
for it instead.

A registration that reaches outside the process and is not named here opens a real socket, or reads
a file this checkout does not carry, the first time a route test resolves it. `DatabaseBinding` and
`Wake` are on the list for the second reason: both are built over engram's own settings file, which
says which databases this process opens and which one carries the wake channel. `StudioApi` is not on the list: it is the alias factory
over `httpx.AsyncClient`, so overriding the client answers for both. `ModelList` is overridden
rather than `ModelListing`, because the container serves the merged list and its remote half is
`ModelListing | None`, which is not a registration key. `SessionTranscript` is on the list for a
third reason: the real one is the record, and a route test wants a store that answers rather than a
fake pool recording the record's statements. `RecordDatabase` is on it for the first: without it the
first route test to enter a lifespan opens the record's own pool onto `aifactory_sessions`.
"""


def rooted_at(settings: HarnessSettings, state: Path) -> HarnessSettings:
    """The same settings with their state home pointed at `state`, so no test writes into the real one."""
    return settings.model_copy(update={"state": StateSettings(home=state)})


@contextmanager
def answering_from_memory(container: AsyncContainer) -> Generator[None]:
    """Replace every registration that would otherwise reach a network, for the duration of the block."""
    with container.override(dict(IO_LEAVES)):
        yield


def an_app(settings: HarnessSettings | None = None, *, state: Path) -> FastAPI:
    """One application of this test's own, with its state home under `state` and nothing shared.

    The lifespan is not entered: what this answers is the routes as composed, which is what a route
    test that scripts its own collaborators wants. `serving` is the one that enters it.
    """
    return create_app(rooted_at(settings if settings is not None else HarnessSettings(), state))


def a_client(app: FastAPI) -> httpx.AsyncClient:
    """An async client over `app`, without entering its lifespan, under a `Host` the app answers."""
    return httpx.AsyncClient(transport=ASGITransport(app=app), base_url="http://localhost")


@asynccontextmanager
async def a_client_of_its_own() -> AsyncGenerator[httpx.AsyncClient]:
    """A client over an application whose state home is a directory living exactly as long as the client.

    For a caller with no `tmp_path` to hand -- a module-level context manager, or a block inside one
    test -- so that composing an application never means writing into the real state home.
    """
    with TemporaryDirectory() as state:
        async with a_client(an_app(state=Path(state))) as opened:
            yield opened


def app_of(client: TestClient) -> FastAPI:
    """The application `client` drives, as the type it is rather than as any ASGI callable.

    Raises:
        TypeError: the client was built over something other than this application.
    """
    application = client.app
    if not isinstance(application, FastAPI):  # pragma: no cover - every client here is built over one
        msg = "this client was not built over a FastAPI application"
        raise TypeError(msg)
    return application


@contextmanager
def serving(settings: HarnessSettings, *, state: Path) -> Generator[TestClient]:
    """A client over a freshly composed application whose container reaches nothing."""
    built: FastAPI = create_app(rooted_at(settings, state))
    with answering_from_memory(built.state.wireup_container), TestClient(built, base_url="http://localhost", raise_server_exceptions=False) as client:
        yield client
