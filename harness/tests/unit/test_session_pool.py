"""The pool of runtimes: one process, many sessions, one agent.

Every case opens its own state home -- the session log and the transcript a hydration reads back
are the ones the test itself seeded, with nothing else patched.

The clock is an argument rather than `time.monotonic`, so the eviction test winds it forward by
hand instead of sleeping or patching the module.
"""

from __future__ import annotations

import asyncio

from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any, cast

import pytest

from core.ai.workflows.contracts import StepRegistry
from pydantic_ai.messages import ModelRequest, UserPromptPart
from pydantic_ai_harness.step_persistence import ContinuableSnapshot, RunRecord

from harness import session as session_state
from harness.domain.events import SessionDeleted, SessionStarted, TurnSettled, TurnStarted
from harness.features.running.domain.progress import PendingReview
from harness.main import sweep_idle_once
from harness.session import NoSessionLog, Runtime, SessionPool
from harness.workspace import open_workspace
from tests.support.fakes.broadcaster import FakeBroadcaster
from tests.support.state import a_session_log, a_state, a_transcript

if TYPE_CHECKING:
    from pathlib import Path

STORED = "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"
UNKNOWN = "bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb"


def _default_session(home: Path) -> Runtime:
    """The process's own runtime under `home`, with stand-in agent and store objects.

    Neither is ever called here: what the tests read off them is identity -- that every pooled
    runtime holds the same two objects.
    """
    return Runtime(
        registry=StepRegistry(),
        library=cast("Any", object()),
        agent=cast("Any", object()),
        session_log=a_session_log(home),
        store=a_transcript(home),
        state=a_state(home),
    )


class _Clock:
    """A hand-wound monotonic clock: the seam `SessionPool` reads instead of `time.monotonic`."""

    def __init__(self) -> None:
        self.seconds = 0.0

    def __call__(self) -> float:
        return self.seconds


async def _seed_stored_session(session_id: str, tmp_path: Path, *, run_id: str, said: str) -> None:
    """One settled turn on the checkout's timeline, with its snapshot in the transcript."""
    store = a_transcript(tmp_path)
    await store.register_run(RunRecord(run_id=run_id, started_at=datetime.now(tz=UTC)))
    await store.save_snapshot(
        ContinuableSnapshot(run_id=run_id, step_index=0, messages=[ModelRequest(parts=[UserPromptPart(content=said)])])
    )
    a_session_log(tmp_path).record(SessionStarted(session_id=session_id, session_kind="chat"))
    started = a_session_log(tmp_path).record(TurnStarted(entry="spa", chars=len(said), session_id=session_id, prompt_excerpt=None))
    assert started is not None
    a_session_log(tmp_path).record(TurnSettled(run_id=run_id, started_seq=started.seq))


async def test_two_created_sessions_hold_disjoint_conversations(tmp_path: Path) -> None:
    default = _default_session(tmp_path)
    pool = SessionPool(default)

    first = await pool.create(None)
    second = await pool.create(None)

    assert first is not second
    assert first.session_id != second.session_id
    first.conversation.history.append(ModelRequest(parts=[UserPromptPart(content="only mine")]))
    assert second.conversation.history == [], "one conversation per session, never a shared list"
    assert first.agent is second.agent is default.agent, "one process, one agent"
    assert first.registry is second.registry is default.registry
    assert first.library is second.library is default.library
    minted = [record.event.session_id for record in a_session_log(tmp_path).events() if isinstance(record.event, SessionStarted)]
    assert minted == [first.session_id, second.session_id], "a created session is on the timeline before its first turn"


async def test_get_hydrates_a_stored_session_from_the_journal_and_the_step_log(tmp_path: Path) -> None:
    await _seed_stored_session(STORED, tmp_path, run_id="r1", said="stored talk")
    pool = SessionPool(_default_session(tmp_path))

    runtime = await pool.get(STORED)

    assert runtime.session_id == STORED
    part = runtime.conversation.history[-1].parts[0]
    assert isinstance(part, UserPromptPart)
    assert part.content == "stored talk"
    assert await pool.get(STORED) is runtime, "a hydrated runtime is held open, not rebuilt per request"


async def test_two_requests_arriving_together_hydrate_one_runtime(tmp_path: Path) -> None:
    await _seed_stored_session(STORED, tmp_path, run_id="r1", said="stored talk")
    pool = SessionPool(_default_session(tmp_path))

    first, second = await asyncio.gather(pool.get(STORED), pool.get(STORED))

    assert first is second, "two runtimes for one session would answer into conversations neither can see"


async def test_an_unknown_session_id_is_a_key_error(tmp_path: Path) -> None:
    pool = SessionPool(_default_session(tmp_path))

    with pytest.raises(KeyError):
        await pool.get(UNKNOWN)


async def test_a_held_lock_is_what_answering_reports(tmp_path: Path) -> None:
    pool = SessionPool(_default_session(tmp_path))
    runtime = await pool.create(None)
    assert runtime.session_id is not None

    assert not pool.is_answering(runtime.session_id)
    hold = await pool.begin_turn(runtime.session_id)
    assert pool.is_answering(runtime.session_id), "a turn holding the session is what the next turn is refused on"
    hold.release()
    assert not pool.is_answering(runtime.session_id)
    assert not pool.is_answering(UNKNOWN), "a session this process is not holding cannot be mid-turn"


async def test_eviction_drops_an_idle_runtime_and_get_rebuilds_it(tmp_path: Path) -> None:
    clock = _Clock()
    pool = SessionPool(_default_session(tmp_path), clock=clock)
    runtime = await pool.create(None)
    assert runtime.session_id is not None
    clock.seconds = 31 * 60

    pool.evict_idle()

    rebuilt = await pool.get(runtime.session_id)
    assert rebuilt is not runtime, "the idle runtime was dropped; get built a fresh one from the record"
    assert rebuilt.session_id == runtime.session_id


async def test_a_runtime_mid_turn_survives_the_idle_sweep(tmp_path: Path) -> None:
    clock = _Clock()
    pool = SessionPool(_default_session(tmp_path), clock=clock)
    runtime = await pool.create(None)
    assert runtime.session_id is not None
    clock.seconds = 31 * 60

    hold = await pool.begin_turn(runtime.session_id)
    try:
        pool.evict_idle()

        assert await pool.get(runtime.session_id) is runtime, "a turn in flight is never swept out from under itself"
    finally:
        hold.release()


async def test_a_run_waiting_at_a_gate_survives_the_idle_sweep(tmp_path: Path) -> None:
    """A button-launched run holds no turn lock at all, so the sweep must read the gate itself."""
    clock = _Clock()
    pool = SessionPool(_default_session(tmp_path), clock=clock)
    runtime = await pool.create(None)
    assert runtime.session_id is not None
    runtime.active.pending = PendingReview(gate_id="approve", workflow="wf.report", reviewing="draft", rejection_reruns="draft_report")
    clock.seconds = 31 * 60

    pool.evict_idle()

    assert await pool.get(runtime.session_id) is runtime, "a waiting gate is never swept out from under itself"


async def _wait_forever(hold: asyncio.Event) -> None:
    await hold.wait()


async def test_a_live_run_task_survives_the_idle_sweep(tmp_path: Path) -> None:
    """A running task with nothing waiting yet is exactly as live -- also unguarded by the turn lock."""
    clock = _Clock()
    pool = SessionPool(_default_session(tmp_path), clock=clock)
    runtime = await pool.create(None)
    assert runtime.session_id is not None
    hold = asyncio.Event()
    runtime.active.task = asyncio.create_task(_wait_forever(hold))
    clock.seconds = 31 * 60

    try:
        pool.evict_idle()
        assert await pool.get(runtime.session_id) is runtime, "a live run's task is never swept out from under itself"
    finally:
        hold.set()
        assert runtime.active.task is not None
        await runtime.active.task


async def test_an_idle_runtime_with_no_run_still_evicts(tmp_path: Path) -> None:
    """No turn lock, no live task, no waiting gate: the ordinary idle case still sweeps clean."""
    clock = _Clock()
    pool = SessionPool(_default_session(tmp_path), clock=clock)
    runtime = await pool.create(None)
    assert runtime.session_id is not None
    clock.seconds = 31 * 60

    pool.evict_idle()

    rebuilt = await pool.get(runtime.session_id)
    assert rebuilt is not runtime, "nothing kept it, so the sweep drops it exactly as before"


async def test_the_serving_process_sweeps_idle_runtimes(tmp_path: Path) -> None:
    """Nothing evicts on its own: the server's own sweep is what calls it, and the test drives it."""
    clock = _Clock()
    pool = SessionPool(_default_session(tmp_path), clock=clock)
    runtime = await pool.create(None)
    assert runtime.session_id is not None
    clock.seconds = 31 * 60

    sweep_idle_once(pool, FakeBroadcaster())

    assert pool.held_sessions() == (pool.default,), "the sweep drops what nobody has touched"
    assert (await pool.get(runtime.session_id)).session_id == runtime.session_id, "a swept session is rebuilt on demand"


async def test_a_dropped_session_is_no_longer_held_and_is_not_rebuilt(tmp_path: Path) -> None:
    """A deleted session is dropped from the pool, and its own timeline never brings it back."""
    pool = SessionPool(_default_session(tmp_path))
    runtime = await pool.create(None)
    assert runtime.session_id is not None
    a_session_log(tmp_path).record(SessionDeleted(session_id=runtime.session_id))

    assert pool.drop(runtime.session_id) is True
    assert pool.held_sessions() == (pool.default,)

    with pytest.raises(KeyError):
        await pool.get(runtime.session_id)

    assert pool.drop(runtime.session_id) is False, "there is nothing left to drop"


async def test_the_default_session_and_a_session_mid_turn_are_never_dropped(tmp_path: Path) -> None:
    pool = SessionPool(_default_session(tmp_path))
    pool.default.session_id = STORED
    runtime = await pool.create(None)
    assert runtime.session_id is not None

    hold = await pool.begin_turn(runtime.session_id)
    try:
        assert pool.drop(runtime.session_id) is False, "a turn in flight owns the runtime it is answering into"
        assert pool.drop(STORED) is False, "the process's own session is what every unscoped route acts on"
    finally:
        hold.release()

    assert await pool.get(runtime.session_id) is runtime


async def test_the_pools_default_is_the_process_own_session(tmp_path: Path) -> None:
    default = _default_session(tmp_path)
    pool = SessionPool(default)

    pooled = await pool.create(None)

    assert pooled is not default
    assert pool.default is default, "a pooled session never displaces the process's own"
async def test_a_project_session_lives_in_the_projects_own_state_home(tmp_path: Path) -> None:
    pool = SessionPool(_default_session(tmp_path))

    runtime = await pool.create(tmp_path)

    assert runtime.workspace is not None
    assert runtime.workspace.root == tmp_path
    assert [record.event for record in runtime.workspace.session_log.events()] == [SessionStarted(session_id=runtime.session_id or "", session_kind="chat")]
    assert a_session_log(tmp_path).events() == (), "a project's session is narrated in the project's session log, not the checkout's"
    assert runtime.draft_file == tmp_path / ".aifactory" / "state" / "sessions" / f"{runtime.session_id}.draft.json"


async def test_two_runtimes_waiting_run_paths_differ(tmp_path: Path) -> None:
    pool = SessionPool(_default_session(tmp_path))
    project_runtime = await pool.create(tmp_path)

    checkout_path = session_state.run_log_path(pool.default)
    project_path = session_state.run_log_path(project_runtime)

    assert checkout_path != project_path
    assert project_runtime.workspace is not None
    assert project_path == project_runtime.workspace.state.home / "sessions" / f"{project_runtime.session_id}.waiting-run.json"


async def test_two_checkout_rooted_sessions_park_their_runs_in_different_files(tmp_path: Path) -> None:
    """Two sessions on one state home each wait their own run: one file per session, never per home."""
    pool = SessionPool(_default_session(tmp_path))
    first, second = await pool.create(None), await pool.create(None)

    first_path = session_state.run_log_path(first)
    second_path = session_state.run_log_path(second)

    assert first_path != second_path, "one waiting-run file for two sessions loses whichever run waiting first"
    assert first_path.parent == second_path.parent
    assert session_state.run_log_path(pool.default) not in {first_path, second_path}


async def test_the_process_own_session_keeps_the_legacy_waiting_run_path(tmp_path: Path) -> None:
    """The unscoped run entries and the boot-time restore read one file, and it does not move."""
    pool = SessionPool(_default_session(tmp_path))

    assert session_state.run_log_path(pool.default) == a_state(tmp_path).waiting_run(project=False)


async def test_a_settled_turn_schedules_the_projection_refresh_behind_it(tmp_path: Path) -> None:
    refreshed: list[str | None] = []

    async def refresh(runtime: Runtime) -> None:
        refreshed.append(runtime.session_id)

    runtime = _default_session(tmp_path)
    runtime.session_id = STORED
    session_state.refresh_projection(runtime, None)
    assert refreshed == [], "with no refresh to run, a settle schedules nothing"

    session_state.refresh_projection(runtime, refresh)

    assert refreshed == [], "the refresh runs behind the turn, never inside it"
    await asyncio.sleep(0)
    assert refreshed == [STORED]


def test_workspace_root_is_none_without_a_workspace(tmp_path: Path) -> None:
    assert session_state.workspace_root(_default_session(tmp_path)) is None


def test_workspace_root_is_the_workspace_own_root_when_one_is_open(tmp_path: Path) -> None:
    workspace = open_workspace(tmp_path)
    runtime = Runtime(registry=StepRegistry(), library=cast("Any", object()), agent=cast("Any", object()), workspace=workspace, session_log=NoSessionLog(), state=a_state(tmp_path))

    assert session_state.workspace_root(runtime) == tmp_path
