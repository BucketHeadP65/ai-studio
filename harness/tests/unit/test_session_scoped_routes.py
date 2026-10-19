"""The session-scoped API: many sessions answering at once, and one turn each.

Every test drives the real app over ASGI with a pool installed, so what is asserted is what a
browser would see. The model is a `FunctionModel` that stops mid-answer on an `asyncio.Event`:
that is what makes "two sessions are answering right now" and "this session is mid-turn" facts a
test can hold still rather than race against.

The state paths are the suite's autouse `STATE` redirect, so the session log and the transcript a
hydration reads back are the ones the test itself seeded, with nothing patched.
"""

from __future__ import annotations

import asyncio
import json

from contextlib import asynccontextmanager
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any, cast

import pytest
import yaml

from core.ai.workflows.contracts import StepRegistry, WorkflowPresentation
from core.ai.workflows.infrastructure.folder import FolderLibrary
from pydantic_ai import Agent
from pydantic_ai.messages import ModelRequest, ModelResponse, TextPart, UserPromptPart
from pydantic_ai.models.function import FunctionModel
from pydantic_ai_harness.step_persistence import ContinuableSnapshot, RunRecord

from harness.adapters.session_log import SessionLog
from harness.domain.events import SessionStarted, TurnSettled, TurnStarted
from harness.features.authoring.domain import Draft
from harness.features.running import service
from harness.features.running.domain.progress import GateVerdict, PendingReview
from harness.features.running.infrastructure import run_events
from harness.features.sessions.controller import SessionsController, live_prompt_messages
from harness.features.sessions.service import SessionProjection
from harness.session import Runtime, SessionPool
from harness.workspace import open_workspace
from tests.support.fakes.broadcaster import FakeBroadcaster
from tests.support.fakes.conversation_store import FakeConversationStore
from tests.support.fakes.pools import FakePool
from tests.support.serving import a_served_client
from tests.support.state import a_session_log, a_state, a_transcript, recorded, records_under
from tests.support.streaming import submit

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator, AsyncIterator, Sequence
    from pathlib import Path

    import httpx

    from core.ai.workflows.contracts import Workflow
    from pydantic_ai.messages import ModelMessage
    from pydantic_ai.models.function import AgentInfo
    from pydantic_ai_harness.step_persistence import StepStore

    from harness.domain.ports import SessionTranscript

STORED = "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"
OTHER = "cccccccc-cccc-cccc-cccc-cccccccccccc"
UNKNOWN = "bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb"


class _Gate:
    """A streamed answer that stops half-written until the test lets it finish.

    `arrived` is set once `expected` turns are all waiting inside the model at the same moment,
    which is how a test asserts concurrency instead of hoping for it.
    """

    def __init__(self, *, expected: int = 1, released: bool = False) -> None:
        self.arrived = asyncio.Event()
        self.release = asyncio.Event()
        if released:
            self.release.set()
        self.waiting = 0
        self._expected = expected

    async def stream(self, messages: Sequence[ModelMessage], info: AgentInfo) -> AsyncIterator[str]:
        """Half the reply, then a wait, then the rest."""
        del messages, info
        text = "ok"
        yield text[:1]
        self.waiting += 1
        if self.waiting >= self._expected:
            self.arrived.set()
        await self.release.wait()
        yield text[1:]


def _gated_agent(gate: _Gate) -> Agent[Runtime, str]:
    """An agent whose every turn waits in `gate` before it finishes."""

    def function(messages: Sequence[ModelMessage], info: AgentInfo) -> ModelResponse:
        del messages, info
        return ModelResponse(parts=[TextPart("ok")])

    return Agent(FunctionModel(function=function, stream_function=gate.stream), deps_type=Runtime)


class _FakeProjects:
    """A stand-in for the projection: which project a root is, and where a session's home is.

    `wait` holds one session's home lookup open, which is how the per-id hydration locks are
    proven: an unrelated session must not queue behind a lookup that is still out. `reached` fires
    when the waiting lookup is actually out, so the test waits on a fact rather than on a clock.
    """

    def __init__(self, *, homes: dict[str, Path] | None = None, roots: dict[str, Path] | None = None) -> None:
        self.homes = dict(homes or {})
        self.roots = dict(roots or {})
        self.reached: dict[str, asyncio.Event] = {}
        self.release: dict[str, asyncio.Event] = {}

    def wait(self, session_id: str) -> None:
        """Hold `session_id`'s home lookup open until `release[session_id]` is set."""
        self.reached[session_id] = asyncio.Event()
        self.release[session_id] = asyncio.Event()

    def id_of(self, root: Path) -> str:
        return f"project-{root.name}"

    async def root_of(self, project_id: str) -> Path | None:
        return self.roots.get(project_id)

    async def root_of_session(self, session_id: str) -> Path | None:
        arrived = self.reached.get(session_id)
        if arrived is not None:
            arrived.set()
        held = self.release.get(session_id)
        if held is not None:
            await held.wait()
        return self.homes.get(session_id)


def _sessions(pool: SessionPool, *, projected: FakePool | None = None) -> SessionsController:
    """The sessions controller over `pool`, with a projection that records the statements it is given.

    The routes driven through this controller only ever delete rows.
    """
    return SessionsController(
        projection=SessionProjection(projected if projected is not None else FakePool()), sessions=pool, topics=FakeBroadcaster()
    )


def _default_runtime(
    agent: Agent[Runtime, str] | None = None, *, registry: StepRegistry | None = None, home: Path, store: SessionTranscript | None = None
) -> Runtime:
    """The runtime the process's own session opens against `home`, with a stand-in library nothing here calls.

    `store` is the record every session in the pool writes through, a file transcript under `home`
    unless a case wants one whose erasures it can read back.
    """
    return Runtime(
        registry=registry if registry is not None else StepRegistry(),
        library=cast("Any", None),
        agent=agent if agent is not None else cast("Any", object()),
        session_log=a_session_log(home),
        store=store if store is not None else a_transcript(home),
        state=a_state(home),
    )


@asynccontextmanager
async def _studio(
    agent: Agent[Runtime, str],
    home: Path,
    *,
    projects: _FakeProjects | None = None,
    registry: StepRegistry | None = None,
    store: SessionTranscript | None = None,
) -> AsyncGenerator[tuple[httpx.AsyncClient, SessionPool]]:
    """The real app over ASGI answering from this test's own pool, torn back down to a clean process.

    The application is entered, which is what mounts its controllers, and the container answers the
    pool this test built: what a route resolves is the very pool the assertions read. The state home
    lives exactly as long as the client, so no test writes into the real one.
    """
    pool = SessionPool(
        _default_runtime(agent, registry=registry, home=home, store=store), projects=projects if projects is not None else _FakeProjects()
    )
    async with a_served_client(state=home, answering={Runtime: pool.default, SessionPool: pool}) as client:
        yield client, pool


def _chunks(body: str) -> list[dict[str, Any]]:
    """The SSE body as protocol chunks, in the order they arrived; the [DONE] terminator dropped."""
    chunks: list[dict[str, Any]] = []
    for line in body.splitlines():
        if not line.startswith("data: "):
            continue
        data = line.removeprefix("data: ")
        if data == "[DONE]":
            continue
        chunks.append(json.loads(data))
    return chunks


def _kinds(home: Path) -> list[str]:
    return [record.event.kind for record in a_session_log(home).events()]


async def _new_session(client: httpx.AsyncClient) -> str:
    created = await client.post("/api/sessions", json={})
    assert created.status_code == 200
    session_id = created.json()["session"]["id"]
    assert isinstance(session_id, str)
    return session_id


async def _seed(recorder: SessionLog, store: StepStore, *, session_id: str, run_id: str, said: str) -> None:
    """One settled turn for `session_id` on `recorder`'s timeline, with its snapshot in `store`."""
    await store.register_run(RunRecord(run_id=run_id, started_at=datetime.now(tz=UTC)))
    await store.save_snapshot(
        ContinuableSnapshot(run_id=run_id, step_index=0, messages=[ModelRequest(parts=[UserPromptPart(content=said)])])
    )
    recorder.record(SessionStarted(session_id=session_id, session_kind="chat"))
    started = recorded(recorder, TurnStarted(entry="spa", chars=len(said), session_id=session_id, prompt_excerpt=None))
    recorder.record(TurnSettled(run_id=run_id, started_seq=started.seq))


async def _forever() -> None:
    await asyncio.Event().wait()


async def _waiting(runtime: Runtime) -> asyncio.Future[GateVerdict]:
    """The gate `runtime`'s run waiting at, once it gets there."""
    for _ in range(500):
        if runtime.active.decision is not None:
            return runtime.active.decision
        await asyncio.sleep(0.01)
    msg = "the run never waiting at its gate"
    raise AssertionError(msg)


def _recorded(runtime: Runtime) -> list[str]:
    """Every event kind on this session's OWN session log file, in order."""
    assert runtime.workspace is not None, "a project session narrates through its workspace session log"
    return [record.event.kind for record in runtime.workspace.session_log.events()]


def _run_recorded(run_id: str | None, session: Runtime) -> list[str]:
    """Every event kind in one run's OWN log, under the home of the session that launched it."""
    assert run_id is not None, "the launch mints the run id its own log is filed under"
    return [entry.event.kind for entry in run_events.events_for(session.state, run_id)]


async def _refused(client: httpx.AsyncClient, path: str, json_body: dict[str, object] | None = None) -> httpx.Response:
    """A request that must come straight back with a refusal, never wait behind the live turn.

    Bounded on purpose: a lost guard would admit the turn, and the transport does not hand back a
    response until the whole stream is written -- so the failure would be a hung suite rather than
    a red test.
    """
    return await asyncio.wait_for(client.post(path, json=json_body), timeout=5)


async def test_a_created_session_answers_a_streamed_turn(tmp_path: Path) -> None:
    gate = _Gate(released=True)

    async with _studio(_gated_agent(gate), tmp_path) as (client, _pool):
        session_id = await _new_session(client)

        response = await client.post(f"/api/sessions/{session_id}/messages", json=submit("hello"))

        assert response.status_code == 200
        assert response.headers["content-type"].startswith("text/event-stream")
        settles = [chunk for chunk in _chunks(response.text) if chunk.get("type") == "data-settle"]
        assert len(settles) == 1, "the turn's settled payload rides exactly one data-settle chunk"
        assert settles[0]["data"]["message"] == "ok"
        stamps = [record.event.session_id for record in a_session_log(tmp_path).events() if isinstance(record.event, TurnStarted)]
        assert stamps == [session_id], "the turn is stamped with the session it belongs to"


async def test_two_sessions_answer_at_the_same_time(tmp_path: Path) -> None:
    gate = _Gate(expected=2)

    async with _studio(_gated_agent(gate), tmp_path) as (client, _pool):
        first, second = await _new_session(client), await _new_session(client)
        turns = [
            asyncio.create_task(client.post(f"/api/sessions/{session_id}/messages", json=submit("hi"))) for session_id in (first, second)
        ]

        await asyncio.wait_for(gate.arrived.wait(), timeout=5)
        gate.release.set()
        answered = await asyncio.gather(*turns)

        assert [response.status_code for response in answered] == [200, 200]
        assert all(any(chunk.get("type") == "data-settle" for chunk in _chunks(response.text)) for response in answered)


async def test_a_second_turn_in_a_busy_session_is_refused_while_the_first_streams(tmp_path: Path) -> None:
    gate = _Gate()

    async with _studio(_gated_agent(gate), tmp_path) as (client, _pool):
        session_id = await _new_session(client)
        turn = asyncio.create_task(client.post(f"/api/sessions/{session_id}/messages", json=submit("hi")))
        await asyncio.wait_for(gate.arrived.wait(), timeout=5)

        refused = await _refused(client, f"/api/sessions/{session_id}/messages", submit("again"))

        assert refused.status_code == 409
        assert "already streaming" in refused.json()["detail"], "the refusal says why, in words the composer shows"
        gate.release.set()
        assert (await turn).status_code == 200


async def test_steer_reaches_the_running_turn_and_only_a_running_one(tmp_path: Path) -> None:
    gate = _Gate()

    async with _studio(_gated_agent(gate), tmp_path) as (client, pool):
        session_id = await _new_session(client)
        turn = asyncio.create_task(client.post(f"/api/sessions/{session_id}/messages", json=submit("hi")))
        await asyncio.wait_for(gate.arrived.wait(), timeout=5)

        steered = await client.post(f"/api/sessions/{session_id}/steer", json={"message": "also check the tests"})

        assert steered.status_code == 200
        runtime = await pool.get(session_id)
        assert runtime.steer_inbox == ["also check the tests"]
        assert _kinds(tmp_path)[-1] == "turn_steered", "a steer is on the timeline like any other act on the session"
        assert (await client.post(f"/api/sessions/{session_id}/steer", json={"message": "   "})).status_code == 400
        live = live_prompt_messages(pool, session_id, base_idx=2)
        assert [(row.idx, row.kind, row.text) for row in live] == [(2, "request", "hi"), (3, "request", "also check the tests")]
        assert live[0].payload == {"kind": "request", "parts": [{"part_kind": "user-prompt", "content": "hi"}]}

        gate.release.set()
        assert (await turn).status_code == 200
        assert live_prompt_messages(pool, session_id, base_idx=0) == [], "an ended turn overlays nothing however it ended"
        assert (await client.post(f"/api/sessions/{session_id}/steer", json={"message": "too late"})).status_code == 409
        assert (await client.post(f"/api/sessions/{UNKNOWN}/steer", json={"message": "x"})).status_code == 404


async def test_a_new_turn_clears_a_steer_no_turn_picked_up(tmp_path: Path) -> None:
    gate = _Gate()

    async with _studio(_gated_agent(gate), tmp_path) as (client, pool):
        session_id = await _new_session(client)
        turn = asyncio.create_task(client.post(f"/api/sessions/{session_id}/messages", json=submit("hi")))
        await asyncio.wait_for(gate.arrived.wait(), timeout=5)
        await client.post(f"/api/sessions/{session_id}/steer", json={"message": "never read"})
        assert (await client.post(f"/api/sessions/{session_id}/interrupt")).status_code == 200
        await asyncio.wait([turn])

        gate.release.set()
        follow_up = await client.post(f"/api/sessions/{session_id}/messages", json=submit("fresh"))

        assert follow_up.status_code == 200
        runtime = await pool.get(session_id)
        assert runtime.steer_inbox == [], "a steer the dead turn never read does not leak into the next one"


async def test_interrupt_abandons_the_turn_and_frees_the_session(tmp_path: Path) -> None:
    gate = _Gate()

    async with _studio(_gated_agent(gate), tmp_path) as (client, _pool):
        session_id = await _new_session(client)
        turn = asyncio.create_task(client.post(f"/api/sessions/{session_id}/messages", json=submit("hi")))
        await asyncio.wait_for(gate.arrived.wait(), timeout=5)

        stopped = await client.post(f"/api/sessions/{session_id}/interrupt")

        assert stopped.status_code == 200
        await asyncio.wait([turn])
        assert _kinds(tmp_path)[-1] == "turn_abandoned", "the cancelled turn is on the timeline, not left open until the next boot"
        assert (await client.post(f"/api/sessions/{session_id}/interrupt")).status_code == 409, "nothing is in flight now"

        gate.release.set()
        follow_up = await client.post(f"/api/sessions/{session_id}/messages", json=submit("again"))
        assert follow_up.status_code == 200, "an interrupted session is usable immediately"


async def test_a_client_that_hangs_up_mid_stream_hands_the_session_back(tmp_path: Path) -> None:
    """The hold is the stream's, not the handler's, so the stream dying is what gives it back."""
    gate = _Gate()

    async with _studio(_gated_agent(gate), tmp_path) as (client, _pool):
        session_id = await _new_session(client)
        turn = asyncio.create_task(client.post(f"/api/sessions/{session_id}/messages", json=submit("hi")))
        await asyncio.wait_for(gate.arrived.wait(), timeout=5)

        turn.cancel()
        await asyncio.wait([turn])

        assert _kinds(tmp_path)[-1] == "turn_abandoned", "a turn nobody is listening to is closed on the spot"
        gate.release.set()
        follow_up = await _refused(client, f"/api/sessions/{session_id}/messages", submit("again"))
        assert follow_up.status_code == 200, "the abandoned turn released the session it was holding"


async def test_an_unknown_session_is_a_404_on_every_scoped_door(tmp_path: Path) -> None:
    async with _studio(_gated_agent(_Gate(released=True)), tmp_path) as (client, _pool):
        assert (await client.post(f"/api/sessions/{UNKNOWN}/messages", json=submit("hi"))).status_code == 404
        assert (await client.post(f"/api/sessions/{UNKNOWN}/interrupt")).status_code == 404
        assert (await client.get(f"/api/sessions/{UNKNOWN}/conversation")).status_code == 404
        assert (await client.post(f"/api/sessions/{UNKNOWN}/run", json={"workflow_id": "nope"})).status_code == 404
        assert (await client.post(f"/api/sessions/{UNKNOWN}/run/pause")).status_code == 404
        assert (await client.post(f"/api/sessions/{UNKNOWN}/run/resume", json={})).status_code == 404
        assert (await client.get(f"/api/sessions/{UNKNOWN}/run/state")).status_code == 404
        assert (await client.post(f"/api/sessions/{UNKNOWN}/decide", json={"approve": True})).status_code == 404
        assert (await client.post(f"/api/sessions/{UNKNOWN}/save")).status_code == 404


async def test_scoped_save_saves_that_sessions_draft_and_only_that(
    tmp_path: Path, quarterly_report: Workflow, registry: StepRegistry
) -> None:
    async with _studio(_gated_agent(_Gate(released=True)), tmp_path, registry=registry) as (client, pool):
        drafting, idle = await pool.create(None), await pool.create(None)
        drafting.library = FolderLibrary(tmp_path / "workflows")
        drafting.conversation.draft = Draft(
            spec_yaml=yaml.safe_dump(quarterly_report.model_dump(mode="json"), sort_keys=False),
            title="Quarterly report",
            summary="Close the quarter",
            applicability="Use for quarterly closes. Not for ad-hoc reports.",
            presentation=WorkflowPresentation(
                purpose="Close one quarter's books.",
                produces="The published quarterly report.",
                owner_participation="Approve the draft before it is published.",
                not_for="A question about a single month.",
            ),
        )

        saved = await client.post(f"/api/sessions/{drafting.session_id}/save")

        assert saved.status_code == 200
        body = saved.json()
        assert body["ok"] is True
        assert "quarterly-report" in body["id"]
        assert await drafting.library.fetch_workflow(body["id"]) is not None

        refused = await client.post(f"/api/sessions/{idle.session_id}/save")
        assert refused.status_code == 400, "a session with nothing on screen has nothing to save"


async def test_an_unknown_project_id_refuses_to_open_a_session(tmp_path: Path) -> None:
    async with _studio(_gated_agent(_Gate(released=True)), tmp_path) as (client, _pool):
        refused = await client.post("/api/sessions", json={"project_id": "no-such-project"})

        assert refused.status_code == 404


async def test_a_session_opens_on_a_named_projects_own_home(tmp_path: Path) -> None:
    home = tmp_path / "client-repo"
    home.mkdir()
    projects = _FakeProjects(roots={"project-client-repo": home})

    async with _studio(_gated_agent(_Gate(released=True)), tmp_path, projects=projects) as (client, pool):
        created = await client.post("/api/sessions", json={"project_id": "project-client-repo"})

        assert created.status_code == 200
        summary = created.json()["session"]
        assert summary["project_id"] == "project-client-repo"
        runtime = await pool.get(summary["id"])
        assert runtime.workspace is not None
        assert runtime.workspace.root == home
        assert _kinds(tmp_path) == ["boot"], "a project's session is narrated in the project's session log, not the checkout's"


async def test_the_scoped_conversation_carries_the_draft_and_the_dialogue(tmp_path: Path) -> None:
    async with _studio(_gated_agent(_Gate(released=True)), tmp_path) as (client, pool):
        runtime = await pool.create(None)
        runtime.conversation.history = [ModelRequest(parts=[UserPromptPart(content="draft me one")])]
        runtime.conversation.draft = Draft(spec_yaml="not: valid: yaml:", title="Nightly sweep")

        answered = await client.get(f"/api/sessions/{runtime.session_id}/conversation")

        assert answered.status_code == 200
        body = answered.json()
        assert body["turns"] == [{"who": "you", "text": "draft me one"}]
        assert body["draft"]["title"] == "Nightly sweep"
        assert body["valid"] is False
        assert body["issues"], "the inspector must survive a refresh, verdict included"
        assert body["graph"] is None


async def test_scoped_decide_answers_only_its_own_sessions_gate(tmp_path: Path) -> None:
    async with _studio(_gated_agent(_Gate(released=True)), tmp_path) as (client, pool):
        first, second = await pool.create(None), await pool.create(None)
        for runtime in (first, second):
            runtime.active.pending = PendingReview(gate_id="approve", workflow="sweep", reviewing="the draft", rejection_reruns="draft")
            runtime.active.decision = asyncio.get_running_loop().create_future()

        answered = await client.post(f"/api/sessions/{first.session_id}/decide", json={"approve": True})

        assert answered.status_code == 200
        assert first.active.decision is not None
        assert first.active.decision.result() == GateVerdict(approve=True)
        assert second.active.decision is not None
        assert not second.active.decision.done(), "a gate is answered in the session that waiting at it, and nowhere else"
        second.active.decision.cancel()


async def test_api_runs_lists_one_card_per_session_with_a_live_run(tmp_path: Path) -> None:
    async with _studio(_gated_agent(_Gate(released=True)), tmp_path) as (client, pool):
        running, waiting, idle = await pool.create(None), await pool.create(None), await pool.create(None)
        started: list[asyncio.Task[None]] = []
        for runtime, workflow in ((running, "sweep"), (waiting, "report")):
            runtime.active.workflow_id = workflow
            runtime.active.task = asyncio.create_task(_forever())
            started.append(runtime.active.task)
        running.active.progress.started("fetch")
        waiting.active.pending = PendingReview(gate_id="approve", workflow="report", reviewing="the draft", rejection_reruns="draft_report")

        try:
            listed = await client.get("/api/runs")
        finally:
            for task in started:
                task.cancel()
            await asyncio.gather(*started, return_exceptions=True)

        assert listed.status_code == 200
        rows = {row["session_id"]: row for row in listed.json()["runs"]}
        assert set(rows) == {running.session_id, waiting.session_id}
        assert rows[running.session_id]["state"] == "running"
        assert rows[running.session_id]["workflow_id"] == "sweep"
        assert rows[running.session_id]["active"] == ["fetch"]
        assert rows[waiting.session_id]["state"] == "waiting"
        assert rows[waiting.session_id]["waiting_at"] == "approve"
        assert rows[waiting.session_id]["gate"]["reviewing"] == "the draft"
        assert idle.session_id not in rows, "a session with no run has no card"


async def test_api_runs_reports_started_at(tmp_path: Path) -> None:
    async with _studio(_gated_agent(_Gate(released=True)), tmp_path) as (client, pool):
        stamped, unstamped = await pool.create(None), await pool.create(None)
        started: list[asyncio.Task[None]] = []
        for runtime in (stamped, unstamped):
            runtime.active.task = asyncio.create_task(_forever())
            started.append(runtime.active.task)
        stamped.active.started_at = "2026-08-24T12:00:00+00:00"

        try:
            listed = await client.get("/api/runs")
        finally:
            for task in started:
                task.cancel()
            await asyncio.gather(*started, return_exceptions=True)

        assert listed.status_code == 200
        rows = {row["session_id"]: row for row in listed.json()["runs"]}
        assert rows[stamped.session_id]["started_at"] == "2026-08-24T12:00:00+00:00"
        assert rows[unstamped.session_id]["started_at"] == "", "unstamped stays empty, never invented"


async def test_a_deleted_session_stops_answering(tmp_path: Path) -> None:
    """Delete removes the session everywhere, so the next turn in it is a 404 rather than a reply."""
    home = tmp_path / "client-repo"
    home.mkdir()
    record = FakeConversationStore()
    async with _studio(_gated_agent(_Gate(released=True)), tmp_path, store=record) as (client, pool):
        runtime = await pool.create(home)
        assert runtime.session_id is not None
        answered = await client.post(f"/api/sessions/{runtime.session_id}/messages", json=submit("hi"))
        assert answered.status_code == 200

        deleted = await _sessions(pool).delete_session(runtime.session_id)

        assert deleted.status_code == 204
        assert pool.held_sessions() == (pool.default,), "the deleted session's runtime is let go, not left chatting"
        assert record.deleted == [runtime.session_id], "the conversation goes with the session, not only its rows"
        refused = await client.post(f"/api/sessions/{runtime.session_id}/messages", json=submit("again"))
        assert refused.status_code == 404


async def test_deleting_one_session_erases_that_conversation_and_no_other(tmp_path: Path) -> None:
    """The cascade is keyed on the session being deleted, so a neighbour's transcript survives it."""
    home = tmp_path / "client-repo"
    home.mkdir()
    record = FakeConversationStore()
    async with _studio(_gated_agent(_Gate(released=True)), tmp_path, store=record) as (client, pool):
        doomed, spared = await pool.create(home), await pool.create(home)
        assert doomed.session_id is not None
        assert spared.session_id is not None
        for session_id in (doomed.session_id, spared.session_id):
            assert (await client.post(f"/api/sessions/{session_id}/messages", json=submit("hi"))).status_code == 200

        deleted = await _sessions(pool).delete_session(doomed.session_id)

        assert deleted.status_code == 204
        assert record.deleted == [doomed.session_id], "exactly one conversation was erased, and it was that session's"
        assert (await client.post(f"/api/sessions/{spared.session_id}/messages", json=submit("still here?"))).status_code == 200


async def test_a_pooled_sessions_run_is_recorded_on_its_own_project(
    tmp_path: Path, quarterly_report: Workflow, registry: StepRegistry
) -> None:
    """The launch is the session's own fact; what became of the run belongs to the run."""
    home = tmp_path / "client-repo"
    home.mkdir()

    async with _studio(_gated_agent(_Gate(released=True)), tmp_path, registry=registry) as (_client, pool):
        runtime = await pool.create(home)

        service.launch(
            runtime,
            records=records_under(tmp_path),
            topics=FakeBroadcaster(),
            spec=quarterly_report,
            workflow_id="wf.report",
            params={"quarter": "Q2"},
        )
        run_id = runtime.active.run_id
        (await _waiting(runtime)).set_result(GateVerdict(approve=True))
        task = runtime.active.task
        assert task is not None
        await task

        assert _recorded(runtime) == ["session_started", "run_launched"], "the session log says it launched one, and no more"
        assert _run_recorded(run_id, runtime) == ["run_launched", "gate_reached", "run_finished"]
        assert _kinds(tmp_path) == ["boot"], (
            "the checkout's timeline carries this application's own boot and nothing of another project's run"
        )


async def test_a_scoped_pause_is_recorded_on_its_own_project(tmp_path: Path, quarterly_report: Workflow, registry: StepRegistry) -> None:
    """The pause is the paused run's own fact, and the session log carries nothing about it."""
    home = tmp_path / "client-repo"
    home.mkdir()

    async with _studio(_gated_agent(_Gate(released=True)), tmp_path, registry=registry) as (client, pool):
        runtime = await pool.create(home)
        service.launch(
            runtime,
            records=records_under(tmp_path),
            topics=FakeBroadcaster(),
            spec=quarterly_report,
            workflow_id="wf.report",
            params={"quarter": "Q2"},
        )
        run_id = runtime.active.run_id
        await _waiting(runtime)

        paused = await client.post(f"/api/sessions/{runtime.session_id}/run/pause")

        assert paused.status_code == 200
        assert _recorded(runtime) == ["session_started", "run_launched"], "a pause is not a fact about the conversation"
        assert _run_recorded(run_id, runtime) == ["run_launched", "gate_reached", "run_paused"]
        assert _kinds(tmp_path) == ["boot"], (
            "the checkout's timeline carries this application's own boot and nothing of another project's pause"
        )


async def test_a_scoped_gate_decision_is_recorded_on_its_own_project(tmp_path: Path) -> None:
    """The verdict answers a gate in one run, so it is that run's own event and nobody else's."""
    home = tmp_path / "client-repo"
    home.mkdir()

    async with _studio(_gated_agent(_Gate(released=True)), tmp_path) as (client, pool):
        runtime = await pool.create(home)
        runtime.active.run_id = "20260903T120000-decidedq"
        runtime.active.workflow_id = "wf.sweep"
        runtime.active.pending = PendingReview(gate_id="approve", workflow="sweep", reviewing="the draft", rejection_reruns="draft")
        runtime.active.decision = asyncio.get_running_loop().create_future()

        answered = await client.post(f"/api/sessions/{runtime.session_id}/decide", json={"approve": True})

        assert answered.status_code == 200
        assert _recorded(runtime) == ["session_started"], "a gate decision is a fact about the run, not the conversation"
        assert _run_recorded(runtime.active.run_id, runtime) == ["gate_decided"]
        assert _kinds(tmp_path) == ["boot"], (
            "the checkout's timeline carries this application's own boot and nothing of another project's gate"
        )


async def test_a_session_whose_project_root_is_gone_is_refused_as_gone(tmp_path: Path) -> None:
    """A project that has been moved or deleted is said to be gone, and is not put back on disk."""
    gone = tmp_path / "deleted-repo"
    projects = _FakeProjects(homes={STORED: gone}, roots={"project-deleted-repo": gone})

    async with _studio(_gated_agent(_Gate(released=True)), tmp_path, projects=projects) as (client, _pool):
        hydrating = await client.get(f"/api/sessions/{STORED}/conversation")
        opening = await client.post("/api/sessions", json={"project_id": "project-deleted-repo"})

        assert hydrating.status_code == 410, "the id is right and the project is not there any more"
        assert hydrating.json()["root"] == str(gone)
        assert opening.status_code == 410
        assert not gone.exists(), "asking for a session must never recreate the project it named"


async def test_a_session_whose_project_root_is_gone_can_still_be_deleted(tmp_path: Path) -> None:
    """Delete removes the projected rows of a session whose folder is gone, rather than refusing it as gone."""
    gone = tmp_path / "deleted-repo"
    projects = _FakeProjects(homes={STORED: gone})
    projected = FakePool()
    record = FakeConversationStore()
    async with _studio(_gated_agent(_Gate(released=True)), tmp_path, projects=projects, store=record) as (_client, pool):
        deleted = await _sessions(pool, projected=projected).delete_session(STORED)

        assert deleted.status_code == 204
        assert any(sql.startswith("DELETE FROM sessions") and args == (STORED,) for sql, args in projected.statements)
        assert record.deleted == [], "there is no runtime to reach a record through, so only the rows go"
        assert not gone.exists(), "deleting a session must never recreate the project it named"


async def test_a_session_from_an_earlier_process_hydrates_from_its_recorded_home(tmp_path: Path) -> None:
    home = tmp_path / "client-repo"
    home.mkdir()
    workspace = open_workspace(home)
    await _seed(workspace.session_log, a_transcript(tmp_path), session_id=STORED, run_id="r9", said="yesterday's talk")
    pool = SessionPool(_default_runtime(home=tmp_path), projects=_FakeProjects(homes={STORED: home}))

    runtime = await pool.get(STORED)

    assert runtime.workspace is not None
    assert runtime.workspace.root == home, "nothing in memory named this home; the projection did"
    part = runtime.conversation.history[-1].parts[0]
    assert isinstance(part, UserPromptPart)
    assert part.content == "yesterday's talk"


async def test_unrelated_sessions_never_wait_behind_one_hydration(tmp_path: Path) -> None:
    home = tmp_path / "slow-project"
    home.mkdir()
    workspace = open_workspace(home)
    await _seed(workspace.session_log, a_transcript(tmp_path), session_id=STORED, run_id="r1", said="slow talk")
    await _seed(
        SessionLog(a_state(tmp_path).session_log, topics=None), a_transcript(tmp_path), session_id=OTHER, run_id="r2", said="quick talk"
    )
    projects = _FakeProjects(homes={STORED: home})
    projects.wait(STORED)
    pool = SessionPool(_default_runtime(home=tmp_path), projects=projects)

    slow = asyncio.create_task(pool.get(STORED))
    await asyncio.wait_for(projects.reached[STORED].wait(), timeout=5)

    quick = await asyncio.wait_for(pool.get(OTHER), timeout=2)

    assert quick.session_id == OTHER, "one session's slow home lookup must not hold every other session up"
    projects.release[STORED].set()
    assert (await slow).session_id == STORED


if __name__ == "__main__":  # pragma: no cover
    pytest.main([__file__])
