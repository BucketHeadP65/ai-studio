"""The knowledge base HTTP API that AI Factory Studio calls.

`create_app` builds the container, mounts every controller in `CONTROLLERS` over it, and puts a resolver on `app.state.resolver`.
Each controller carries the contract of the routes it serves.
Every other way into a knowledge base is not HTTP: the `studio` CLI for operations, and the MCP server and the in-process capability for agents.
The app answers only a `Host` of localhost or 127.0.0.1, and a browser only from AI Factory Studio's origin.
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from typing import TYPE_CHECKING

import wireup.integration.fastapi as wireup_fastapi

from core.cqrs.contracts import Broadcaster, JobQueries, Jobs, JobsAndQueries
from core.observability import get_logger
from core.wiring.contracts import ContainerResolver
from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from starlette.middleware.trustedhost import TrustedHostMiddleware
from wireup import AsyncContainer

from studio.knowledge_base.container import create_container, dispose_engines
from studio.knowledge_base.http.api_models import error_response
from studio.knowledge_base.http.controllers.events import EventsController
from studio.knowledge_base.http.controllers.graph import GraphController
from studio.knowledge_base.http.controllers.knowledge_bases import KnowledgeBasesController
from studio.knowledge_base.http.controllers.notes import NotesController
from studio.knowledge_base.http.controllers.refresh import RefreshController
from studio.knowledge_base.http.controllers.templates import TemplatesController
from studio.knowledge_base.http.controllers.watch_list import WatchListController
from studio.knowledge_base.http.exceptions import KnowledgeBaseRefusal, KnowledgeBaseRefusedError
from studio.knowledge_base.settings import ApplicationSettings, SettingsFile, default_settings_path

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator
    from pathlib import Path

    from core.supervisor import SupervisedChild

_logger = get_logger(__name__)

# The browser client's two origins, the same port by name and by address. CORS refuses a client served from anywhere else.
_AI_FACTORY_STUDIO_ORIGINS = ("http://localhost:3031", "http://127.0.0.1:3031")

# The only `Host` names answered. Without this check, a page on a rebound hostname can call the API from a browser.
_ALLOWED_HOSTS = ("localhost", "127.0.0.1")

KNOWLEDGE_BASE_REFUSAL_STATUS: dict[KnowledgeBaseRefusal, int] = {
    KnowledgeBaseRefusal.SCHEMA_AND_TEMPLATE: 400,
    KnowledgeBaseRefusal.UNKNOWN_TEMPLATE: 400,
    KnowledgeBaseRefusal.SCHEMA_OR_TEMPLATE_REQUIRED: 400,
    KnowledgeBaseRefusal.HIDDEN_DIRECTORY: 400,
    KnowledgeBaseRefusal.NAME_COLLECTION_MISMATCH: 400,
    KnowledgeBaseRefusal.INVALID_PATH: 400,
    KnowledgeBaseRefusal.SOURCES_WITHOUT_QUEUE: 400,
    KnowledgeBaseRefusal.KNOWLEDGE_BASE_REQUIRED: 400,
    KnowledgeBaseRefusal.UNKNOWN_SCANNER: 400,
    KnowledgeBaseRefusal.UNKNOWN_KNOWLEDGE_BASE: 404,
    KnowledgeBaseRefusal.SCHEMA_NOT_FOUND: 404,
    KnowledgeBaseRefusal.UNKNOWN_JOB: 404,
    KnowledgeBaseRefusal.UNKNOWN_PAGE: 404,
    KnowledgeBaseRefusal.NAME_COLLISION: 409,
    KnowledgeBaseRefusal.PATH_COLLISION: 409,
    KnowledgeBaseRefusal.DIRECTORY_NOT_EMPTY: 409,
    KnowledgeBaseRefusal.BROKEN_KNOWLEDGE_BASE: 409,
    KnowledgeBaseRefusal.SOURCE_QUEUE_UNREADABLE: 409,
    KnowledgeBaseRefusal.WATCH_LIST_UNREADABLE: 409,
    KnowledgeBaseRefusal.NOTHING_TO_RETRY: 409,
    KnowledgeBaseRefusal.INVALID_DECISION: 409,
    KnowledgeBaseRefusal.TEMPLATE_SCHEMA_UNREADABLE: 500,
    KnowledgeBaseRefusal.REGISTRY_WRITE_REFUSED: 500,
}
"""How each refusal of the registry of knowledge bases reads over HTTP. Exhaustive over the refusals, so a new refusal is a new entry here."""


CONTROLLERS: tuple[type, ...] = (
    EventsController,
    GraphController,
    KnowledgeBasesController,
    NotesController,
    RefreshController,
    TemplatesController,
    WatchListController,
)
"""Every controller this application mounts. A new route group is one more entry."""


def create_app(
    settings: ApplicationSettings,
    *,
    container: AsyncContainer | None = None,
    settings_path: Path | None = None,
    jobs: JobsAndQueries | None = None,
    worker: SupervisedChild | None = None,
    broadcaster: Broadcaster | None = None,
) -> FastAPI:
    """Assemble the knowledge base application: a container, the controllers over it, and the `/api` frame.

    This is the only place application state is created. Calling it twice yields two independent application and container pairs.

    Args:
        settings: Fully constructed application settings. This function never reads the environment.
        container: A pre-built container, such as one whose IO leaves are overridden. One is built from `settings` when it is omitted.
        settings_path: The settings file the registry of knowledge bases is read from and written to. It is `default_settings_path()`
            when omitted.
        jobs: The queue and history of jobs that refreshes are enqueued on and read from. The container's Postgres store serves when it
            is omitted.
        worker: The worker process to keep alive for as long as this app serves. `None` runs the API alone.
        broadcaster: The change channel this app streams from. A caller that passes one starts and stops it. When it is omitted, one
            is built on the settings database, started with the app and stopped on shutdown.

    Returns:
        The application.
    """
    resolved_container = container if container is not None else create_container(settings)
    resolver = ContainerResolver(resolved_container)
    resolved_container.override.set(SettingsFile, SettingsFile(path=settings_path if settings_path is not None else default_settings_path()))
    if jobs is not None:
        resolved_container.override.set(JobsAndQueries, jobs)
        resolved_container.override.set(Jobs, jobs)
        resolved_container.override.set(JobQueries, jobs)

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncGenerator[None]:
        """Start the change channel and the worker, and dispose the engines on the way out."""
        owns_broadcaster = broadcaster is None
        channel = broadcaster if broadcaster is not None else Broadcaster(settings.infrastructure.database.url)
        if owns_broadcaster:
            await channel.start()
        app.state.broadcaster = channel
        if worker is not None:
            worker.start()
        _logger.info("knowledge base API ready")
        try:
            yield
        finally:
            if worker is not None:
                await worker.stop()
            if owns_broadcaster:
                await channel.stop()
            await dispose_engines(resolved_container)

    # With FastAPI's own telemetry on, every request is traced, metered and logged into the process's providers, the
    # change stream and the client's polls included.
    app = FastAPI(
        title="AI Factory Studio knowledge base API",
        description="The registry of knowledge bases: their notes, refreshes, source queues and watch lists, and the change stream.",
        lifespan=lifespan,
        telemetry={"tracing": False, "metrics": False, "logs": False},
    )
    app.state.resolver = resolver
    app.add_middleware(CORSMiddleware, allow_origins=list(_AI_FACTORY_STUDIO_ORIGINS), allow_credentials=True, allow_methods=["*"], allow_headers=["*"])
    app.add_middleware(TrustedHostMiddleware, allowed_hosts=list(_ALLOWED_HOSTS))
    _add_exception_handlers(app)
    wireup_fastapi.setup(resolved_container, app, class_based_handlers=list(CONTROLLERS))
    return app


def _add_exception_handlers(app: FastAPI) -> None:
    """Map every failure to an `{error, detail}` response, never a traceback over the wire."""

    @app.exception_handler(Exception)
    async def _catch_all(_request: Request, exc: Exception) -> JSONResponse:
        _logger.exception("unhandled error serving the knowledge base API")
        return error_response(500, "internal_error", str(exc))

    @app.exception_handler(RequestValidationError)
    async def _invalid_request(_request: Request, exc: RequestValidationError) -> JSONResponse:
        return error_response(422, "invalid_request", str(exc))

    @app.exception_handler(KnowledgeBaseRefusedError)
    async def _refused(_request: Request, exc: KnowledgeBaseRefusedError) -> JSONResponse:
        return error_response(KNOWLEDGE_BASE_REFUSAL_STATUS[exc.refusal], exc.refusal.value, str(exc))
