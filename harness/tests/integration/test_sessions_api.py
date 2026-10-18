"""The session read API and the project lifecycle, against a real Postgres and a real pool.

Every session-listing test seeds a session log and rebuilds the projection exactly as
`test_sessions_projection.py` does; every session-answering test drives a real `SessionPool` so
`answering` is asserted off the pool's own lock, never a stored column. Every project test drives
the real ASGI app so a caller sees exactly what a browser would.
"""

from __future__ import annotations

import json
import shutil

from contextlib import asynccontextmanager
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import TYPE_CHECKING, cast

import pytest

from core.ai.workflows.contracts import StepRegistry
from pydantic_ai.messages import ModelRequest, ModelResponse, TextPart, UserPromptPart
from pydantic_ai.usage import RequestUsage
from pydantic_ai_harness.step_persistence import ContinuableSnapshot, InMemoryStepStore

from harness import session as session_state
from harness.adapters.session_log import SessionLog
from harness.adapters.store import ScheduleDatabase
from harness.domain.events import ConversationForgotten, FrictionFlagged, SessionArchived, SessionStarted, TurnSettled, TurnStarted
from harness.features.sessions import project_registry, service
from harness.features.sessions.infrastructure.projects_log import ProjectsLog
from harness.features.sessions.internal_contracts import ProjectionPool, Sessions
from harness.features.sessions.service import ProjectedIndex, rebuild
from harness.session import Runtime, SessionPool
from harness.settings import CHECKOUT
from tests.support.container import IO_LEAVES, a_client, an_app
from tests.support.fakes.pools import FakeScheduleDatabase
from tests.support.state import a_log_under, a_throwaway_state, recorded

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator, Iterator

    import httpx

    from core.ai.workflows.contracts import WorkflowLibrary
    from pydantic_ai import Agent
    from pydantic_ai.tools import DeferredToolRequests

ALPHA = "11111111-1111-1111-1111-111111111111"
BETA = "22222222-2222-2222-2222-222222222222"
SESSION = "33333333-3333-3333-3333-333333333333"
UNKNOWN = "99999999-9999-9999-9999-999999999999"
PROJECTS = "/api/projects"


@pytest.fixture(autouse=True)
def _fresh_registry(tmp_path: Path) -> Iterator[None]:
    """Every test gets its own registry file -- never the real checkout's."""
    project_registry.install(ProjectsLog(tmp_path / "registry.jsonl"))
    yield
    project_registry._REGISTRY = None


def _default_runtime() -> Runtime:
    """A process's own runtime for these tests: nothing here ever calls its agent or its store.

    It narrates into a session log of its own, so a session minted through the pool is one the
    projection can read back and the listing routes can answer with.
    """
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

    The projection is wiped and the registry re-opened empty after the lifespan has run, so the
    boot sweep's own rows and the checkout the boot registers are not mistaken for the test's own.
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
    """Bring `runtime`'s own projected rows up to date, awaited rather than fired-and-forgotten."""
    await service.refresh_live(
        pg_pool,
        session_state.timeline(runtime),
        runtime.store,
        project_root=session_state.project_root(runtime),
        only_session_id=runtime.session_id,
    )


def _row(response: httpx.Response, session_id: str) -> dict[str, object]:
    return next(row for row in response.json()["sessions"] if row["id"] == session_id)


async def test_listing_hides_archived_by_default_and_shows_them_with_the_flag(tmp_path: Path, projection_pool: ProjectionPool) -> None:
    session_log = SessionLog(tmp_path / "session-log.jsonl", topics=None)
    session_log.record(SessionStarted(session_id=ALPHA))
    session_log.record(TurnStarted(entry="spa", chars=3, session_id=ALPHA))
    session_log.record(TurnSettled(run_id="run-a"))
    session_log.record(SessionStarted(session_id=BETA))
    session_log.record(TurnStarted(entry="spa", chars=3, session_id=BETA))
    session_log.record(TurnSettled(run_id="run-b"))
    session_log.record(SessionArchived(session_id=BETA, archived=True))

    async with _api(projection_pool) as (client, _pool, pg_pool):
        await rebuild(pg_pool, session_log.events(), InMemoryStepStore(), project_root=tmp_path)

        default = await client.get("/api/sessions")
        ids_default = {row["id"] for row in default.json()["sessions"]}
        assert ALPHA in ids_default
        assert BETA not in ids_default

        with_archived = await client.get("/api/sessions", params={"archived": "true"})
        ids_all = {row["id"] for row in with_archived.json()["sessions"]}
        assert {ALPHA, BETA} <= ids_all


async def test_answering_reflects_the_pools_lock_not_a_stored_column(projection_pool: ProjectionPool) -> None:
    async with _api(projection_pool) as (client, pool, pg_pool):
        runtime = await pool.create(None)
        session_id = runtime.session_id
        assert session_id is not None
        await _sync_refresh(pg_pool, runtime)

        idle = await client.get("/api/sessions")
        assert _row(idle, session_id)["answering"] is False

        hold = await pool.begin_turn(session_id)
        try:
            busy = await client.get("/api/sessions")
            assert _row(busy, session_id)["answering"] is True
        finally:
            hold.release()

        freed = await client.get("/api/sessions")
        assert _row(freed, session_id)["answering"] is False


async def test_detail_returns_turns_in_seq_order(tmp_path: Path, projection_pool: ProjectionPool) -> None:
    session_log = SessionLog(tmp_path / "session-log.jsonl", topics=None)
    session_log.record(SessionStarted(session_id=SESSION))
    session_log.record(TurnStarted(entry="spa", chars=1, session_id=SESSION))
    session_log.record(TurnSettled(run_id="run-1"))
    session_log.record(TurnStarted(entry="spa", chars=1, session_id=SESSION))
    session_log.record(TurnSettled(run_id="run-2"))
    session_log.record(TurnStarted(entry="spa", chars=1, session_id=SESSION))
    session_log.record(TurnSettled(run_id="run-3"))

    async with _api(projection_pool) as (client, _pool, pg_pool):
        await rebuild(pg_pool, session_log.events(), InMemoryStepStore(), project_root=tmp_path)

        detail = await client.get(f"/api/sessions/{SESSION}")
        missing = await client.get(f"/api/sessions/{UNKNOWN}")

        assert detail.status_code == 200
        body = detail.json()
        assert body["session"]["id"] == SESSION
        assert [turn["run_id"] for turn in body["turns"]] == ["run-1", "run-2", "run-3"]
        assert [turn["seq"] for turn in body["turns"]] == sorted(turn["seq"] for turn in body["turns"])
        assert missing.status_code == 404


async def test_transcript_returns_payload_and_text(tmp_path: Path, projection_pool: ProjectionPool) -> None:
    session_log = SessionLog(tmp_path / "session-log.jsonl", topics=None)
    session_log.record(SessionStarted(session_id=SESSION))
    session_log.record(TurnStarted(entry="spa", chars=5, session_id=SESSION))
    session_log.record(TurnSettled(run_id="run-1"))
    store = InMemoryStepStore()
    request = ModelRequest(parts=[UserPromptPart(content="hello there")])
    response = ModelResponse(parts=[TextPart(content="hi back")], usage=RequestUsage(input_tokens=1, output_tokens=1))
    await store.save_snapshot(ContinuableSnapshot(run_id="run-1", step_index=0, messages=[request, response]))

    async with _api(projection_pool) as (client, _pool, pg_pool):
        await rebuild(pg_pool, session_log.events(), store, project_root=tmp_path)

        transcript = await client.get(f"/api/sessions/{SESSION}/transcript")

        assert transcript.status_code == 200
        messages = transcript.json()["messages"]
        assert [message["kind"] for message in messages] == ["request", "response"]
        assert messages[0]["text"] == "hello there"
        assert messages[1]["text"] == "hi back"
        assert isinstance(messages[0]["payload"], dict)
        assert "parts" in messages[0]["payload"]
        assert messages[1]["run_id"] == "run-1"


_OLD_ENVELOPE = json.dumps(
    {"message": "the prose", "spec_yaml": "name: x\nversion: 1\n", "title": "t", "summary": "s", "applicability": "a"}
)
"""The reply shape every turn recorded before replies were plain prose."""


async def test_transcript_serves_an_old_envelope_as_the_prose_it_carried(tmp_path: Path, projection_pool: ProjectionPool) -> None:
    """A reply the SPA reads back must be words, whichever side of the prose change recorded it.

    Both halves of a response row are read: `text` is what search and the turn inspector show, and
    the payload's text part is what the chat bubble renders. Prose recorded after the change comes
    back exactly as it was said, and so does the person's own prompt.
    """
    session_log = SessionLog(tmp_path / "session-log.jsonl", topics=None)
    session_log.record(SessionStarted(session_id=SESSION))
    session_log.record(TurnStarted(entry="spa", chars=5, session_id=SESSION))
    session_log.record(TurnSettled(run_id="run-1"))
    store = InMemoryStepStore()
    usage = RequestUsage(input_tokens=1, output_tokens=1)
    await store.save_snapshot(
        ContinuableSnapshot(
            run_id="run-1",
            step_index=0,
            messages=[
                ModelRequest(parts=[UserPromptPart(content="draft me one")]),
                ModelResponse(parts=[TextPart(content=_OLD_ENVELOPE)], usage=usage),
                ModelRequest(parts=[UserPromptPart(content="and again")]),
                ModelResponse(parts=[TextPart(content="plain prose, untouched")], usage=usage),
            ],
        )
    )

    async with _api(projection_pool) as (client, _pool, pg_pool):
        await rebuild(pg_pool, session_log.events(), store, project_root=tmp_path)

        transcript = await client.get(f"/api/sessions/{SESSION}/transcript")

        assert transcript.status_code == 200
        messages = transcript.json()["messages"]
        replies = [message for message in messages if message["kind"] == "response"]
        prompts = [message for message in messages if message["kind"] == "request"]
        assert [message["text"] for message in replies] == ["the prose", "plain prose, untouched"]
        assert [part["content"] for message in replies for part in message["payload"]["parts"] if part["part_kind"] == "text"] == [
            "the prose",
            "plain prose, untouched",
        ]
        assert [message["text"] for message in prompts] == ["draft me one", "and again"]


async def test_detail_and_transcript_honor_the_forget_watermark(tmp_path: Path, projection_pool: ProjectionPool) -> None:
    """After a Forget, the detail plugin's turns and the transcript plugin's messages start over.

    Two turns, a Forget, then one more turn -- the same shape `forget_session` steps on the
    session log. Both plugins must agree with the session summary: one turn, one run's messages.
    """
    session_log = SessionLog(tmp_path / "session-log.jsonl", topics=None)
    session_log.record(SessionStarted(session_id=SESSION))
    started_1 = recorded(session_log, TurnStarted(entry="spa", chars=5, session_id=SESSION))
    session_log.record(TurnSettled(run_id="run-before-1", started_seq=started_1.seq))
    started_2 = recorded(session_log, TurnStarted(entry="spa", chars=5, session_id=SESSION))
    session_log.record(TurnSettled(run_id="run-before-2", started_seq=started_2.seq))
    session_log.record(ConversationForgotten(session_id=SESSION))
    started_3 = recorded(session_log, TurnStarted(entry="spa", chars=5, session_id=SESSION))
    session_log.record(TurnSettled(run_id="run-after", started_seq=started_3.seq))

    store = InMemoryStepStore()
    await store.save_snapshot(
        ContinuableSnapshot(
            run_id="run-after",
            step_index=0,
            messages=[
                ModelRequest(parts=[UserPromptPart(content="are we still talking")]),
                ModelResponse(parts=[TextPart(content="yes, fresh start")], usage=RequestUsage(input_tokens=3, output_tokens=2)),
            ],
        )
    )

    async with _api(projection_pool) as (client, _pool, pg_pool):
        await rebuild(pg_pool, session_log.events(), store, project_root=tmp_path)

        detail = await client.get(f"/api/sessions/{SESSION}")
        transcript = await client.get(f"/api/sessions/{SESSION}/transcript")

        assert detail.status_code == 200
        body = detail.json()
        assert body["session"]["turn_count"] == 1
        assert [turn["run_id"] for turn in body["turns"]] == ["run-after"]

        assert transcript.status_code == 200
        messages = transcript.json()["messages"]
        assert {message["run_id"] for message in messages} == {"run-after"}
        assert [message["text"] for message in messages] == ["are we still talking", "yes, fresh start"]


async def test_flags_filter_by_kind_target_and_session(tmp_path: Path, projection_pool: ProjectionPool) -> None:
    session_log = SessionLog(tmp_path / "session-log.jsonl", topics=None)
    session_log.record(SessionStarted(session_id=SESSION))
    session_log.record(TurnStarted(entry="spa", chars=1, session_id=SESSION))
    session_log.record(TurnSettled(run_id="run-1"))
    session_log.record(
        FrictionFlagged(target_kind="tool", target="shell", note="slow", tool_call_id=None, source="live", subject_session_id=None)
    )
    session_log.record(
        FrictionFlagged(target_kind="skill", target="writing", note="vague", tool_call_id=None, source="live", subject_session_id=None)
    )

    async with _api(projection_pool) as (client, _pool, pg_pool):
        await rebuild(pg_pool, session_log.events(), InMemoryStepStore(), project_root=tmp_path)

        by_kind = await client.get("/api/flags", params={"target_kind": "tool"})
        by_session = await client.get("/api/flags", params={"session_id": SESSION})
        by_target = await client.get("/api/flags", params={"target": "writing"})

        assert [flag["target"] for flag in by_kind.json()["flags"]] == ["shell"]
        assert len(by_session.json()["flags"]) == 2
        assert [flag["target"] for flag in by_target.json()["flags"]] == ["writing"]


async def test_search_returns_only_matching_sessions_ranked(tmp_path: Path, projection_pool: ProjectionPool) -> None:
    session_log = SessionLog(tmp_path / "session-log.jsonl", topics=None)
    session_log.record(SessionStarted(session_id=ALPHA))
    session_log.record(TurnStarted(entry="spa", chars=5, session_id=ALPHA))
    session_log.record(TurnSettled(run_id="run-alpha"))
    session_log.record(SessionStarted(session_id=BETA))
    session_log.record(TurnStarted(entry="spa", chars=5, session_id=BETA))
    session_log.record(TurnSettled(run_id="run-beta"))
    store = InMemoryStepStore()
    await store.save_snapshot(
        ContinuableSnapshot(run_id="run-alpha", step_index=0, messages=[ModelRequest(parts=[UserPromptPart(content="tell me about zephyrwood")])])
    )
    await store.save_snapshot(
        ContinuableSnapshot(run_id="run-beta", step_index=0, messages=[ModelRequest(parts=[UserPromptPart(content="what is the weather today")])])
    )

    async with _api(projection_pool) as (client, _pool, pg_pool):
        await rebuild(pg_pool, session_log.events(), store, project_root=tmp_path)

        found = await client.get("/api/sessions", params={"q": "zephyrwood"})

        assert [row["id"] for row in found.json()["sessions"]] == [ALPHA]


async def test_rename_archive_and_delete_flow_end_to_end(projection_pool: ProjectionPool) -> None:
    async with _api(projection_pool) as (client, pool, pg_pool):
        runtime = await pool.create(None)
        session_id = runtime.session_id
        assert session_id is not None
        await _sync_refresh(pg_pool, runtime)

        renamed = await client.patch(f"/api/sessions/{session_id}", json={"title": "Nightly sweep"})
        assert renamed.status_code == 200
        assert renamed.json()["session"]["title"] == "Nightly sweep"

        archived = await client.patch(f"/api/sessions/{session_id}", json={"archived": True})
        assert archived.status_code == 200
        assert archived.json()["session"]["archived"] is True

        listing_default = await client.get("/api/sessions")
        assert session_id not in {row["id"] for row in listing_default.json()["sessions"]}

        deleted = await client.delete(f"/api/sessions/{session_id}")
        assert deleted.status_code == 204

        gone = await client.get(f"/api/sessions/{session_id}")
        assert gone.status_code == 404


async def test_delete_refuses_while_the_session_is_answering(projection_pool: ProjectionPool) -> None:
    async with _api(projection_pool) as (client, pool, _pg_pool):
        runtime = await pool.create(None)
        session_id = runtime.session_id
        assert session_id is not None

        hold = await pool.begin_turn(session_id)
        try:
            refused = await client.delete(f"/api/sessions/{session_id}")
            assert refused.status_code == 409
        finally:
            hold.release()


async def test_patch_and_delete_404_for_an_unknown_session(projection_pool: ProjectionPool) -> None:
    async with _api(projection_pool) as (client, _pool, _pg_pool):
        patched = await client.patch(f"/api/sessions/{UNKNOWN}", json={"title": "nope"})
        deleted = await client.delete(f"/api/sessions/{UNKNOWN}")

        assert patched.status_code == 404
        assert deleted.status_code == 404


async def test_register_project_refuses_a_root_that_is_not_a_directory(tmp_path: Path, projection_pool: ProjectionPool) -> None:
    async with _api(projection_pool) as (client, _pool, _pg_pool):
        response = await client.post(PROJECTS, json={"root_path": str(tmp_path / "nope")})

        assert response.status_code == 400


async def test_register_list_patch_and_delete_a_project(tmp_path: Path, projection_pool: ProjectionPool) -> None:
    root = tmp_path / "client-repo"
    root.mkdir()

    async with _api(projection_pool) as (client, _pool, _pg_pool):
        registered = await client.post(PROJECTS, json={"root_path": str(root)})
        assert registered.status_code == 200
        project = registered.json()["project"]
        assert project["root_path"] == str(root.resolve())
        assert project["missing"] is False
        assert project["running_sessions"] == 0
        project_id = project["id"]

        listing = await client.get(PROJECTS)
        ids = {row["id"] for row in listing.json()["projects"]}
        assert project_id in ids
        assert service.project_id_of(CHECKOUT) in ids, "the checkout is always its own project row"

        renamed = await client.patch(f"{PROJECTS}/{project_id}", json={"name": "Renamed Repo"})
        assert renamed.status_code == 200
        assert renamed.json()["project"]["name"] == "Renamed Repo"

        shutil.rmtree(root)
        after_removal = await client.get(PROJECTS)
        row = next(r for r in after_removal.json()["projects"] if r["id"] == project_id)
        assert row["missing"] is True

        deleted = await client.delete(f"{PROJECTS}/{project_id}")
        assert deleted.status_code == 204

        after_delete = await client.get(PROJECTS)
        assert project_id not in {r["id"] for r in after_delete.json()["projects"]}, "unregistered projects are no longer offered"


async def test_running_sessions_counts_only_answering_ones(tmp_path: Path, projection_pool: ProjectionPool) -> None:
    root = tmp_path / "client-repo"
    root.mkdir()

    async with _api(projection_pool) as (client, pool, _pg_pool):
        registered = await client.post(PROJECTS, json={"root_path": str(root)})
        project_id = registered.json()["project"]["id"]

        opened = await client.post("/api/sessions", json={"project_id": project_id})
        assert opened.status_code == 200
        session_id = opened.json()["session"]["id"]

        idle = await client.get(PROJECTS)
        idle_row = next(r for r in idle.json()["projects"] if r["id"] == project_id)
        assert idle_row["running_sessions"] == 0

        hold = await pool.begin_turn(session_id)
        try:
            busy = await client.get(PROJECTS)
            busy_row = next(r for r in busy.json()["projects"] if r["id"] == project_id)
            assert busy_row["running_sessions"] == 1
        finally:
            hold.release()


async def test_delete_project_refuses_while_one_of_its_sessions_is_answering(tmp_path: Path, projection_pool: ProjectionPool) -> None:
    root = tmp_path / "client-repo"
    root.mkdir()

    async with _api(projection_pool) as (client, pool, _pg_pool):
        registered = await client.post(PROJECTS, json={"root_path": str(root)})
        project_id = registered.json()["project"]["id"]
        opened = await client.post("/api/sessions", json={"project_id": project_id})
        session_id = opened.json()["session"]["id"]

        hold = await pool.begin_turn(session_id)
        try:
            refused = await client.delete(f"{PROJECTS}/{project_id}")
            assert refused.status_code == 409
        finally:
            hold.release()


async def test_delete_project_404_for_a_never_registered_id(projection_pool: ProjectionPool) -> None:
    async with _api(projection_pool) as (client, _pool, _pg_pool):
        response = await client.delete(f"{PROJECTS}/{UNKNOWN}")

        assert response.status_code == 404
        assert response.json()["detail"] == "no project with that id here"
        assert project_registry.current().events() == (), "a never-registered id must not record an unregister"


def test_register_drops_a_default_config_and_is_idempotent_on_root(tmp_path: Path) -> None:
    root = tmp_path / "client-repo"
    root.mkdir()

    first = project_registry.register(root, project_id="pid-1")
    second = project_registry.register(root, project_id="pid-1")

    assert (root / ".aifactory" / "config.json").exists()
    assert first == second
    assert [project.project_id for project in project_registry.registered_projects()] == ["pid-1"]


def test_set_display_name_preserves_other_config_keys(tmp_path: Path) -> None:
    root = tmp_path / "client-repo"
    (root / ".aifactory").mkdir(parents=True)
    (root / ".aifactory" / "config.json").write_text(json.dumps({"skills": ["docs"]}), encoding="utf-8")

    project_registry.set_display_name(root, "Renamed")

    payload = json.loads((root / ".aifactory" / "config.json").read_text(encoding="utf-8"))
    assert payload == {"skills": ["docs"], "name": "Renamed"}


def test_auto_register_checkout_never_opens_a_project_workspace(tmp_path: Path) -> None:
    fake_checkout = tmp_path / "harness-checkout"
    fake_checkout.mkdir()

    project_registry.auto_register_checkout(fake_checkout)
    project_registry.auto_register_checkout(fake_checkout)  # idempotent: still one entry

    assert (fake_checkout / ".aifactory" / "config.json").exists()
    assert not (fake_checkout / ".aifactory" / "state").exists()
    assert len(project_registry.registered_projects()) == 1


def test_boot_installs_the_registry_and_registers_the_checkout(tmp_path: Path) -> None:
    checkout = tmp_path / "checkout"
    checkout.mkdir()

    project_registry.boot(checkout)

    assert (checkout / ".state" / "projects-log.jsonl").exists()
    ids = {project.project_id for project in project_registry.registered_projects()}
    assert service.project_id_of(checkout) in ids


if __name__ == "__main__":  # pragma: no cover
    pytest.main([__file__])
