"""Runner sessions as a first-class kind: the projected column and the HTTP filter.

The projection folds the event's `session_kind` (defaulting a legacy line to `chat`) into a real
column. The HTTP layer maps `kind` on the
wire to `session_kind` on the event and the column: `POST /api/sessions` mints one,
`GET /api/sessions` filters by it. Every test runs against the isolated `projection_pool`, never
the developer's live projection: `rebuild` truncates whatever pool it is handed.
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import TYPE_CHECKING, cast

import pytest

from core.ai.workflows.contracts import StepRegistry
from pydantic_ai_harness.step_persistence import InMemoryStepStore

from harness import session as session_state
from harness.adapters.session_log import SessionLog
from harness.adapters.store import ScheduleDatabase
from harness.domain.events import SessionStarted
from harness.features.sessions import project_registry, service
from harness.features.sessions.infrastructure.projects_log import ProjectsLog
from harness.features.sessions.internal_contracts import ProjectionPool, Sessions
from harness.features.sessions.service import ProjectedIndex, rebuild
from harness.session import Runtime, SessionPool
from tests.support.container import IO_LEAVES, a_client, an_app
from tests.support.fakes.pools import FakeScheduleDatabase

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator, Iterator

    import httpx

    from core.ai.workflows.contracts import WorkflowLibrary
    from pydantic_ai import Agent
    from pydantic_ai.tools import DeferredToolRequests

from tests.support.state import a_log_under, a_throwaway_state

SESSION = "44444444-4444-4444-4444-444444444444"


@pytest.fixture(autouse=True)
def _fresh_registry(tmp_path: Path) -> Iterator[None]:
    """Every test gets its own registry file -- never the real checkout's."""
    project_registry.install(ProjectsLog(tmp_path / "registry.jsonl"))
    yield
    project_registry._REGISTRY = None


async def test_rebuild_projects_session_kind_from_the_event(tmp_path: Path, projection_pool: ProjectionPool) -> None:
    session_log = SessionLog(tmp_path / "session-log.jsonl", topics=None)
    session_log.record(SessionStarted(session_id=SESSION, session_kind="runner"))

    await rebuild(projection_pool, session_log.events(), InMemoryStepStore(), project_root=tmp_path)

    row = await projection_pool.fetchrow("SELECT session_kind FROM sessions WHERE id = $1", SESSION)
    assert row is not None
    assert row["session_kind"] == "runner"


async def test_rebuild_projects_a_legacy_session_started_line_as_chat(tmp_path: Path, projection_pool: ProjectionPool) -> None:
    journal_path = tmp_path / "session-log.jsonl"
    journal_path.write_text(
        f'{{"seq": 1, "at": "2026-08-22T12:00:00+00:00", "kind": "session_started", "session_id": "{SESSION}"}}\n'
    )

    await rebuild(projection_pool, SessionLog(journal_path, topics=None).events(), InMemoryStepStore(), project_root=tmp_path)

    row = await projection_pool.fetchrow("SELECT session_kind FROM sessions WHERE id = $1", SESSION)
    assert row is not None
    assert row["session_kind"] == "chat", "an old line with no session_kind at all projects as chat"


def _default_runtime() -> Runtime:
    """The process's own runtime, narrating into a session log of its own so a minted session projects."""
    state = a_throwaway_state()
    return Runtime(
        registry=StepRegistry(),
        library=cast("WorkflowLibrary", None),
        agent=cast("Agent[Runtime, str | DeferredToolRequests]", object()),
        session_log=a_log_under(state),
        state=state,
    )


@asynccontextmanager
async def _api(pg_pool: ProjectionPool) -> AsyncGenerator[tuple[httpx.AsyncClient, SessionPool, ProjectionPool]]:
    """The real ASGI app over the isolated projection, its lifespan entered so the controllers mount.

    The projection is wiped after the lifespan has run, so the boot sweep's own rows are gone by the
    time a test seeds its own.
    """
    pool = SessionPool(_default_runtime(), projects=ProjectedIndex(pg_pool))
    leaves: dict[type, object] = {
        **dict(IO_LEAVES),
        ProjectionPool: pg_pool,
        ScheduleDatabase: FakeScheduleDatabase(),
        SessionPool: pool,
        Sessions: pool,
    }
    with TemporaryDirectory() as state:
        app = an_app(state=Path(state))
        with app.state.wireup_container.override(leaves):
            async with app.router.lifespan_context(app):
                await pg_pool.execute("TRUNCATE projects, sessions, turns, messages, flags")
                project_registry.install(ProjectsLog(Path(state) / "registry.jsonl"))
                async with a_client(app) as client:
                    yield client, pool, pg_pool


async def _sync_refresh(pg_pool: ProjectionPool, runtime: Runtime) -> None:
    """Bring `runtime`'s own projected row up to date, awaited rather than fired-and-forgotten."""
    await service.refresh_live(
        pg_pool,
        session_state.timeline(runtime),
        runtime.store,
        project_root=session_state.project_root(runtime),
        only_session_id=runtime.session_id,
    )


async def test_a_runner_session_is_absent_from_the_default_listing_but_present_under_all_and_runner(
    projection_pool: ProjectionPool,
) -> None:
    async with _api(projection_pool) as (client, pool, pg_pool):
        created = await client.post("/api/sessions", json={"kind": "runner"})
        assert created.status_code == 200
        body = created.json()["session"]
        assert body["kind"] == "runner"
        session_id = body["id"]
        await _sync_refresh(pg_pool, await pool.get(session_id))

        default_listing = await client.get("/api/sessions")
        assert session_id not in {row["id"] for row in default_listing.json()["sessions"]}, "the default listing hides runners"

        all_listing = await client.get("/api/sessions", params={"kind": "all"})
        all_kinds = {row["id"]: row["kind"] for row in all_listing.json()["sessions"]}
        assert all_kinds.get(session_id) == "runner"

        runner_listing = await client.get("/api/sessions", params={"kind": "runner"})
        runner_rows = runner_listing.json()["sessions"]
        assert session_id in {row["id"] for row in runner_rows}
        assert all(row["kind"] == "runner" for row in runner_rows)


async def test_a_default_kind_session_is_chat_and_shows_in_the_default_listing(projection_pool: ProjectionPool) -> None:
    async with _api(projection_pool) as (client, pool, pg_pool):
        created = await client.post("/api/sessions", json={})
        assert created.status_code == 200
        body = created.json()["session"]
        assert body["kind"] == "chat"
        session_id = body["id"]
        await _sync_refresh(pg_pool, await pool.get(session_id))

        default_listing = await client.get("/api/sessions")
        default_kinds = {row["id"]: row["kind"] for row in default_listing.json()["sessions"]}
        assert default_kinds.get(session_id) == "chat"
