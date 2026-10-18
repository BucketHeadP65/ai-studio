"""What the HTTP test modules share: a full settings file, a fake change channel, and raw streaming.

Both `test_http.py` and `test_routes.py` build the same application, so what they share lives here
rather than in one of them, and neither imports the other.
"""

from __future__ import annotations

import asyncio
import contextlib
import json

from dataclasses import dataclass
from typing import TYPE_CHECKING

from core.cqrs.contracts import Broadcaster, MemoryJobs
from engram.retrieval.queries import KnowledgeBasesHandler, KnowledgeBaseValidationHandler
from engram.testing.fakes.gateway import FakeSqlGateway

from studio.knowledge_base.container import create_container
from studio.knowledge_base.http.api import create_app
from studio.knowledge_base.settings import (
    ApplicationSettings,
    AzureOpenAiSettings,
    DatabaseSettings,
    DocumentIntelligenceSettings,
    EvalsSettings,
    GatewaySettings,
    HarnessServiceSettings,
    InfrastructureSettings,
    IngestionSettings,
    LogfireSettings,
    RetrievalSettings,
    SourceSettings,
)
from studio.knowledge_base.wiring.contracts import AdminSqlGateway, ReaderSqlGateway
from tests.support.fakes.topics import FakeListener, FakeNotifier

if TYPE_CHECKING:
    from pathlib import Path

    from fastapi import FastAPI
    from pydantic import JsonValue
    from starlette.types import Message, Scope
    from wireup import AsyncContainer

INFRASTRUCTURE_JSON: dict[str, JsonValue] = {
    "database": {
        "url": "postgresql+asyncpg://knowledge_bases@db/knowledge_bases",
        "reader_url": "postgresql+asyncpg://kb_reader@db/knowledge_bases",
    },
    "azure_openai": {"endpoint": "https://example.openai.azure.com/"},
    "document_intelligence": {"endpoint": ""},
    "logfire": {"write_token": "", "read_token": "", "base_url": "https://logfire-eu.pydantic.dev"},
    "gateway": {"api_key": "", "base_url": "https://gateway-eu.pydantic.dev/proxy"},
    "openrouter": {"api_key": ""},
    "harness": {"base_url": "http://127.0.0.1:8770", "timeout_seconds": 10.0},
}
"""The `infrastructure` section of `SETTINGS_JSON`, importable on its own for a test overriding one leaf."""

SETTINGS_JSON: dict[str, JsonValue] = {
    "knowledge_bases": {},
    "retrieval": {"dialect": "pg_textsearch", "graph_backend": "age"},
    "ingestion": {"embedding_deployment": "text-embed", "embedding_dimensions": 1024, "embedding_batch_size": 64},
    "sources": {},
    "evals": {"autogen": "queue", "datasets_dir": "evals/datasets", "rca_queries_path": "evals/rca/queries.sql"},
    "infrastructure": INFRASTRUCTURE_JSON,
}
"""A settings file carrying every section, over a database nothing in a unit test connects to."""


def write_settings_json(path: Path, knowledge_bases: dict[str, dict[str, JsonValue]] | None = None) -> Path:
    """Write a full settings file, with `knowledge_bases` as its registry, and hand back its path."""
    path.write_text(json.dumps(SETTINGS_JSON | {"knowledge_bases": knowledge_bases or {}}), encoding="utf-8")
    return path


def fake_broadcaster() -> Broadcaster:
    """A broadcaster over fake connections, so entering a lifespan opens no database."""
    return Broadcaster("postgresql://u:p@h/db", notifier=FakeNotifier(), listener=FakeListener())


def build_app(tmp_path: Path) -> FastAPI:
    """The served application, built the way production builds it minus the two IO leaves."""
    tmp_path.mkdir(parents=True, exist_ok=True)
    settings_path = write_settings_json(tmp_path / "application_settings.json")
    return create_app(
        ApplicationSettings(json_file=settings_path), settings_path=settings_path, jobs=MemoryJobs(), broadcaster=fake_broadcaster()
    )


async def raw_stream(app: FastAPI, path: str) -> tuple[Message, asyncio.Queue[Message], asyncio.Task[None]]:
    """Call the app over raw ASGI, and hand back the start message, the body queue, and its task.

    `httpx`'s ASGI transport collects a whole response before handing any of it back, which never
    happens for a stream this route only ever closes on a client disconnect. Driving the app
    directly is what lets a test read one frame and then walk away. The `Host` header names a
    `TrustedHostMiddleware`-allowed host.
    """
    root, _, query = path.partition("?")
    scope: Scope = {
        "type": "http",
        "asgi": {"version": "3.0", "spec_version": "2.4"},
        "http_version": "1.1",
        "method": "GET",
        "headers": [(b"host", b"localhost")],
        "scheme": "http",
        "path": root,
        "raw_path": root.encode(),
        "query_string": query.encode(),
        "server": ("localhost", 80),
        "client": ("test", 123),
        "root_path": "",
    }
    body: asyncio.Queue[Message] = asyncio.Queue()

    async def receive() -> Message:
        return {"type": "http.disconnect"}

    async def send(message: Message) -> None:
        await body.put(message)

    task = asyncio.create_task(app(scope, receive, send))
    start = await asyncio.wait_for(body.get(), timeout=2)
    return start, body, task


async def stop_stream(task: asyncio.Task[None]) -> None:
    """Stop driving the app, since a change stream never ends on its own."""
    task.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await task


@dataclass(frozen=True, slots=True)
class AppWiring:
    """A real container with its database leaves replaced, and the fake gateway behind them.

    The routes resolve through the same container production builds, so a key nothing registers
    fails here the way it would fail in the running app. `gateway` is the one seam a test scripts:
    the rows every read comes back with.
    """

    container: AsyncContainer
    gateway: FakeSqlGateway


def hermetic_settings() -> ApplicationSettings:
    """A settings tree carrying every section, over a database nothing connects to."""
    return ApplicationSettings(
        retrieval=RetrievalSettings(),
        ingestion=IngestionSettings(embedding_deployment="text-embed"),
        sources=SourceSettings({}),
        evals=EvalsSettings(),
        infrastructure=InfrastructureSettings(
            database=DatabaseSettings(
                url="postgresql+asyncpg://knowledge_bases@db/knowledge_bases", reader_url="postgresql+asyncpg://kb_reader@db/knowledge_bases"
            ),
            azure_openai=AzureOpenAiSettings(endpoint="https://example.openai.azure.com/"),
            document_intelligence=DocumentIntelligenceSettings(),
            logfire=LogfireSettings(),
            gateway=GatewaySettings(),
            harness=HarnessServiceSettings(),
        ),
    )


def wiring() -> AppWiring:
    """A container over hermetic settings, with every read of Postgres answered by one fake gateway."""
    gateway = FakeSqlGateway()
    container = create_container(hermetic_settings())
    container.override.set(ReaderSqlGateway, gateway)
    container.override.set(AdminSqlGateway, gateway)
    container.override.set(KnowledgeBasesHandler, KnowledgeBasesHandler(gateway))
    container.override.set(KnowledgeBaseValidationHandler, KnowledgeBaseValidationHandler(gateway))
    return AppWiring(container=container, gateway=gateway)
