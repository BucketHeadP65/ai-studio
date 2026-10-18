"""A real Temporal server for the durability tier, or a clean skip when there is none.

`WorkflowEnvironment.start_local` fetches and runs Temporal's dev server binary. That needs
the network the first time and a writable cache after it, so a machine with neither must skip
this tier rather than fail it -- the same bargain engram's testcontainers tests make with
Docker. Nothing here is mocked: a test in this directory that passes did so against a server
that wrote real workflow history.
"""

from __future__ import annotations

from typing import TYPE_CHECKING
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

import asyncpg
import pytest
import pytest_asyncio

from core.ai.history.contracts import PoolDatabase, create_record_schema
from core.ai.workflows.contracts import connect
from engram.testing.infrastructure.knowledge_base_db import (
    pg,  # noqa: F401 -- imported to be used as a pytest fixture, never called directly
)
from temporalio.testing import WorkflowEnvironment

from harness.adapters.store import RecordDatabase
from harness.adapters.wiring import open_record_pool
from harness.features.sessions.infrastructure.postgres import PostgresProjectionPool
from harness.features.sessions.infrastructure.schema import ensure_schema

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

    from temporalio.client import Client


@pytest_asyncio.fixture(scope="module", loop_scope="module")
async def temporal_env() -> AsyncIterator[WorkflowEnvironment]:
    """One dev server for the module. Skips the tier if it cannot be started."""
    try:
        env = await WorkflowEnvironment.start_local()
    except Exception as exc:  # no network, no cached binary, no port -- none of them are failures here
        pytest.skip(f"no local Temporal dev server available: {type(exc).__name__}: {exc}")
    try:
        yield env
    finally:
        await env.shutdown()


@pytest_asyncio.fixture(loop_scope="module")
async def temporal_client(temporal_env: WorkflowEnvironment) -> Client:
    """A client on the dev server, wired with `PydanticAIPlugin` exactly as production is.

    Reconnected through the adapter's own `connect` rather than handed `env.client`, because
    the payload converter is the thing under test as much as the workflow is: a run that only
    passes with the default converter has not shown that step types cross the boundary.
    """
    return await connect(temporal_env.client.service_client.config.target_host, namespace=temporal_env.client.namespace)


def as_asyncpg_dsn(url: str) -> str:
    """The container's URL spelled for asyncpg, saying out loud that this local server has no TLS.

    `sslmode` is the one term the pool openers read off a DSN, and a container that would refuse an
    SSL upgrade has to say so there rather than leave it to a default. Merged into whatever query
    the URL already carries, never appended to it.
    """
    parts = urlsplit(url.replace("postgresql+asyncpg://", "postgresql://", 1))
    terms = dict(parse_qsl(parts.query)) | {"sslmode": "disable"}
    return urlunsplit((parts.scheme, parts.netloc, parts.path, urlencode(terms), parts.fragment))


@pytest_asyncio.fixture
async def projection_pool(pg: str) -> AsyncIterator[PostgresProjectionPool]:  # noqa: F811 -- must keep this exact
    # parameter name: pytest resolves the imported `pg` fixture by its registered name (fixed at
    # its own definition), not by whatever local name imports it here.
    """A pool onto the shared integration-tier container -- never the developer's live projection.

    `aifactory_sessions` is an owner surface: `harness sessions rebuild` is how the owner
    recovers it after an accidental wipe, so a test suite that opened a pool onto it would be one
    `rebuild()` call away from truncating the owner's real session list. Every session test gets
    this pool instead, pointed at the disposable container `pg` starts. Isolation between tests is
    each test's own `rebuild`/`TRUNCATE` call, exactly as it was when every test shared the one
    live pool.
    """
    pool = await asyncpg.create_pool(as_asyncpg_dsn(pg))
    projected = PostgresProjectionPool(pool)
    await ensure_schema(projected)
    try:
        yield projected
    finally:
        await pool.close()


@pytest_asyncio.fixture
async def record_database(projection_pool: PostgresProjectionPool, pg: str) -> AsyncIterator[RecordDatabase]:  # noqa: F811 -- the
    # same rule the fixture above states: the parameter must keep the imported fixture's registered
    # name for pytest to resolve it.
    """The record's own pool onto the same container database, opened the way this app opens it.

    Both pools a deployment holds, side by side: the projection's on `public` and the record's born
    in the record's schema. A test that wants either one alone asks for that one.
    """
    pool = await open_record_pool(projection_pool.pool, dsn=as_asyncpg_dsn(pg))
    await create_record_schema(PoolDatabase(pool))
    try:
        yield RecordDatabase(pool)
    finally:
        await pool.close()
